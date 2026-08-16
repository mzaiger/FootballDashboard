#!/usr/bin/env python3
"""
NFL Betting Dashboard builder.

Pulls this week's schedule + national broadcast network from ESPN's public
(unofficial, no-key-required) scoreboard API, then attaches DraftKings /
FanDuel spread + moneyline odds from SharpAPI (via common.py). Exports
everything to data/nfl_dashboard.json for the static nfl.html front-end.

IMPORTANT LIMITATION -- read before trusting the "regional_pick" field:
This script does NOT scrape 506sports.com's regional coverage maps. Those
pages build their market-by-market data client-side in JavaScript (a plain
HTTP GET returns an empty shell -- confirmed by hand while building this),
so a `requests`-based script can't read them, and neither could a plain
Python script Marc runs elsewhere. See README.md for what was tried and
what a real fix would require (a headless-browser scraper).

Instead, `regional_pick` is a HEURISTIC guess at what airs in the Omaha /
Lincoln, NE market: when a CBS or FOX window has more than one game, it
picks whichever game features the Kansas City Chiefs, then the Denver
Broncos (both have historically been the closest teams to that market).
This is NOT authoritative -- always cross-check at 506sports.com before
relying on it.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/build_nfl_dashboard.py
    python scripts/build_nfl_dashboard.py --week 1 --year 2026 --season-type 1
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import requests

from common import (
    DISPLAY_TIMEZONE,
    TIME_SLOT_ORDER,
    assign_matchup_ranks,
    carry_forward_odds,
    fetch_all_odds,
    load_existing_dashboard,
    load_previous_odds_by_game,
    log,
    match_odds_for_game,
    merge_weeks,
    normalize_minmax,
    time_slot_for,
)
from gemini_predictions import attach_gemini_predictions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REQUEST_TIMEOUT = 20

# seasontype: 1=preseason, 2=regular season, 3=postseason
# Used only as a last-resort fallback if ESPN's response is ever missing a
# season type entirely (see get_espn_current_state() below) -- normal runs
# don't use this constant at all anymore. A previous version of this file
# pinned --season-type's default to this constant and used it for every
# build regardless of the actual calendar, which is why the board got
# stuck showing preseason weeks (P1/P2) long after the season had already
# moved on to P3/regular season -- season type now comes from ESPN itself,
# fresh, every run, unless --season-type is explicitly passed.
SEASON_TYPE_FALLBACK = 1
SEASON_YEAR_DEFAULT = 2026

# Time Slot Best Matchup team priority: if either of these teams is playing
# in a window, they win the slot pick over the blended spread/wins score.
# Checked in order -- Chiefs first, then Broncos.
SLOT_PICK_TEAM_PRIORITY = ["Kansas City Chiefs", "Denver Broncos"]

# A team with no games played yet (e.g. before its season opener) gets this
# win-rank value -- one worse than the worst possible real rank (32 teams
# in the league) -- so it never outranks a team that actually has a record,
# mirroring how CFB treats an unranked team as rank 26 (one past AP's 25).
UNRANKED_WIN_RANK = 33


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(year, week, season_type):
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": year, "week": week, "seasontype": season_type, "limit": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def broadcast_label(event):
    """Join all national broadcast names for a game across all ESPN payload structures."""
    names = []
    
    # 1. Top-level event broadcasts
    for b in event.get("broadcasts", []):
        for n in b.get("names", []):
            if n and n not in names:
                names.append(n)
                
    # 2. Nested competition broadcasts (often populated when top-level is empty)
    for comp in event.get("competitions", []):
        for b in comp.get("broadcasts", []):
            for n in b.get("names", []):
                if n and n not in names:
                    names.append(n)
            # Check market/media type shortNames
            market = b.get("market")
            media_name = b.get("media", {}).get("shortName")
            if media_name and media_name not in names:
                names.append(media_name)

    # 3. geoBroadcasts fallback
    for gb in event.get("geoBroadcasts", []):
        short = gb.get("media", {}).get("shortName")
        if short and short not in names:
            names.append(short)

    # ", " instead of "/" between multiple networks (e.g. a game
    # simulcast on two channels) -- "/" was wrapping awkwardly inside the
    # TV pill and reading as a fraction/path rather than a list.
    return ", ".join(names) if names else "TBD"

# ---------------------------------------------------------------------------
# Matchup ranking
# ---------------------------------------------------------------------------

def matchup_score(dk_spread, fd_spread, home_win_rank, away_win_rank):
    """
    Lower score = closer / more marquee game for a betting dashboard.
    Blends two 0-1 normalized metrics 50/50 (normalization happens across
    the whole week's games in build_week, via common.normalize_minmax --
    see there for why None-handling works the way it does):

      - spread component: the smaller the point spread, the more
        competitive Vegas expects the game to be.
      - win component: combined win-rank of both teams, with each team's
        rank computed the same way CFB's AP poll works -- teams are
        ranked 1..N by this week's win percentage (best record = rank 1),
        so two unbeaten teams playing each other scores as well as two
        top-5 AP teams would in the CFB dashboard.

    `home_win_rank`/`away_win_rank` are already resolved ranks (or None
    for a team with no record yet, e.g. before its first game). Actual
    normalization/blending happens in build_week() once every game's raw
    components are known; this function just computes the raw components
    for a single game so build_week can normalize the whole batch at once.
    """
    spread = dk_spread if dk_spread is not None else fd_spread
    spread_component = abs(spread) if spread is not None else None
    win_component = None
    if home_win_rank is not None or away_win_rank is not None:
        win_component = (home_win_rank or UNRANKED_WIN_RANK) + (away_win_rank or UNRANKED_WIN_RANK)
    return spread_component, win_component


def build_win_rank_lookup(team_records):
    """Rank every team with a known record by win percentage, like a poll
    release: rank 1 = best record on the board this week. Ties broken by
    raw win total, then by team name for a stable, deterministic order.
    Teams with no games played yet (0-0) sort to the bottom together.

    `team_records` is {team_name: (wins, losses, ties)}. Returns
    {team_name: rank}.
    """
    entries = []
    for team, (wins, losses, ties) in team_records.items():
        games_played = wins + losses + ties
        pct = (wins + 0.5 * ties) / games_played if games_played else -1
        entries.append((team, pct, wins, team))
    entries.sort(key=lambda e: (-e[1], -e[2], e[3]))
    return {team: i + 1 for i, (team, pct, wins, _) in enumerate(entries)}


def _parse_espn_record(competitor):
    """Extract (wins, losses, ties, summary) from an ESPN scoreboard
    competitor's `records` list, or (None, None, None, None) if no overall
    record is present yet (e.g. before that team's first game)."""
    for rec in competitor.get("records", []):
        if rec.get("type") == "total" or rec.get("name") == "overall":
            summary = rec.get("summary")
            if not summary:
                continue
            parts = summary.split("-")
            try:
                wins, losses = int(parts[0]), int(parts[1])
                ties = int(parts[2]) if len(parts) > 2 else 0
                return wins, losses, ties, summary
            except (ValueError, IndexError):
                return None, None, None, summary
    return None, None, None, None


def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


def _dk_home_spread(odds):
    return odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")


def _fd_home_spread(odds):
    return odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")


def pick_slot_team_priority(games_in_window):
    """
    Given all games in a time-slot window, return the game featuring
    Kansas City or Denver if one is playing, else None. Checked in
    SLOT_PICK_TEAM_PRIORITY order so Chiefs win out over Broncos if both
    happen to be on at once.
    """
    for priority_team in SLOT_PICK_TEAM_PRIORITY:
        for g in games_in_window:
            if g["home_team"] == priority_team or g["away_team"] == priority_team:
                return g
    return None


# ---------------------------------------------------------------------------
# "Current week" resolution
# ---------------------------------------------------------------------------

def _extract_season_type(payload):
    """Pull the numeric season type (1/2/3) out of an ESPN scoreboard
    response. Tries the top-level `season.type` field first (usually a
    plain int for NFL), then falls back to the more deeply-nested
    `leagues[0].season.type` shape (sometimes an object like
    {"id": "2", "type": 2, ...} instead of a bare int). Returns None if
    neither is present so the caller can fall back to SEASON_TYPE_FALLBACK.
    """
    raw = payload.get("season", {}).get("type")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict):
        val = raw.get("type", raw.get("id"))
        try:
            return int(val)
        except (TypeError, ValueError):
            pass

    leagues = payload.get("leagues") or [{}]
    raw2 = (leagues[0].get("season") or {}).get("type")
    if isinstance(raw2, int):
        return raw2
    if isinstance(raw2, dict):
        val = raw2.get("type", raw2.get("id"))
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    return None


def get_espn_current_state():
    """Ask ESPN what NFL week AND season type (preseason/regular/post)
    "today" falls under, letting ESPN's own response decide the season
    type instead of us assuming one.

    Passing an explicit `dates=YYYYMMDD` for today (with NO seasontype
    filter) is what makes ESPN resolve BOTH values off the real calendar
    date, the same way the site itself does -- this is what a season-type
    transition (e.g. the last preseason week ending and the regular season
    starting) needs in order to actually be picked up automatically,
    rather than being stuck on whatever --season-type was pinned to.
    Returns (week_number, season_type).
    """
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": today_str},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    week = payload.get("week", {}).get("number", 1)
    season_type = _extract_season_type(payload)
    if season_type is None:
        log(f"  NOTE: ESPN's response didn't include a season type for {today_str} -- "
            f"falling back to season_type={SEASON_TYPE_FALLBACK}.")
        season_type = SEASON_TYPE_FALLBACK
    log(f"ESPN reports current week {week}, season_type {season_type} for {today_str}.")
    return week, season_type


def highest_stored_week_info(existing_data):
    """(week_number, last_kickoff_utc) for the latest week already sitting
    in a previously-built dashboard file, or (None, None) if there isn't
    one yet. `last_kickoff_utc` is the latest start_time among that week's
    games -- used by resolve_current() to tell whether that week is
    fully in the past."""
    best_week, best_kickoff = None, None
    for w in (existing_data or {}).get("weeks", []):
        games = [g for day in w.get("days", []) for slot in day.get("time_slots", []) for g in slot.get("games", [])]
        kickoffs = []
        for g in games:
            raw = g.get("start_time")
            if not raw:
                continue
            try:
                kickoffs.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
        last_kickoff = max(kickoffs) if kickoffs else None
        if best_week is None or w.get("week", 0) > best_week:
            best_week, best_kickoff = w.get("week"), last_kickoff
    return best_week, best_kickoff


def earliest_unelapsed_stored_week(existing_data):
    """The lowest week number already in existing_data that still has at
    least one game NOT yet kicked off, or None if every stored week is
    fully in the past (or nothing's stored yet).

    Used as a floor on "current": if we've already built a future week's
    full slate on some earlier run, current should never sit two or more
    weeks behind it, even if ESPN's own current-week answer hasn't caught
    up. This specifically catches the case highest_stored_week_info()'s
    older, simpler check couldn't: that check only looked at the SINGLE
    highest stored week, so an already-finished week between "now" and
    that highest week could stay silently stuck as "current" as long as
    something further out had already been built too.
    """
    now = datetime.now(timezone.utc)
    for w in sorted((existing_data or {}).get("weeks", []), key=lambda w: w.get("week", 0)):
        games = [g for day in w.get("days", []) for slot in day.get("time_slots", []) for g in slot.get("games", [])]
        kickoffs = []
        for g in games:
            raw = g.get("start_time")
            if not raw:
                continue
            try:
                kickoffs.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
        if kickoffs and max(kickoffs) >= now:
            return w.get("week")
    return None


def resolve_current(existing_data, forced_season_type=None):
    """Figure out which NFL week (and season type) to treat as "current"
    for this build.

    Primary source is ESPN's own answer (get_espn_current_state). On top
    of that, two safety nets guard against ESPN's answer stalling on an
    old week (observed happening -- most likely during a multi-day gap
    between two weeks, when "today" has no games of its own for ESPN to
    resolve "current" against):

    1. Floor check (earliest_unelapsed_stored_week): if a LATER week is
       already built with games still ahead of us, current is never
       allowed to sit more than one week behind it -- this is what
       actually catches a stale ESPN answer even when the OLD
       highest-stored-week check below wouldn't (that check only compares
       against the single highest week ever built, so a fully-finished
       week sitting between "now" and that highest week could get missed).
    2. Anti-regression: current is never allowed to drop below whatever
       current_week this exact season_type was already confirmed at on a
       previous run -- weeks only move forward during a season.

    `forced_season_type`, when given (via --season-type), skips
    auto-detection entirely and only resolves the week for that season
    type -- useful for manually rebuilding an older season type on demand.
    """
    if forced_season_type is not None:
        espn_week = get_espn_current_week_for(forced_season_type)
        season_type = forced_season_type
    else:
        espn_week, season_type = get_espn_current_state()

    floor_week = earliest_unelapsed_stored_week(existing_data)
    if floor_week is not None and espn_week < floor_week - 1:
        fallback_week = floor_week - 1
        log(f"  NOTE: ESPN reports week {espn_week} as current, but week {floor_week} is "
            f"already built with games still ahead of us -- using week {fallback_week} "
            f"instead so both show.")
        return fallback_week, season_type

    stored_current_week = (existing_data or {}).get("current_week")
    stored_season_type = (existing_data or {}).get("season_type")
    if (stored_current_week is not None and stored_season_type == season_type
            and espn_week < stored_current_week):
        log(f"  NOTE: ESPN reports week {espn_week} as current, but we already advanced to "
            f"week {stored_current_week} on a previous run (season_type {season_type}) -- "
            f"keeping {stored_current_week} instead of regressing.")
        return stored_current_week, season_type

    highest_week, highest_last_kickoff = highest_stored_week_info(existing_data)
    now = datetime.now(timezone.utc)
    if (highest_week is not None and highest_last_kickoff is not None
            and highest_last_kickoff < now and espn_week <= highest_week):
        fallback_week = highest_week + 1
        log(f"  NOTE: every game in stored week {highest_week} has already kicked off, but ESPN "
            f"still reports week {espn_week} as current -- using week {fallback_week} instead.")
        return fallback_week, season_type

    return espn_week, season_type


def get_espn_current_week_for(season_type):
    """Same as get_espn_current_state(), but for an explicitly-forced
    season type (--season-type was passed) rather than letting ESPN
    auto-detect one. Only the week number is used from this path."""
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"seasontype": season_type, "dates": today_str},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    week = resp.json().get("week", {}).get("number", 1)
    log(f"ESPN reports current week as {week} for {today_str} (forced season_type {season_type}).")
    return week


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build_week(year, week, season_type, sharp_key, gemini_key=None, previous_odds_by_id=None):
    """Build a single week's worth of games. Returns the per-week dict
    (no generated_at/season wrapper -- that's added once, by build())."""
    log(f"Fetching NFL schedule for {year}, week {week}, seasontype {season_type}...")
    scoreboard = get_scoreboard(year, week, season_type)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching DraftKings/FanDuel NFL odds from SharpAPI...")
    # date_from/date_to scope the request to this week's actual games
    # instead of asking for everything currently posted league-wide --
    # fewer rows/pages to page through, and less exposed to any
    # pagination edge case.
    game_dates = []
    for event in events:
        raw = event.get("date")
        if not raw:
            continue
        try:
            game_dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    date_from = min(game_dates).strftime("%Y-%m-%d") if game_dates else None
    date_to = max(game_dates).strftime("%Y-%m-%d") if game_dates else None
    odds_rows = fetch_all_odds(sharp_key, league="nfl", date_from=date_from, date_to=date_to)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    # days[date_key][slot] -> list of game entries
    days = {}
    all_games = []  # flat list, mirrors what's in `days`, for the Gemini pass below
    team_records = {}       # {team_name: (wins, losses, ties)} -- accumulated as we go
    raw_components_by_id = {}  # {game_id: (dk_spread, fd_spread)}

    for event in events:
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        outlet = broadcast_label(event)

        start_raw = event.get("date")
        try:
            start_dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        status = event.get("status", {}).get("type", {})
        is_tbd = bool(status.get("isTBDFlex")) or "TBD" in (event.get("shortName") or "")
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        day_key = local_dt.date().isoformat()
        slot = time_slot_for(local_dt, is_tbd)

        home_team = home["team"]["displayName"]
        away_team = away["team"]["displayName"]

        home_wins, home_losses, home_ties, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_ties, away_record = _parse_espn_record(away)
        if home_wins is not None:
            team_records[home_team] = (home_wins, home_losses, home_ties)
        if away_wins is not None:
            team_records[away_team] = (away_wins, away_losses, away_ties)

        odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
        if previous_odds_by_id:
            odds = carry_forward_odds(odds, previous_odds_by_id.get(event.get("id")))
        dk_spread = _dk_home_spread(odds)
        fd_spread = _fd_home_spread(odds)

        game_id = event.get("id")
        raw_components_by_id[game_id] = (dk_spread, fd_spread)

        game_entry = {
            "id": game_id,
            "start_time": start_raw,
            "start_time_tbd": is_tbd,
            "home_team": home_team,
            "home_abbr": home["team"].get("abbreviation"),
            "home_record": home_record,
            "away_team": away_team,
            "away_abbr": away["team"].get("abbreviation"),
            "away_record": away_record,
            "matchup_score": None,  # filled in below, once every team's win rank is known
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        days.setdefault(day_key, {}).setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    # Rank every team on the board by this week's win percentage, like a
    # poll release (rank 1 = best record), then blend each game's spread
    # closeness with its combined win rank -- 50/50, both normalized 0-1
    # across this week's games so neither metric's raw scale dominates.
    win_rank_lookup = build_win_rank_lookup(team_records)
    spread_components, win_components = [], []
    for g in all_games:
        dk_spread, fd_spread = raw_components_by_id[g["id"]]
        s, w = matchup_score(dk_spread, fd_spread,
                              win_rank_lookup.get(g["home_team"]), win_rank_lookup.get(g["away_team"]))
        spread_components.append(s)
        win_components.append(w)
    spread_norm = normalize_minmax(spread_components)
    win_norm = normalize_minmax(win_components)
    for g, sn, wn in zip(all_games, spread_norm, win_norm):
        g["matchup_score"] = round(100 * (0.5 * sn + 0.5 * wn), 1)

    # Rank spans the WHOLE WEEK, across every day in it, not just
    # whichever time slot or day a game lands in.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="nfl", season=year, week=week, gemini_key=gemini_key)

    day_list = []
    for day_key in sorted(days.keys()):
        slots_for_day = days[day_key]
        time_slots = []
        for slot_name in TIME_SLOT_ORDER:
            if slot_name not in slots_for_day:
                continue
            games_sorted = sorted(slots_for_day[slot_name], key=lambda x: x["matchup_score"])
            best_score = games_sorted[0]["matchup_score"] if games_sorted else None

            # Slot Pick: prefer the Chiefs or Broncos game if either is
            # playing in this window; otherwise fall back to the game with
            # the best blended (50% spread + 50% win-rank) score --
            # games_sorted is already sorted that way, so index 0 is it.
            team_priority_pick = pick_slot_team_priority(games_sorted)
            pick_reason = None
            for g in games_sorted:
                is_pick = (g is team_priority_pick) if team_priority_pick else (g is games_sorted[0] if games_sorted else False)
                g["is_slot_pick"] = is_pick
                if is_pick:
                    pick_reason = "team_priority" if team_priority_pick else "blended_score"

            time_slots.append({
                "slot": slot_name,
                "best_matchup_score": best_score,
                "pick_reason": pick_reason,
                "games": games_sorted,
            })
        weekday_name = date.fromisoformat(day_key).strftime("%A")
        day_game_count = sum(len(ts["games"]) for ts in time_slots)
        day_list.append({
            "date": day_key,
            "weekday": weekday_name,
            "game_count": day_game_count,
            "time_slots": time_slots,
        })

    return {
        "week": week,
        "total_games": sum(d["game_count"] for d in day_list),
        "days": day_list,
    }


def build(year, week_start, season_type, sharp_key, gemini_key=None, num_weeks=2, previous_odds_by_id=None):
    """Build `num_weeks` consecutive weeks starting at week_start (default:
    this week + next week) and wrap them into the full output payload."""
    weeks = []
    for offset in range(num_weeks):
        weeks.append(build_week(year, week_start + offset, season_type, sharp_key, gemini_key, previous_odds_by_id=previous_odds_by_id))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "season_type": season_type,
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": weeks,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NFL betting dashboard JSON.")
    p.add_argument("--year", type=int, default=SEASON_YEAR_DEFAULT, help="Season year")
    p.add_argument("--week", type=int, required=False, help="Starting NFL week number (default: ESPN's current week); this week and the following week are both built")
    p.add_argument("--num-weeks", type=int, default=2, help="How many consecutive weeks to build starting at --week (default: 2)")
    p.add_argument("--season-type", type=int, default=None,
                    help="1=preseason, 2=regular season, 3=postseason (default: auto-detected from ESPN each run)")
    p.add_argument("--out", default=None, help="Output path (default: data/nfl_dashboard.json)")
    return p.parse_args()


def main():
    args = parse_args()

    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("GEMINI_KEY not set -- building without Gemini predictions.")

    week = args.week
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "nfl_dashboard.json")
    out_path = os.path.abspath(out_path)

    existing_data = load_existing_dashboard(out_path)

    season_type = args.season_type
    if week is None:
        week, season_type = resolve_current(existing_data, forced_season_type=args.season_type)
    elif season_type is None:
        # An explicit --week with no --season-type: still need SOME season
        # type to build with -- ask ESPN what's current rather than
        # guessing, same as the no-args path does.
        _, season_type = get_espn_current_state()

    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    output = build(args.year, week, season_type, sharp_key, gemini_key,
                    num_weeks=args.num_weeks, previous_odds_by_id=previous_odds_by_id)
    fresh_week_nums = [w["week"] for w in output["weeks"]]

    # Record which week THIS build resolved as "current" -- College/NFL use
    # this (plus current_week + 1) to decide what to display, instead of
    # re-deriving "current" client-side from individual game timestamps.
    output["current_week"] = week

    # Never drop old weeks -- merge today's freshly-built weeks on top of
    # whatever weeks were already on disk instead of replacing the file
    # wholesale, so lines/scores/predictions from every past week stay
    # available (Picks shows all of them; NFL only shows current_week and
    # current_week + 1).
    output["weeks"] = merge_weeks(existing_data, output["weeks"])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    week_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across {len(week_nums)} week(s) total ({week_nums}) to {out_path}; "
        f"freshly built this run: {fresh_week_nums} (season_type={season_type})")
    if season_type == 1 and max(fresh_week_nums) > 3:
        log("Note: preseason only runs ~3 weeks -- a week number past that rolls into regular season "
            "with a different --season-type, so the 2nd week here may come back empty.")


if __name__ == "__main__":
    main()
