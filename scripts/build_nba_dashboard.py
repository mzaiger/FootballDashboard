#!/usr/bin/env python3
"""
NBA Betting Dashboard builder.

Pulls the day's schedule + broadcast + team records from ESPN's public
(unofficial, no-key-required) basketball scoreboard API, then attaches
DraftKings / FanDuel spread + moneyline odds from SharpAPI (via
common.py). Exports everything to data/nba_dashboard.json for the static
nba.html front-end.

Like MLB, the NBA plays most days of the week (no real "week N" concept
during the regular season), so this script -- like
build_mlb_dashboard.py -- moves one calendar day at a time instead of one
week at a time. To reuse the exact same JSON shape and front-end/merge
code CFB/NFL/MLB already share (weeks -> days -> time_slots -> games),
each "week" entry in nba_dashboard.json actually holds exactly one
calendar day, and the "week" number is that day's date as an integer
(YYYYMMDD) rather than a real week number. See build_mlb_dashboard.py's
own module docstring for the full rationale -- this script follows it
exactly.

Unlike MLB's run line (nearly always a fixed +/-1.5), an NBA point spread
varies meaningfully game to game, so the matchup score blends 50% posted
spread + 50% combined win-rank (teams ranked by this season's record),
the same blend build_nfl_dashboard.py uses for its own (non Chiefs/Broncos)
games.

Off-season handling: if today falls BEFORE the first date on ESPN's
calendar (leagues[].calendar), the build window snaps forward to that
first game date -- so as soon as ESPN publishes the schedule, the board
centers on opening night instead of sitting blank until the day before
tip-off. See resolve_effective_today() below.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/build_nba_dashboard.py
    python scripts/build_nba_dashboard.py --start-date 2026-11-15 --num-days 2
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

ESPN_NBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
REQUEST_TIMEOUT = 20

NUM_DAYS_DEFAULT = 3  # "yesterday" + "today" + "tomorrow", same window MLB uses.

# A team with no games played yet gets this win-rank value -- one worse
# than the worst possible real rank (30 teams in the NBA) -- so it never
# outranks a team that actually has a record, same convention as
# NFL/MLB/NCAAF's own "unranked" win-rank values.
UNRANKED_WIN_RANK = 31


def current_season_year():
    """Auto-derive the NBA season's starting year from today's date, so
    this never needs a manual update when a new season starts. An NBA
    season is named for the year it tips off in (October); games played
    January-September belong to the season that started the PREVIOUS
    calendar year (regular season stretches into April, playoffs into
    June, and there's nothing else on the schedule the rest of the
    summer). Games from October onward belong to the season starting
    that same year."""
    today = datetime.now(timezone.utc).date()
    return today.year if today.month >= 10 else today.year - 1


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(date_str):
    """Fetch one day's scoreboard. IMPORTANT: ESPN's scoreboard endpoint
    does NOT return an empty event list for a date with no games -- it
    silently snaps forward to the next date that actually has something
    scheduled (verified directly: requesting a date weeks before the
    regular season tips off still returns that later date's game, with
    the response's own "day"/"date" field showing the date ESPN actually
    used, not the one requested). Guard against that here: if the
    response's own date doesn't match what was asked for, treat it as
    "no games that day" rather than silently mislabeling a future game as
    if it happened on the requested date.
    """
    resp = requests.get(
        ESPN_NBA_SCOREBOARD_URL,
        params={"dates": date_str, "limit": 100},
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


def calendar_game_dates(scoreboard):
    """Every scheduled game date in the scoreboard payload's
    leagues[].calendar array, as date objects. ESPN ships this as a flat
    list of ISO timestamps ("2026-10-21T07:00Z") -- one per day the league
    has anything scheduled -- so the FIRST entry is the season's first
    game date. Parse only the leading YYYY-MM-DD: the stamps are midnight
    ET, so the date component IS the game date (no timezone conversion
    wanted -- converting would shift late-night ET stamps back a day for
    Pacific viewers)."""
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
    blank from the final buzzer of last season until the day before
    opening night; with it, the board centers on the opener as soon as
    ESPN publishes the schedule. Falls back to the real date on any
    network/parse failure so a bad calendar never breaks a normal
    mid-season build."""
    try:
        scoreboard = get_scoreboard(default_today.strftime("%Y%m%d"))
    except requests.RequestException as exc:
        log(f"  NOTE: couldn't fetch ESPN calendar ({exc}) -- keeping {default_today} as 'today'.")
        return default_today

    upcoming = [d for d in calendar_game_dates(scoreboard) if d >= default_today]
    if not upcoming or min(upcoming) == default_today:
        return default_today  # season live or over-and-no-new-schedule yet

    first_game_day = min(upcoming)
    log(f"  Off-season detected: ESPN's calendar has nothing until {first_game_day} "
        f"-- treating {first_game_day} as 'today' for this build.")
    return first_game_day


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


# ---------------------------------------------------------------------------
# Matchup ranking -- 50% posted spread + 50% combined win rank, same blend
# pattern build_nfl_dashboard.py uses for its own default (non
# Chiefs/Broncos) games; unlike MLB's run line, an NBA spread moves
# meaningfully game to game so it's worth blending in here.
# ---------------------------------------------------------------------------

def build_win_rank_lookup(team_records):
    """Rank every team with a known record by win percentage (rank 1 =
    best record on the board). Ties broken by raw win total, then by team
    name for a stable, deterministic order. `team_records` is
    {team_name: (wins, losses)}. Returns {team_name: rank}."""
    entries = []
    for team, (wins, losses) in team_records.items():
        games_played = wins + losses
        pct = wins / games_played if games_played else -1
        entries.append((team, pct, wins, team))
    entries.sort(key=lambda e: (-e[1], -e[2], e[3]))
    return {team: i + 1 for i, (team, pct, wins, _) in enumerate(entries)}


def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


# ---------------------------------------------------------------------------
# Main build -- one calendar day at a time
# ---------------------------------------------------------------------------

def build_day(day, sharp_key, gemini_key=None, previous_odds_by_id=None,
              previous_entries_by_id=None, started_game_ids=None):
    """Build a single day's worth of games. Returns a "week"-shaped dict
    (week=YYYYMMDD int, days=[<this single day>]) so it slots into the
    same merge_weeks()/front-end code the other dashboards use."""
    date_str = day.strftime("%Y%m%d")
    log(f"Fetching NBA schedule for {date_str}...")
    scoreboard = get_scoreboard(date_str)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching DraftKings/FanDuel NBA odds from SharpAPI...")
    day_str = day.isoformat()
    odds_rows = fetch_all_odds(sharp_key, league="nba", markets=("spread", "moneyline"),
                                date_from=day_str, date_to=day_str)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    slots = {}  # slot_name -> list of game entries
    all_games = []
    team_records = {}  # {team_name: (wins, losses)}
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
        status = event.get("status", {}).get("type", {})
        is_tbd = "TBD" in (event.get("shortName") or "")
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        slot = time_slot_for(local_dt, is_tbd)

        home_team = home["team"]["displayName"]
        away_team = away["team"]["displayName"]

        home_wins, home_losses, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_record = _parse_espn_record(away)
        if home_wins is not None:
            team_records[home_team] = (home_wins, home_losses)
        if away_wins is not None:
            team_records[away_team] = (away_wins, away_losses)

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
            "matchup_score": None,  # filled in below, once every team's win rank is known
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        if already_started and previous_entry is not None and previous_entry.get("gemini_prediction"):
            game_entry["gemini_prediction"] = previous_entry["gemini_prediction"]
        slots.setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    # Blend 50% posted spread + 50% combined win rank -- both normalized
    # 0-100 across the whole day before blending, same pattern build_nfl_
    # dashboard.py's own default blend uses.
    win_rank_lookup = build_win_rank_lookup(team_records)
    spread_components, win_components = [], []
    for g in all_games:
        spread = _home_spread(g["odds"])
        spread_components.append(abs(spread) if spread is not None else None)

        home_rank = win_rank_lookup.get(g["home_team"])
        away_rank = win_rank_lookup.get(g["away_team"])
        win_components.append(
            (home_rank or UNRANKED_WIN_RANK) + (away_rank or UNRANKED_WIN_RANK)
            if (home_rank is not None or away_rank is not None) else None
        )
    spread_norm = normalize_minmax(spread_components)
    win_norm = normalize_minmax(win_components)
    for g, sn, wn in zip(all_games, spread_norm, win_norm):
        g["matchup_score"] = round(100 * (0.5 * sn + 0.5 * wn), 1)

    # Rank spans the WHOLE DAY, not just whichever time slot a game lands
    # in -- computed here, once per day, before games get split up into
    # time_slots below.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="nba", season=day.year,
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
            "pick_reason": "spread_and_wins" if games_sorted else None,
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
    output payload, "week"-shaped the same way CFB/NFL/MLB are."""
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
        "season": start_date.year if start_date.month >= 10 else start_date.year - 1,
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": days_out,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NBA betting dashboard JSON.")
    p.add_argument("--start-date", default=None, help="The 'today' date to center the build window on, YYYY-MM-DD (default: today)")
    p.add_argument("--num-days", type=int, default=NUM_DAYS_DEFAULT,
                    help=f"How many consecutive days to build, centered on --start-date (default: {NUM_DAYS_DEFAULT})")
    p.add_argument("--out", default=None, help="Output path (default: data/nba_dashboard.json)")
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
    out_path = args.out or os.path.join(script_dir, "..", "data", "nba_dashboard.json")
    out_path = os.path.abspath(out_path)
    scores_path = args.scores or os.path.join(script_dir, "..", "data", "scores.json")
    scores_path = os.path.abspath(scores_path)

    existing_data = load_existing_dashboard(out_path)
    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    previous_entries_by_id = load_previous_game_entries(out_path)
    started_game_ids = load_started_game_ids(scores_path, "nba")
    if started_game_ids:
        log(f"{len(started_game_ids)} NBA game(s) already have a score recorded in {scores_path} -- "
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
        f"freshly built this run: {fresh_day_nums}")


if __name__ == "__main__":
    main()
