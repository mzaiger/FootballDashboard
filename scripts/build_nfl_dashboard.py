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
    python scripts/build_nfl_dashboard.py --week 1 --year 2026 --season-type 2
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
    fetch_all_odds,
    log,
    match_odds_for_game,
    time_slot_for,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REQUEST_TIMEOUT = 20

# seasontype: 1=preseason, 2=regular season, 3=postseason
SEASON_TYPE_DEFAULT = 1
SEASON_YEAR_DEFAULT = 2026

# 2026 regular-season Week 1 runs Sept 9-15 (confirmed against ESPN's own
# calendar). Used to deterministically default --week to 1 whenever this
# script is run before the season starts, rather than asking ESPN to infer
# "the current week" with no date given -- that inference is undocumented
# behavior, and doesn't reliably map onto Week 1 during the real calendar
# gap between now and the season (we're not even into preseason yet as of
# this writing). Mirrors build_dashboard.py's WEEK1_START/derive_week for CFB.
WEEK1_START = date(2026, 9, 9)

# Regional-pick heuristic priority for the Omaha/Lincoln, NE market (no home
# NFL team). Checked in order; first match in a multi-game window wins.
REGIONAL_TEAM_PRIORITY = ["Kansas City Chiefs", "Denver Broncos"]

# "Main channels" here just means "has a national broadcast at all" -- ESPN's
# API already only returns games with a real network/streaming assignment
# (CBS, FOX, NBC, ESPN, ABC, Amazon, Netflix, NFL Network, Peacock), so
# there's no separate filter needed the way CFB needed one for ESPNU/ACCN/etc.


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
    """Join all national broadcast names for a game, e.g. 'CBS' or 'FOX/CBS'."""
    names = []
    for b in event.get("broadcasts", []):
        for n in b.get("names", []):
            if n not in names:
                names.append(n)
    if names:
        return "/".join(names)
    # geoBroadcasts is the fallback some events use instead of broadcasts[]
    for gb in event.get("geoBroadcasts", []):
        short = gb.get("media", {}).get("shortName")
        if short and short not in names:
            names.append(short)
    return "/".join(names) if names else None


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

def derive_week(today, season_type):
    """
    Deterministically pick a week when --week isn't given, instead of
    trusting ESPN's undocumented "current week" inference. Only meaningful
    for regular season (seasontype 2) -- preseason/postseason callers should
    pass --week explicitly.
    """
    if today < WEEK1_START:
        return 1, True  # before the season starts: default to week 1, flag it
    days_since = (today - WEEK1_START).days
    return (days_since // 7) + 1, False


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

def build(year, week, season_type, sharp_key):
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
    skipped_no_broadcast = 0

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
        if not outlet:
            skipped_no_broadcast += 1
            continue

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

    log(f"  {skipped_no_broadcast} games skipped (no broadcast info yet)")

    day_list = []
    for day_key in sorted(days.keys()):
        slots_for_day = days[day_key]
        time_slots = []
        for slot_name in TIME_SLOT_ORDER:
            if slot_name not in slots_for_day:
                continue
            games_sorted = sorted(slots_for_day[slot_name], key=lambda x: x["matchup_score"])
            best_score = games_sorted[0]["matchup_score"] if games_sorted else None

            # Regional pick heuristic: group this window's games by channel
            # (a "CBS" window and a "FOX" window are simultaneous but
            # separate -- Omaha gets one game from each, not one overall).
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

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "week": week,
        "season_type": season_type,
        "display_timezone": DISPLAY_TIMEZONE,
        "total_games": sum(d["game_count"] for d in day_list),
        "regional_market_note": (
            "regional_picks_omaha_lincoln is an UNOFFICIAL heuristic guess "
            "(Kansas City Chiefs, then Denver Broncos, when a network window "
            "has multiple games) -- not scraped from an actual coverage map. "
            "Verify at https://506sports.com/nfl.php before relying on it."
        ),
        "days": day_list,
    }
    return output


def parse_args():
    p = argparse.ArgumentParser(description="Build the NFL betting dashboard JSON.")
    p.add_argument("--year", type=int, default=SEASON_YEAR_DEFAULT, help="Season year")
    p.add_argument("--week", type=int, required=False,
                    help="NFL week number (default: auto from Sept 9, 2026 Week 1 start)")
    p.add_argument("--season-type", type=int, default=SEASON_TYPE_DEFAULT,
                    help="1=preseason, 2=regular season, 3=postseason")
    p.add_argument("--out", default=None, help="Output path (default: data/nfl_dashboard.json)")
    return p.parse_args()


def autodetect_season(year):
    for season_type in (1, 2, 3):
        for week in range(1, 25):
            try:
                sb = get_scoreboard(year, week, season_type)
                if sb.get("events"):
                    return season_type, week
            except requests.HTTPError:
                pass

    raise RuntimeError("No NFL schedule found.")

def main():
    args = parse_args()

    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    if args.week is None:
        season_type, week = autodetect_season(args.year)
    else:
        season_type = args.season_type
        week = args.week

    output = build(args.year, week, season_type, sharp_key)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "nfl_dashboard.json")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    log(f"Wrote {output['total_games']} games across {len(output['days'])} day(s) to {out_path}")


if __name__ == "__main__":
    main()
