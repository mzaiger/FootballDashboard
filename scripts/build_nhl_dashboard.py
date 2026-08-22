#!/usr/bin/env python3
"""
NHL Betting Dashboard builder.

Pulls the day's schedule + broadcast + team records from ESPN's public
(unofficial, no-key-required) hockey scoreboard API, then attaches
DraftKings / FanDuel puck-line (spread) + moneyline odds from SharpAPI
(via common.py). Exports everything to data/nhl_dashboard.json for the
static nhl.html front-end.

Like MLB/NBA, the NHL plays most days of the week (no real "week N"
concept during the regular season), so this script -- like
build_mlb_dashboard.py / build_nba_dashboard.py -- moves one calendar day
at a time instead of one week at a time. To reuse the exact same JSON
shape and front-end/merge code CFB/NFL/MLB/NBA/NCAAMB already share
(weeks -> days -> time_slots -> games), each "week" entry in
nhl_dashboard.json actually holds exactly one calendar day, and the
"week" number is that day's date as an integer (YYYYMMDD) rather than a
real week number. See build_mlb_dashboard.py's own module docstring for
the full rationale -- this script follows it exactly.

The puck line (NHL's version of a point spread) is, like MLB's run line,
almost always fixed at +/-1.5 goals and doesn't discriminate between
games the way a football point spread does -- so, like build_mlb_
dashboard.py, the matchup score here is combined win rank ONLY, no
spread blending (unlike build_nba_dashboard.py, whose spread genuinely
varies game to game and IS blended in).

NHL records come back from ESPN as a three-part "W-L-OTL" summary (wins-
losses-overtime losses), not the two-part "W-L" NBA/MLB use -- an
overtime/shootout loss still earns a point in the standings, so ranking
teams by raw win percentage alone (ignoring OTL entirely) would
understate a team that's lost several close overtime games. This script
instead ranks by standings POINTS percentage (2 pts per win, 1 pt per
OTL, out of 2 pts possible per game), same as the NHL's own standings
formula -- see build_win_rank_lookup() below.

NOTE ON SHARPAPI'S NHL MARKET NAME: SharpAPI's spread-equivalent market
is called "run_line" for MLB and "point_spread" for football; this
script assumes NHL's is "puck_line" by analogy (added to common.py's
_SPREAD_MARKET_ALIASES), since there was no network access to SharpAPI
itself while writing this. If the spread column comes back empty on the
very first real run, check the raw market_type strings in a
fetch_all_odds(league="nhl") response and fix that mapping in common.py.

Off-season handling: if today falls BEFORE the first date on ESPN's
calendar (leagues[].calendar), the build window stays centered on real
"today" (so the Stanley Cup Final finale keeps showing) for
OFFSEASON_GRACE_DAYS after its last kickoff, then snaps forward to that
first game date -- so the board previews next season's opener instead of
sitting blank for months. See resolve_effective_today() below.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/build_nhl_dashboard.py
    python scripts/build_nhl_dashboard.py --start-date 2026-11-15 --num-days 2
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from common import (
    DISPLAY_TIMEZONE,
    TIME_SLOT_ORDER,
    assign_matchup_ranks,
    carry_forward_odds,
    fetch_all_odds,
    load_existing_dashboard,
    load_previous_game_entries,
    load_previous_odds_by_game,
    load_started_game_ids,
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

ESPN_NHL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
REQUEST_TIMEOUT = 20

NUM_DAYS_DEFAULT = 3  # "yesterday" + "today" + "tomorrow", same window MLB/NBA use.

# How many days AFTER the last stored game's kickoff (the Stanley Cup
# Final finale, in practice) to keep the board centered on real "today"
# before the off-season guard (resolve_effective_today, below) snaps it
# forward to next season's opening-night preview. Without this grace
# period the Final's finale would vanish from the board's yesterday/
# today/tomorrow window the very next day -- see resolve_effective_today()'s
# docstring for the full rationale.
OFFSEASON_GRACE_DAYS = 7

# A team with no games played yet gets this win-rank value -- one worse
# than the worst possible real rank (32 teams in the NHL) -- so it never
# outranks a team that actually has a record, same convention as
# NFL/MLB/NBA/NCAAF's own "unranked" win-rank values.
UNRANKED_WIN_RANK = 33


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(date_str):
    """Fetch one day's scoreboard. Matches build_mlb_dashboard.py's own
    get_scoreboard(): unverified live for NHL specifically (no network
    access to ESPN's hockey endpoint while writing this), but ESPN's
    scoreboard endpoints are one shared implementation across sports, and
    MLB's confirmed behavior is to return an honest empty "events": []
    for a date with no games rather than silently snapping forward to
    the next date that has one (unlike the NBA endpoint, which DOES snap
    forward -- see build_nba_dashboard.py's own get_scoreboard()). Worth
    a quick sanity check against a real off-season date on the first live
    run; if NHL's endpoint turns out to behave like the NBA one instead,
    port that function's "returned date doesn't match requested date"
    guard over here too."""
    resp = requests.get(
        ESPN_NHL_SCOREBOARD_URL,
        params={"dates": date_str, "limit": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def calendar_game_dates(scoreboard):
    """Every date listed in the scoreboard payload's leagues[].calendar
    array, as date objects. Parses only the leading YYYY-MM-DD: the
    stamps are midnight ET, so the date component IS the game date (no
    timezone conversion wanted -- converting would shift late-night ET
    stamps back a day for Pacific viewers)."""
    dates = []
    for league in scoreboard.get("leagues") or []:
        for entry in league.get("calendar") or []:
            if isinstance(entry, str) and len(entry) >= 10:
                try:
                    dates.append(date.fromisoformat(entry[:10]))
                except ValueError:
                    continue
        if dates:
            break
    return sorted(set(dates))


def get_scoreboard_undated():
    """Fetch the default (undated) scoreboard. CRITICAL difference vs.
    get_scoreboard(): a DATED request for a dead off-season date makes
    ESPN anchor the whole response to the previously COMPLETED season --
    its leagues[].calendar then lists only PAST dates. The UNDATED
    request instead snaps forward to the next game day and ships the
    UPCOMING season's calendar -- which is the only place the next
    season's first date exists. The off-season resolver therefore reads
    THIS payload, never the dated one."""
    resp = requests.get(
        ESPN_NHL_SCOREBOARD_URL,
        params={"limit": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def latest_kickoff_overall(existing_data):
    """The single latest game start time (UTC) among EVERY game currently
    stored, across every day -- used to anchor the off-season grace
    period on the actual last game played (the Stanley Cup Final
    finale), not on whatever day happens to be labeled highest."""
    latest = None
    for w in (existing_data or {}).get("weeks", []):
        for day in w.get("days", []):
            for slot in day.get("time_slots", []):
                for g in slot.get("games", []):
                    raw = g.get("start_time")
                    if not raw:
                        continue
                    try:
                        kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if latest is None or kickoff > latest:
                        latest = kickoff
    return latest


def resolve_effective_today(default_today, existing_data=None):
    """Off-season guard WITH a grace period: once the season is truly
    over, keep the build window centered on the real "today" -- so the
    Stanley Cup Final finale keeps showing in the yesterday/today/
    tomorrow window -- for OFFSEASON_GRACE_DAYS after its last kickoff.
    Only once that grace period elapses does "today" get snapped forward
    to next season's opening night, so the board previews the new season
    instead of drifting through months of empty days. Fetches the
    UNDATED scoreboard for the calendar (see get_scoreboard_undated -- a
    dated request anchors to the completed season and makes every
    upcoming-date test silently fail). Every branch logs loudly so a
    silent fallback can never hide a problem again."""
    last_kickoff = latest_kickoff_overall(existing_data)
    if last_kickoff is not None:
        grace_until = last_kickoff + timedelta(days=OFFSEASON_GRACE_DAYS)
        now_utc = datetime.now(timezone.utc)
        if now_utc < grace_until:
            log(f"  Season-finale grace period active (last kickoff {last_kickoff.isoformat()}, "
                f"holding until {grace_until.isoformat()}) -- keeping {default_today} as 'today'.")
            return default_today

    try:
        scoreboard = get_scoreboard_undated()
    except (requests.RequestException, ValueError) as exc:
        log(f"  NOTE: couldn't fetch ESPN calendar ({exc}) -- keeping {default_today} as 'today'.")
        return default_today

    all_dates = calendar_game_dates(scoreboard)
    if not all_dates:
        log(f"  NOTE: ESPN's scoreboard carries no league calendar -- keeping {default_today} as 'today'.")
        return default_today

    first_day = all_dates[0]  # sorted ascending -- upcoming season's first date
    if default_today >= first_day:
        log(f"  Season underway (calendar starts {first_day}, on/before today) -- keeping {default_today} as 'today'.")
        return default_today

    log(f"  Off-season detected (grace period elapsed): today is before the season's first "
        f"calendar date ({first_day}) -- previewing {first_day} as 'today' for this build.")
    return first_day


def broadcast_label(event):
    """Join all national broadcast names for a game across all ESPN payload structures."""
    names = []

    for b in event.get("broadcasts", []):
        for n in b.get("names", []):
            if n and n not in names:
                names.append(n)

    for comp in event.get("competitions", []):
        for b in comp.get("broadcasts", []):
            for n in b.get("names", []):
                if n and n not in names:
                    names.append(n)
            media_name = b.get("media", {}).get("shortName")
            if media_name and media_name not in names:
                names.append(media_name)

    for gb in event.get("geoBroadcasts", []):
        short = gb.get("media", {}).get("shortName")
        if short and short not in names:
            names.append(short)

    return ", ".join(names) if names else "TBD"


def _parse_espn_record(competitor):
    """Extract (wins, losses, otl, summary) from an ESPN scoreboard
    competitor's `records` list, or (None, None, None, None) if no
    overall record is present yet (e.g. before opening night).

    NHL's summary is three-part ("21-14-5", wins-losses-OTL) once a team
    has at least one overtime/shootout loss on the books, but early in
    the season (or for a team that hasn't lost in OT/SO yet) it can come
    back as plain two-part "W-L" -- handle both rather than assuming a
    fixed part count."""
    for rec in competitor.get("records", []):
        if rec.get("type") == "total" or rec.get("name") == "overall":
            summary = rec.get("summary")
            if not summary:
                continue
            parts = summary.split("-")
            try:
                wins, losses = int(parts[0]), int(parts[1])
                otl = int(parts[2]) if len(parts) > 2 else 0
                return wins, losses, otl, summary
            except (ValueError, IndexError):
                return None, None, None, summary
    return None, None, None, None


# ---------------------------------------------------------------------------
# Matchup ranking -- standings-points rank only (no spread blending; like
# MLB's run line, the NHL puck line is nearly always a fixed +/-1.5 and
# doesn't discriminate between games the way a football point spread does).
# ---------------------------------------------------------------------------

def build_win_rank_lookup(team_records):
    """Rank every team with a known record by NHL standings points
    percentage (2 pts per win, 1 pt per OTL, out of 2 possible per game
    played) -- rank 1 = best points percentage on the board. This is the
    NHL's own standings formula, and matters here specifically because a
    team with several close overtime losses banks real points for them;
    ranking by raw win percentage alone (ignoring OTL) would understate
    that team relative to one with the same win total but more regulation
    losses. Ties broken by raw point total, then by team name for a
    stable, deterministic order. `team_records` is {team_name: (wins,
    losses, otl)}. Returns {team_name: rank}."""
    entries = []
    for team, (wins, losses, otl) in team_records.items():
        games_played = wins + losses + otl
        points = 2 * wins + otl
        pct = points / (2 * games_played) if games_played else -1
        entries.append((team, pct, points, team))
    entries.sort(key=lambda e: (-e[1], -e[2], e[3]))
    return {team: i + 1 for i, (team, pct, points, _) in enumerate(entries)}


# ---------------------------------------------------------------------------
# Main build -- one calendar day at a time
# ---------------------------------------------------------------------------

def build_day(day, sharp_key, gemini_key=None, previous_odds_by_id=None,
              previous_entries_by_id=None, started_game_ids=None):
    """Build a single day's worth of games. Returns a "week"-shaped dict
    (week=YYYYMMDD int, days=[<this single day>]) so it slots into the
    same merge_weeks()/front-end code the other dashboards use."""
    date_str = day.strftime("%Y%m%d")
    log(f"Fetching NHL schedule for {date_str}...")
    scoreboard = get_scoreboard(date_str)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching DraftKings/FanDuel NHL odds from SharpAPI...")
    # SharpAPI's spread-equivalent market for hockey is assumed to be
    # called "puck_line" (see this module's docstring for why that's
    # unconfirmed) -- see common.py's _SPREAD_MARKET_ALIASES, which maps
    # it back to our internal "spread" bucket once the rows come back.
    day_str = day.isoformat()
    odds_rows = fetch_all_odds(sharp_key, league="nhl", markets=("puck_line", "moneyline"),
                                date_from=day_str, date_to=day_str)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    slots = {}  # slot_name -> list of game entries
    all_games = []
    team_records = {}  # {team_name: (wins, losses, otl)}
    started_game_ids = started_game_ids or set()
    previous_entries_by_id = previous_entries_by_id or {}
    frozen_prediction_skip_ids = set()  # passed to attach_gemini_predictions below

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
        is_tbd = "TBD" in (event.get("shortName") or "")
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        slot = time_slot_for(local_dt, is_tbd)

        home_team = home["team"]["displayName"]
        away_team = away["team"]["displayName"]

        home_wins, home_losses, home_otl, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_otl, away_record = _parse_espn_record(away)
        if home_wins is not None:
            team_records[home_team] = (home_wins, home_losses, home_otl)
        if away_wins is not None:
            team_records[away_team] = (away_wins, away_losses, away_otl)

        game_id = event.get("id")
        gid_str = str(game_id)
        already_started = gid_str in started_game_ids
        previous_entry = previous_entries_by_id.get(gid_str)

        # Once a game has ANY score recorded in scores.json (live or
        # final -- see load_started_game_ids), freeze its odds and Gemini
        # prediction at exactly whatever was last saved instead of
        # re-fetching/re-matching/re-calling: an in-game line moves
        # constantly and doesn't reflect the pregame market the pick/
        # prediction was actually made against.
        if already_started and previous_entry is not None:
            odds = previous_entry.get("odds") or {}
            frozen_prediction_skip_ids.add(gid_str)
        else:
            odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
            if previous_odds_by_id:
                odds = carry_forward_odds(odds, previous_odds_by_id.get(event.get("id")))

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
            "matchup_score": None,  # filled in below, once every team's points rank is known
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        if already_started and previous_entry is not None and previous_entry.get("gemini_prediction"):
            game_entry["gemini_prediction"] = previous_entry["gemini_prediction"]
        slots.setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    # Rank every team on the board by today's standings points percentage
    # (rank 1 = best), then use each game's combined points rank (lower =
    # better/more marquee matchup) as the matchup score -- no spread
    # blending, since the NHL puck line is nearly always a fixed +/-1.5
    # and doesn't discriminate between games the way a football point
    # spread does.
    win_rank_lookup = build_win_rank_lookup(team_records)
    win_components = []
    for g in all_games:
        home_rank = win_rank_lookup.get(g["home_team"])
        away_rank = win_rank_lookup.get(g["away_team"])
        win_components.append(
            (home_rank or UNRANKED_WIN_RANK) + (away_rank or UNRANKED_WIN_RANK)
            if (home_rank is not None or away_rank is not None) else None
        )
    win_norm = normalize_minmax(win_components)
    for g, wn in zip(all_games, win_norm):
        g["matchup_score"] = round(100 * wn, 1)

    # Rank spans the WHOLE DAY, not just whichever time slot a game lands
    # in -- computed here, once per day, before games get split up into
    # time_slots below.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="nhl", season=day.year,
                               week=day.isoformat(), gemini_key=gemini_key,
                               skip_ids=frozen_prediction_skip_ids)

    time_slots = []
    for slot_name in TIME_SLOT_ORDER:
        if slot_name not in slots:
            continue
        games_sorted = sorted(slots[slot_name], key=lambda x: x["matchup_score"])
        best_score = games_sorted[0]["matchup_score"] if games_sorted else None
        for i, g in enumerate(games_sorted):
            g["is_slot_pick"] = (i == 0)
        time_slots.append({
            "slot": slot_name,
            "best_matchup_score": best_score,
            "pick_reason": "combined_points" if games_sorted else None,
            "games": games_sorted,
        })

    day_dict = {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "game_count": len(all_games),
        "time_slots": time_slots,
    }

    week_num = int(day.strftime("%Y%m%d"))
    return {
        "week": week_num,
        "total_games": len(all_games),
        "days": [day_dict],
    }


def build(sharp_key, gemini_key=None, start_date=None, num_days=NUM_DAYS_DEFAULT, previous_odds_by_id=None,
          previous_entries_by_id=None, started_game_ids=None):
    """Build `num_days` consecutive calendar days centered on `start_date`
    (default: today, in DISPLAY_TIMEZONE) and wrap them into the full
    output payload, "week"-shaped the same way CFB/NFL/MLB/NBA are."""
    if start_date is None:
        start_date = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    window_start = start_date - timedelta(days=(num_days - 1) // 2)

    days_out = []
    for offset in range(num_days):
        d = window_start + timedelta(days=offset)
        days_out.append(build_day(d, sharp_key, gemini_key, previous_odds_by_id=previous_odds_by_id,
                                   previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": start_date.year if start_date.month >= 9 else start_date.year - 1,
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": days_out,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NHL betting dashboard JSON.")
    p.add_argument("--start-date", default=None, help="The 'today' date to center the build window on, YYYY-MM-DD (default: today, or the season's first game date during the off-season)")
    p.add_argument("--num-days", type=int, default=NUM_DAYS_DEFAULT,
                    help=f"How many consecutive days to build, centered on --start-date (default: {NUM_DAYS_DEFAULT})")
    p.add_argument("--out", default=None, help="Output path (default: data/nhl_dashboard.json)")
    p.add_argument("--scores", default=None, help="Path to scores.json, used to freeze odds/Gemini predictions for started games (default: data/scores.json)")
    return p.parse_args()


def main():
    args = parse_args()

    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("GEMINI_KEY not set -- building without Gemini predictions.")

    start_date = date.fromisoformat(args.start_date) if args.start_date else None

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "nhl_dashboard.json")
    out_path = os.path.abspath(out_path)
    scores_path = args.scores or os.path.join(script_dir, "..", "data", "scores.json")
    scores_path = os.path.abspath(scores_path)

    existing_data = load_existing_dashboard(out_path)

    # Resolve "today" here (not inside build()) so we can record it as
    # current_week below -- with num_days=3 the build window is centered
    # on this date (yesterday/today/tomorrow), so the FIRST day in
    # output["weeks"] is now yesterday, not today; using that directly
    # would record current_week as yesterday's date, which would break
    # nhl.html's "which day is current" resolution once the window
    # stopped starting exactly at today (same lesson build_mlb_dashboard.py
    # already learned the hard way -- see its own main() comment).
    resolved_today = start_date or datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    # Off-season clamp (skipped when --start-date forces a date): only when
    # today is before the season's FIRST calendar date (and the grace
    # period past the Stanley Cup Final finale has elapsed) -- never
    # mid-season.
    if start_date is None:
        resolved_today = resolve_effective_today(resolved_today, existing_data)

    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    previous_entries_by_id = load_previous_game_entries(out_path)
    started_game_ids = load_started_game_ids(scores_path, "nhl")
    if started_game_ids:
        log(f"{len(started_game_ids)} NHL game(s) already have a score recorded in {scores_path} -- "
            f"freezing odds and Gemini predictions for those instead of updating them.")

    output = build(sharp_key, gemini_key, start_date=resolved_today, num_days=args.num_days,
                    previous_odds_by_id=previous_odds_by_id,
                    previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids)
    fresh_day_nums = [w["week"] for w in output["weeks"]]

    # Record which day is "today" -- nhl.html uses this (plus the day
    # immediately before and after it in the file) to decide what to
    # display, instead of re-deriving "today" client-side.
    output["current_week"] = int(resolved_today.strftime("%Y%m%d"))

    # Never drop old days -- merge today's freshly-built days on top of
    # whatever days were already on disk instead of replacing the file
    # wholesale, so odds/scores/predictions from every past day stay
    # available (Picks shows all of them; nhl.html only shows yesterday
    # through tomorrow).
    output["weeks"] = merge_weeks(existing_data, output["weeks"])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    day_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across {len(day_nums)} day(s) total ({day_nums}) to {out_path}; "
        f"freshly built this run: {fresh_day_nums} (current_day={output['current_week']})")


if __name__ == "__main__":
    main()
