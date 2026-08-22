#!/usr/bin/env python3
"""
NCAAMB (Division I men's college basketball) Betting Dashboard builder.

Pulls the day's schedule + broadcast + team records from ESPN's public
(unofficial, no-key-required) men's college basketball scoreboard API,
then attaches DraftKings / FanDuel spread + moneyline odds from SharpAPI
(via common.py). Exports everything to data/ncaamb_dashboard.json for the
static ncaamb.html front-end.

Like MLB/NBA, college basketball plays near-daily once the season starts
(no real "week N" concept on a day-granularity board), so this script --
like build_mlb_dashboard.py / build_nba_dashboard.py -- moves one calendar
day at a time instead of one week at a time. Each "week" entry in
ncaamb_dashboard.json holds exactly one calendar day, and the "week"
number is that day's date as an integer (YYYYMMDD). See
build_mlb_dashboard.py's module docstring for the full rationale.

Differences from the NBA builder:
  * Only "main channel" national-TV games make the board (same idea as
    build_ncaaf_dashboard.py; ESPN sometimes packs multiple networks into
    ONE string inside a broadcast object, so names are split on "/" and
    "," before being checked against MAIN_CHANNELS). The channel set is
    wider than CFB's -- see MAIN_CHANNELS below.
  * Games are ranked with college football's three-component blend --
    50% combined AP Top 25 rank + 25% combined win-rank + 25% posted
    spread -- instead of the NBA's two-component blend, since AP rank is
    the marquee signal in college hoops the same way it is in CFB.
  * Off-season guard: if today falls before the first date on ESPN's
    calendar (leagues[].calendar), the build window snaps forward to that
    first game date so the board shows opening night instead of three
    blank days. See resolve_effective_today() below.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard/rankings endpoints)

Usage:
    python scripts/build_ncaamb_dashboard.py
    python scripts/build_ncaamb_dashboard.py --start-date 2026-11-15 --num-days 2
"""

import argparse
import json
import os
import re
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

ESPN_NCAAMB_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
ESPN_NCAAMB_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/rankings"
REQUEST_TIMEOUT = 20

NUM_DAYS_DEFAULT = 3  # "yesterday" + "today" + "tomorrow", same window NBA/MLB use.

# groups=50 = Division I men's basketball only (verified against the live
# endpoint -- without it the scoreboard mixes in lower divisions), and
# limit=500 because a November Saturday can carry 100+ D1 games and the
# endpoint silently truncates to a subset without a high limit.
D1_GROUP = "50"
SCOREBOARD_LIMIT = 500

# SharpAPI's league id for Division I men's college basketball is "ncaam"
# -- NOT "ncaamb". "ncaamb" is this dashboard's own file/JSON name; the
# odds API rejects it with invalid_filter (its did_you_mean suggests
# "ncaam"). Keep this distinct from the sport keys used for scores.json /
# gemini_predictions below, which are local and separate.
SHARPAPI_LEAGUE = "ncaam"

# "Main channels" = national broadcast + flagship/national cable. This is
# WIDER than the CFB board's set: college hoops' national footprint also
# includes the CBS Sports Network package, NBC/USA's Big Ten national
# windows, and the CBS-Turner March Madness trio (TNT/TBS/truTV), so those
# count as "main" here too. Games on ESPNU, ACCN, SECN, BTN, ESPN+, and
# other streaming/regional outlets are still filtered out -- add any of
# those tokens to this set to widen the board further. Tokens must match
# ESPN's shortName spellings exactly ("CBSSN", not "CBS Sports Network").
MAIN_CHANNELS = {
    # National broadcast
    "ABC", "CBS", "NBC", "FOX",
    # Flagship cable
    "ESPN", "ESPN2", "FS1",
    # National cable -- college hoops additions vs. the CFB set
    "CBSSN",   # CBS Sports Network regular-season + tournament package
    "USA",     # NBC/USA's Big Ten national window
    # March Madness / major neutral-site package (CBS-Turner)
    "TNT", "TBS", "truTV",
}

# A team with no AP-poll ranking gets this value -- one worse than the
# worst possible AP Top 25 rank -- so it never outranks a team that's
# actually ranked. Normalized 0-100 before blending anyway, so the exact
# number only sets the "unranked" floor.
UNRANKED_VALUE = 26

# A team with no games played yet gets this win-rank value -- safely worse
# than any real rank among D1's ~360 teams -- so it never outranks a team
# that actually has a record, same convention as NFL/NBA/NCAAF's own
# "unranked" win-rank values.
UNRANKED_WIN_RANK = 365


def current_season_year():
    """Auto-derive the CBB season's starting year from today's date, so
    this never needs a manual update when a new season starts. A CBB
    season is named for the year it tips off in; the first games
    (exhibitions + opening night) land in late October/early November,
    and everything through the title game the following April belongs to
    that season. October onward -> the season starting this year;
    January-September -> the season that started the PREVIOUS year."""
    today = datetime.now(timezone.utc).date()
    return today.year if today.month >= 10 else today.year - 1


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(date_str):
    """Fetch one day's scoreboard. ESPN's scoreboard endpoint does NOT
    return an empty event list for a date with no games -- it silently
    snaps forward to the next date that actually has something scheduled.
    The NBA payload exposes that via a "day"."date" echo, but the college
    basketball payload carries NO such field (verified live), so the
    guard here only fires when ESPN does include one; the real safety net
    for this sport is build_day()'s local-date filter, which drops any
    event whose LOCAL date isn't the day being built."""
    resp = requests.get(
        ESPN_NCAAMB_SCOREBOARD_URL,
        params={"dates": date_str, "groups": D1_GROUP, "limit": SCOREBOARD_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    returned_date = (data.get("day") or {}).get("date")
    requested_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    if returned_date and returned_date != requested_date:
        log(f"  ESPN returned {returned_date}'s slate instead of the requested {requested_date} "
            f"(nothing scheduled that day) -- treating {requested_date} as having zero games.")
        data["events"] = []

    return data


def get_rankings(date_str):
    """Fetch the AP Top 25 release covering `date_str` (YYYYMMDD). The
    rankings endpoint accepts a dates= filter (verified live) and returns
    the release nearest that date -- during the off-season that's the
    final release of the last completed season, which is harmless: the
    off-season clamp centers builds on next season's opener, and the
    first AP poll of a new season drops shortly before opening night."""
    resp = requests.get(
        ESPN_NCAAMB_RANKINGS_URL,
        params={"dates": date_str},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def calendar_game_dates(scoreboard):
    """Every scheduled game date in the scoreboard payload's
    leagues[].calendar array, as date objects. ESPN ships this as a flat
    list of ISO timestamps ("2026-11-03T08:00Z") -- one per day the
    league has anything scheduled -- so the FIRST entry is the season's
    first game date. Parse only the leading YYYY-MM-DD: the date
    component IS the game date (no timezone conversion wanted --
    converting would shift the stamps back a day for viewers west of the
    stamps' own timezone)."""
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


def resolve_effective_today(default_today):
    """Off-season guard: if today falls BEFORE the first date on ESPN's
    calendar -- i.e. the entire schedule is still ahead of us -- snap the
    build window forward to that first game date instead of building three
    empty days around a dead calendar date. Without this, the board sits
    blank from the title game until the day before opening night; with
    it, the board centers on the opener as soon as ESPN publishes the
    schedule. Falls back to the real date on any network/parse failure so
    a bad calendar never breaks a normal mid-season build."""
    try:
        scoreboard = get_scoreboard(default_today.strftime("%Y%m%d"))
    except (requests.RequestException, ValueError) as exc:
        log(f"  NOTE: couldn't fetch ESPN calendar ({exc}) -- keeping {default_today} as 'today'.")
        return default_today

    upcoming = [d for d in calendar_game_dates(scoreboard) if d >= default_today]
    if not upcoming or min(upcoming) == default_today:
        return default_today  # season live, or over with no new schedule posted yet

    first_game_day = min(upcoming)
    log(f"  Off-season detected: ESPN's calendar has nothing until {first_game_day} "
        f"-- treating {first_game_day} (season's first game date) as 'today' for this build.")
    return first_game_day


def broadcast_names(event):
    """All national broadcast name strings for a game, RAW (not yet split
    or joined) -- across every ESPN payload shape that can carry them.
    Used both for display (broadcast_label, joined with ", ") and for the
    main-channel filter (on_main_channel, which further splits each
    string -- see there for why)."""
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

    return names


def broadcast_label(event):
    """Display string for a game's TV pill -- ", " instead of "/" between
    multiple networks (matches NFL/NBA/MLB's broadcast_label)."""
    names = broadcast_names(event)
    return ", ".join(names) if names else None


_CHANNEL_SPLIT_RE = re.compile(r"[/,]")


def _channel_tokens(names):
    """Split each raw broadcast name into individual channel tokens.

    ESPN sometimes packs more than one network into a SINGLE string
    inside one broadcast object (e.g. "ESPN2/ACCNX" as one entry in
    `names`) instead of giving each network its own list entry. A plain
    `outlet in MAIN_CHANNELS` check would silently miss those
    combined-string games, so every raw name here gets split on "/" and
    "," before being checked against MAIN_CHANNELS.
    """
    tokens = []
    for n in names:
        if not n:
            continue
        for part in _CHANNEL_SPLIT_RE.split(n):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return tokens


def on_main_channel(event):
    return any(tok in MAIN_CHANNELS for tok in _channel_tokens(broadcast_names(event)))


def _parse_espn_record(competitor):
    """Extract (wins, losses, summary) from an ESPN scoreboard competitor's
    `records` list, or (None, None, None) if no overall record is present
    yet (e.g. before opening night)."""
    for rec in competitor.get("records", []):
        if rec.get("type") == "total" or rec.get("name") == "overall":
            summary = rec.get("summary")
            if not summary:
                continue
            parts = summary.split("-")
            try:
                wins, losses = int(parts[0]), int(parts[1])
                return wins, losses, summary
            except (ValueError, IndexError):
                return None, None, summary
    return None, None, None


def build_rank_lookup(rankings_payload, poll_name="AP Top 25"):
    """{team_id: rank} from the first matching poll ("AP Top 25" by
    default). Falls back to any poll present if AP specifically isn't
    found (AP is occasionally slower to post than the Coaches poll early
    in the season)."""
    lookup = {}
    for release in rankings_payload.get("rankings", []):
        if release.get("name") == poll_name or release.get("shortName") == "AP Poll":
            for entry in release.get("ranks", []):
                team_id = entry.get("team", {}).get("id")
                if team_id is not None:
                    lookup[team_id] = entry.get("current")
            if lookup:
                return lookup
    for release in rankings_payload.get("rankings", []):
        for entry in release.get("ranks", []):
            team_id = entry.get("team", {}).get("id")
            if team_id is not None:
                lookup.setdefault(team_id, entry.get("current"))
        if lookup:
            return lookup
    return lookup


def get_rank_lookup(day):
    """{team_id: AP rank} for the poll nearest `day`. Returns {} (every
    team unranked, build still succeeds) on any network/parse failure so
    a rankings outage never kills a build."""
    try:
        return build_rank_lookup(get_rankings(day.strftime("%Y%m%d")))
    except (requests.RequestException, ValueError) as exc:
        log(f"  NOTE: couldn't fetch AP rankings for {day.isoformat()} ({exc}) -- all teams unranked.")
        return {}


# ---------------------------------------------------------------------------
# Matchup ranking -- 50% combined AP Top 25 rank + 25% combined win rank +
# 25% posted spread, same blend pattern build_ncaaf_dashboard.py uses.
# ---------------------------------------------------------------------------

def build_win_rank_lookup(team_records):
    """Rank every team with a known record by win percentage (rank 1 =
    best record on the board). Ties broken by raw win total, then by team
    id for a stable, deterministic order. `team_records` is
    {team_id: (wins, losses)}. Returns {team_id: rank}."""
    entries = []
    for team_id, (wins, losses) in team_records.items():
        games_played = wins + losses
        pct = wins / games_played if games_played else -1
        entries.append((team_id, pct, wins))
    entries.sort(key=lambda e: (-e[1], -e[2], e[0]))
    return {team_id: i + 1 for i, (team_id, pct, wins) in enumerate(entries)}


def matchup_components(home_rank, away_rank, home_win_rank, away_win_rank, odds):
    """Per-game (ap_component, win_component, spread_component) tuple for
    build_day to collect across the whole day and normalize/blend at
    once. Any component with no data for this particular game (e.g.
    neither team ranked, or no line posted yet) comes back None and
    normalize_minmax() treats it as a neutral mid-scale value rather than
    penalizing the game for missing data."""
    ap_component = None
    if home_rank is not None or away_rank is not None:
        ap_component = (home_rank or UNRANKED_VALUE) + (away_rank or UNRANKED_VALUE)

    win_component = None
    if home_win_rank is not None or away_win_rank is not None:
        win_component = (home_win_rank or UNRANKED_WIN_RANK) + (away_win_rank or UNRANKED_WIN_RANK)

    spread = _home_spread(odds)
    spread_component = abs(spread) if spread is not None else None

    return ap_component, win_component, spread_component


def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


# ---------------------------------------------------------------------------
# Main build -- one calendar day at a time
# ---------------------------------------------------------------------------

def build_day(day, sharp_key, gemini_key=None, previous_odds_by_id=None,
              previous_entries_by_id=None, started_game_ids=None, rank_lookup=None):
    """Build a single day's worth of games. Returns a "week"-shaped dict
    (week=YYYYMMDD int, days=[<this single day>]) so it slots into the
    same merge_weeks()/front-end code the other dashboards use."""
    date_str = day.strftime("%Y%m%d")
    log(f"Fetching NCAAMB schedule for {date_str}...")
    scoreboard = get_scoreboard(date_str)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching AP rankings...")
    if rank_lookup is None:
        rank_lookup = get_rank_lookup(day)
    log(f"  {len(rank_lookup)} ranked teams found")

    log("Fetching DraftKings/FanDuel NCAAMB odds from SharpAPI...")
    day_str = day.isoformat()
    odds_rows = fetch_all_odds(sharp_key, league=SHARPAPI_LEAGUE, markets=("spread", "moneyline"),
                                date_from=day_str, date_to=day_str)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    slots = {}  # slot_name -> list of game entries
    all_games = []
    skipped_no_tv = 0
    skipped_wrong_day = 0
    team_records = {}  # {team_id: (wins, losses)}
    started_game_ids = started_game_ids or set()
    previous_entries_by_id = previous_entries_by_id or {}
    frozen_prediction_skip_ids = set()  # passed to attach_gemini_predictions below
    build_day_key = day.isoformat()

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
        if not on_main_channel(event):
            skipped_no_tv += 1
            continue

        start_raw = event.get("date")
        try:
            start_dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        # The CBB payload has no `day.date` echo to detect ESPN's silent
        # snap-forward (see get_scoreboard) -- so drop any event whose
        # LOCAL date isn't the day being built. This is what keeps a
        # future slate from being mislabeled as this day's games.
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        if local_dt.date().isoformat() != build_day_key:
            skipped_wrong_day += 1
            continue
        is_tbd = "TBD" in (event.get("shortName") or "")
        slot = time_slot_for(local_dt, is_tbd)

        home_team = home["team"].get("displayName")
        away_team = away["team"].get("displayName")
        home_id = home["team"].get("id")
        away_id = away["team"].get("id")

        home_wins, home_losses, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_record = _parse_espn_record(away)
        if home_wins is not None and home_id is not None:
            team_records[home_id] = (home_wins, home_losses)
        if away_wins is not None and away_id is not None:
            team_records[away_id] = (away_wins, away_losses)

        home_rank = rank_lookup.get(home_id)
        away_rank = rank_lookup.get(away_id)

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
            "home_rank": home_rank,
            "home_record": home_record or "0-0",
            "away_team": away_team,
            "away_rank": away_rank,
            "away_record": away_record or "0-0",
            "matchup_score": None,  # filled in below, once every game's components are known
            "channel": outlet or "Not on Main TV",
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
            "_home_id": home_id,
            "_away_id": away_id,
        }
        if already_started and previous_entry is not None and previous_entry.get("gemini_prediction"):
            game_entry["gemini_prediction"] = previous_entry["gemini_prediction"]
        slots.setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    if skipped_no_tv:
        log(f"  {skipped_no_tv} game(s) skipped (not on a main channel)")
    if skipped_wrong_day:
        log(f"  {skipped_wrong_day} game(s) skipped (ESPN returned a different date's slate)")

    # Blend 50% AP rank + 25% win rank + 25% posted spread -- each
    # normalized 0-100 across the whole day before blending, same pattern
    # build_ncaaf_dashboard.py uses for its weekly blend.
    win_rank_lookup = build_win_rank_lookup(team_records)
    ap_components, win_components, spread_components = [], [], []
    for g in all_games:
        home_win_rank = win_rank_lookup.get(g["_home_id"])
        away_win_rank = win_rank_lookup.get(g["_away_id"])
        a, w, s = matchup_components(g["home_rank"], g["away_rank"], home_win_rank, away_win_rank, g["odds"])
        ap_components.append(a)
        win_components.append(w)
        spread_components.append(s)
    ap_norm = normalize_minmax(ap_components)
    win_norm = normalize_minmax(win_components)
    spread_norm = normalize_minmax(spread_components)
    for g, an, wn, sn in zip(all_games, ap_norm, win_norm, spread_norm):
        g["matchup_score"] = round(100 * (0.5 * an + 0.25 * wn + 0.25 * sn), 1)
        # Internal-only fields, not part of the public JSON shape.
        del g["_home_id"]
        del g["_away_id"]

    # Rank spans the WHOLE DAY, not just whichever time slot a game lands
    # in -- computed here, once per day, before games get split up into
    # time_slots below.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="ncaamb", season=day.year,
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
            "pick_reason": "ap_wins_spread" if games_sorted else None,
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

    # One rankings fetch shared across the whole build window -- the AP
    # poll updates weekly, so every day in a 3-day window uses the same
    # release (the one nearest the window's center date).
    rank_lookup = get_rank_lookup(start_date)

    days_out = []
    for offset in range(num_days):
        d = window_start + timedelta(days=offset)
        days_out.append(build_day(d, sharp_key, gemini_key, previous_odds_by_id=previous_odds_by_id,
                                   previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids,
                                   rank_lookup=rank_lookup))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": start_date.year if start_date.month >= 10 else start_date.year - 1,
        "main_channels": sorted(MAIN_CHANNELS),
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": days_out,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NCAAMB betting dashboard JSON.")
    p.add_argument("--start-date", default=None, help="The 'today' date to center the build window on, YYYY-MM-DD (default: today, or the season's first game date during the off-season)")
    p.add_argument("--num-days", type=int, default=NUM_DAYS_DEFAULT,
                    help=f"How many consecutive days to build, centered on --start-date (default: {NUM_DAYS_DEFAULT})")
    p.add_argument("--out", default=None, help="Output path (default: data/ncaamb_dashboard.json)")
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
    resolved_today = start_date or datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    # Off-season clamp (skipped when --start-date forces a date): if ESPN's
    # calendar says nothing happens until some future opening date, build
    # around THAT date so the board shows the opener instead of blank.
    if start_date is None:
        resolved_today = resolve_effective_today(resolved_today)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "ncaamb_dashboard.json")
    out_path = os.path.abspath(out_path)
    scores_path = args.scores or os.path.join(script_dir, "..", "data", "scores.json")
    scores_path = os.path.abspath(scores_path)

    existing_data = load_existing_dashboard(out_path)
    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    previous_entries_by_id = load_previous_game_entries(out_path)
    started_game_ids = load_started_game_ids(scores_path, "ncaamb")
    if started_game_ids:
        log(f"{len(started_game_ids)} NCAAMB game(s) already have a score recorded in {scores_path} -- "
            f"freezing odds and Gemini predictions for those instead of updating them.")

    output = build(sharp_key, gemini_key, start_date=resolved_today, num_days=args.num_days,
                    previous_odds_by_id=previous_odds_by_id,
                    previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids)
    fresh_day_nums = [w["week"] for w in output["weeks"]]

    output["current_week"] = int(resolved_today.strftime("%Y%m%d"))
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
