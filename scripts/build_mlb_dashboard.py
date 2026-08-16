#!/usr/bin/env python3
"""
MLB Betting Dashboard builder.

Pulls the day's schedule + broadcast + probable starting pitchers from
ESPN's public (unofficial, no-key-required) baseball scoreboard API, then
attaches DraftKings / FanDuel run-line (spread) + moneyline odds from
SharpAPI (via common.py). Exports everything to data/mlb_dashboard.json
for the static mlb.html front-end.

MLB plays every day (no bye weeks, no single "week N" concept), so unlike
build_dashboard.py (CFB) / build_nfl_dashboard.py (NFL) this script moves
one day at a time instead of one week at a time. To reuse the exact same
JSON shape and front-end/merge code those two already have (weeks -> days
-> time_slots -> games), each "week" entry in mlb_dashboard.json actually
holds exactly one calendar day, and the "week" number is that day's date
as an integer (YYYYMMDD, e.g. 20260815) rather than a real week number.
common.py's merge_weeks() and picks-store.js's filter/record helpers all
key off that same "week" field, so they work unchanged -- they just end
up merging/filtering by day instead of by week for MLB. mlb.html and
picks.html know to format that "week" number back into a date rather
than a "Week N" label (see formatMlbDayLabel() in picks-store.js).

The run line (MLB's version of a point spread) is almost always fixed at
+/-1.5 runs -- SharpAPI's own posted spread line is used as-is rather
than hardcoded, since alternate run lines do occasionally appear, but
+/-1.5 is what you should expect to see on nearly every game.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/build_mlb_dashboard.py
    python scripts/build_mlb_dashboard.py --start-date 2026-08-15 --num-days 2
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

ESPN_MLB_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
REQUEST_TIMEOUT = 20

NUM_DAYS_DEFAULT = 2  # "today" + "tomorrow" -- baseball plays daily, so this
                       # is MLB's equivalent of NFL/CFB showing 2 weeks.

# A team with no games played yet gets this win-rank value -- one worse
# than the worst possible real rank (30 teams in MLB) -- so it never
# outranks a team that actually has a record, same convention as NFL's
# UNRANKED_WIN_RANK / CFB's unranked-AP-poll rank.
UNRANKED_WIN_RANK = 31


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(date_str):
    resp = requests.get(
        ESPN_MLB_SCOREBOARD_URL,
        params={"dates": date_str, "limit": 200},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


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

    return "/".join(names) if names else "TBD"


def _parse_espn_record(competitor):
    """Extract (wins, losses, summary) from an ESPN scoreboard competitor's
    `records` list, or (None, None, None) if no overall record is present
    yet (e.g. before Opening Day)."""
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


def _probable_pitchers(comp):
    """Return {team_id: "K. Bradish (7-11, 3.69)"} from a competition's
    `probables` list, or {} if no probable pitchers are posted yet (common
    more than a day or two out)."""
    pitchers = {}
    for p in comp.get("probables", []):
        athlete = p.get("athlete") or {}
        team_id = (athlete.get("team") or {}).get("id")
        name = athlete.get("shortName") or athlete.get("fullName")
        if not team_id or not name:
            continue
        record = p.get("record")  # e.g. "(7-11, 3.69)"
        pitchers[team_id] = f"{name} {record}" if record else name
    return pitchers


# ---------------------------------------------------------------------------
# Odds helpers (same shape as NFL's)
# ---------------------------------------------------------------------------

def _dk_home_spread(odds):
    return odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")


def _fd_home_spread(odds):
    return odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")


# ---------------------------------------------------------------------------
# Matchup ranking -- combined win count only (no spread blending for MLB;
# the run line is nearly always a fixed +/-1.5, so it doesn't discriminate
# between games the way a point spread does for football).
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


# ---------------------------------------------------------------------------
# Main build -- one calendar day at a time
# ---------------------------------------------------------------------------

def build_day(day, sharp_key, gemini_key=None, previous_odds_by_id=None):
    """Build a single day's worth of games. Returns a "week"-shaped dict
    (week=YYYYMMDD int, days=[<this single day>]) so it slots into the
    same merge_weeks()/front-end code the NFL/CFB dashboards use."""
    date_str = day.strftime("%Y%m%d")
    log(f"Fetching MLB schedule for {date_str}...")
    scoreboard = get_scoreboard(date_str)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching DraftKings/FanDuel MLB odds from SharpAPI...")
    odds_rows = fetch_all_odds(sharp_key, league="mlb")
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    slots = {}  # slot_name -> list of game entries
    all_games = []
    team_records = {}  # {team_name: (wins, losses)}

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
        home_id = home["team"].get("id")
        away_id = away["team"].get("id")

        home_wins, home_losses, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_record = _parse_espn_record(away)
        if home_wins is not None:
            team_records[home_team] = (home_wins, home_losses)
        if away_wins is not None:
            team_records[away_team] = (away_wins, away_losses)

        pitchers = _probable_pitchers(comp)
        home_pitcher = pitchers.get(home_id)
        away_pitcher = pitchers.get(away_id)

        odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
        if previous_odds_by_id:
            odds = carry_forward_odds(odds, previous_odds_by_id.get(event.get("id")))

        game_id = event.get("id")

        game_entry = {
            "id": game_id,
            "start_time": start_raw,
            "start_time_tbd": is_tbd,
            "home_team": home_team,
            "home_abbr": home["team"].get("abbreviation"),
            "home_record": home_record,
            "home_pitcher": home_pitcher,
            "away_team": away_team,
            "away_abbr": away["team"].get("abbreviation"),
            "away_record": away_record,
            "away_pitcher": away_pitcher,
            "matchup_score": None,  # filled in below, once every team's win rank is known
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        slots.setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    # Rank every team on the board by today's win percentage (rank 1 = best
    # record), then use each game's combined win rank (lower = better/more
    # marquee matchup) as the matchup score -- no spread blending, since the
    # MLB run line is nearly always a fixed +/-1.5 and doesn't discriminate
    # between games the way a football point spread does.
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

    attach_gemini_predictions(all_games, sport="mlb", season=day.year,
                               week=day.isoformat(), gemini_key=gemini_key)

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
            "pick_reason": "combined_wins" if games_sorted else None,
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


def build(sharp_key, gemini_key=None, start_date=None, num_days=NUM_DAYS_DEFAULT, previous_odds_by_id=None):
    """Build `num_days` consecutive calendar days starting at `start_date`
    (default: today, in DISPLAY_TIMEZONE) and wrap them into the full
    output payload, "week"-shaped the same way NFL/CFB are."""
    if start_date is None:
        start_date = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    days_out = []
    for offset in range(num_days):
        d = start_date + timedelta(days=offset)
        days_out.append(build_day(d, sharp_key, gemini_key, previous_odds_by_id=previous_odds_by_id))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": start_date.year,
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": days_out,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the MLB betting dashboard JSON.")
    p.add_argument("--start-date", default=None, help="First date to build, YYYY-MM-DD (default: today)")
    p.add_argument("--num-days", type=int, default=NUM_DAYS_DEFAULT,
                    help=f"How many consecutive days to build starting at --start-date (default: {NUM_DAYS_DEFAULT})")
    p.add_argument("--out", default=None, help="Output path (default: data/mlb_dashboard.json)")
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
    out_path = args.out or os.path.join(script_dir, "..", "data", "mlb_dashboard.json")
    out_path = os.path.abspath(out_path)

    existing_data = load_existing_dashboard(out_path)
    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    output = build(sharp_key, gemini_key, start_date=start_date, num_days=args.num_days,
                    previous_odds_by_id=previous_odds_by_id)
    fresh_day_nums = [w["week"] for w in output["weeks"]]

    # Record which day THIS build resolved as "today" -- mlb.html uses this
    # (plus the very next day present in the file) to decide what to
    # display, instead of re-deriving "today" client-side.
    output["current_week"] = fresh_day_nums[0]

    # Never drop old days -- merge today's freshly-built days on top of
    # whatever days were already on disk instead of replacing the file
    # wholesale, so odds/scores/predictions from every past day stay
    # available (Picks shows all of them; mlb.html only shows today + tomorrow).
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
