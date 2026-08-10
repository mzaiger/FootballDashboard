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
    carry_forward_odds,
    fetch_all_odds,
    load_previous_odds_by_game,
    log,
    match_odds_for_game,
    time_slot_for,
)
from gemini_predictions import attach_gemini_predictions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REQUEST_TIMEOUT = 20

# seasontype: 1=preseason, 2=regular season, 3=postseason
SEASON_TYPE_DEFAULT = 1  # Updated to default to preseason
SEASON_YEAR_DEFAULT = 2026

# Regional-pick heuristic priority for the Omaha/Lincoln, NE market (no home
# NFL team). Checked in order; first match in a multi-game window wins.
REGIONAL_TEAM_PRIORITY = ["Kansas City Chiefs", "Denver Broncos"]


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

    return "/".join(names) if names else "TBD"

# ---------------------------------------------------------------------------
# Matchup ranking
# ---------------------------------------------------------------------------

def matchup_score(dk_spread, fd_spread):
    """
    Lower score = closer / more competitive game = better matchup for a
    betting dashboard. Unlike CFB (which has AP rankings to lean on), the
    NFL doesn't have a clean, universally-agreed "how good is this team"
    number this early in a season, so this uses the market's own judgment
    instead: the smaller the point spread, the more competitive Vegas
    expects the game to be. Falls back to FanDuel's spread if DraftKings
    hasn't posted one yet; games with no spread posted from either book
    sort last (they're usually just further out from kickoff).
    """
    spread = dk_spread if dk_spread is not None else fd_spread
    if spread is None:
        return 999
    return abs(spread)


def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


def _dk_home_spread(odds):
    return odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")


def _fd_home_spread(odds):
    return odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")


# ---------------------------------------------------------------------------
# Regional pick heuristic (Omaha / Lincoln, NE)
# ---------------------------------------------------------------------------

def pick_regional_game(games_in_window):
    """
    Given all games sharing one network + kickoff window, guess which one
    the Omaha/Lincoln market gets. Only meaningful when there's more than
    one game in the window (if there's only one, everyone gets it -- no
    guess needed).
    """
    if len(games_in_window) < 2:
        return None
    for priority_team in REGIONAL_TEAM_PRIORITY:
        for g in games_in_window:
            if g["home_team"] == priority_team or g["away_team"] == priority_team:
                return g
    return None


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
    odds_rows = fetch_all_odds(sharp_key, league="nfl")
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    # days[date_key][slot] -> list of game entries
    days = {}
    all_games = []  # flat list, mirrors what's in `days`, for the Gemini pass below

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

        odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
        if previous_odds_by_id:
            odds = carry_forward_odds(odds, previous_odds_by_id.get(event.get("id")))
        dk_spread = _dk_home_spread(odds)
        fd_spread = _fd_home_spread(odds)

        game_entry = {
            "id": event.get("id"),
            "start_time": start_raw,
            "start_time_tbd": is_tbd,
            "home_team": home_team,
            "home_abbr": home["team"].get("abbreviation"),
            "away_team": away_team,
            "away_abbr": away["team"].get("abbreviation"),
            "matchup_score": matchup_score(dk_spread, fd_spread),
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        days.setdefault(day_key, {}).setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

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

            # Slot Pick: NFL doesn't have AP rankings to fall back on, so the
            # pick is simply the game with the closest (most competitive)
            # spread in this window -- games_sorted is already sorted that
            # way, so index 0 is it.
            for i, g in enumerate(games_sorted):
                g["is_slot_pick"] = (i == 0)

            # Regional pick heuristic: group this window's games by channel
            by_channel = {}
            for g in games_sorted:
                by_channel.setdefault(g["channel"], []).append(g)
            regional_picks = []
            for channel, channel_games in by_channel.items():
                pick = pick_regional_game(channel_games)
                if pick:
                    regional_picks.append({
                        "channel": channel,
                        "game_id": pick["id"],
                        "matchup": f"{pick['away_team']} @ {pick['home_team']}",
                    })

            time_slots.append({
                "slot": slot_name,
                "best_matchup_score": best_score if best_score != 999 else None,
                "pick_reason": "closest_spread" if games_sorted else None,
                "regional_picks_omaha_lincoln": regional_picks,
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
        "regional_market_note": (
            "regional_picks_omaha_lincoln is an UNOFFICIAL heuristic guess "
            "(Kansas City Chiefs, then Denver Broncos, when a network window "
            "has multiple games) -- not scraped from an actual coverage map. "
            "Verify at https://506sports.com/nfl.php before relying on it."
        ),
        "weeks": weeks,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NFL betting dashboard JSON.")
    p.add_argument("--year", type=int, default=SEASON_YEAR_DEFAULT, help="Season year")
    p.add_argument("--week", type=int, required=False, help="Starting NFL week number (default: ESPN's current week); this week and the following week are both built")
    p.add_argument("--num-weeks", type=int, default=2, help="How many consecutive weeks to build starting at --week (default: 2)")
    p.add_argument("--season-type", type=int, default=SEASON_TYPE_DEFAULT,
                    help="1=preseason, 2=regular season, 3=postseason")
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
    if week is None:
        # ESPN infers "current week" fine with no week param at all.
        resp = requests.get(
            ESPN_SCOREBOARD_URL,
            params={"seasontype": args.season_type},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        week = resp.json().get("week", {}).get("number", 1)
        log(f"No --week given; ESPN reports current week as {week}.")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "nfl_dashboard.json")
    out_path = os.path.abspath(out_path)

    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    output = build(args.year, week, args.season_type, sharp_key, gemini_key,
                    num_weeks=args.num_weeks, previous_odds_by_id=previous_odds_by_id)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    week_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across weeks {week_nums} to {out_path}")
    if args.season_type == 1 and max(week_nums) > 3:
        log("Note: preseason only runs ~3 weeks -- a week number past that rolls into regular season "
            "with a different --season-type, so the 2nd week here may come back empty.")


if __name__ == "__main__":
    main()
