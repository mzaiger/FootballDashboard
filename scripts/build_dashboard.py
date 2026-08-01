#!/usr/bin/env python3
"""
College Football Betting Dashboard builder.

Pulls this week's TV schedule + AP rankings from CollegeFootballData.com (CFBD),
ranks each matchup by combined team rank (lower = marquee game), then attaches
DraftKings / FanDuel spread + moneyline odds from SharpAPI. Exports everything
to data/dashboard.json for the static index.html front-end to consume.

Env vars required:
    CFBD_API_KEY     - key from https://collegefootballdata.com/key
    SHARPAPI_KEY     - key from https://sharpapi.io

Usage:
    python scripts/build_dashboard.py
    python scripts/build_dashboard.py --week 1 --year 2026
"""

import argparse
import difflib
import json
import os
import sys
import time
from datetime import datetime, timezone, date

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CFBD_BASE = "https://api.collegefootballdata.com"
SHARPAPI_BASE = "https://api.sharpapi.io/api/v1"

# Season 1 kicks off the week of Aug 22, 2026 (per user). Used to auto-derive
# the current CFBD week number if --week isn't passed explicitly.
SEASON_YEAR_DEFAULT = 2026
WEEK1_START = date(2026, 8, 22)

# "Main channels" = national broadcast + flagship cable. Games on ESPNU, ACCN,
# SECN, ESPN+, streaming-only, etc. are filtered out. Edit this list to widen
# or narrow what counts as a "main channel" game.
MAIN_CHANNELS = {"ABC", "CBS", "NBC", "FOX", "ESPN", "ESPN2", "FS1"}

# Unranked teams get this rank value for matchup-score purposes so ranked-vs-
# unranked and unranked-vs-unranked games still sort sensibly (worst last).
UNRANKED_VALUE = 26

REQUEST_TIMEOUT = 20
SHARPAPI_PAGE_LIMIT = 200  # ask for big pages; we still follow pagination


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CFBD calls
# ---------------------------------------------------------------------------

def cfbd_get(path, key, params=None):
    resp = requests.get(
        f"{CFBD_BASE}{path}",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_games(key, year, week, season_type="regular"):
    return cfbd_get(
        "/games",
        key,
        {"year": year, "week": week, "seasonType": season_type, "classification": "fbs"},
    )


def get_media(key, year, week, season_type="regular"):
    return cfbd_get(
        "/games/media",
        key,
        {"year": year, "week": week, "seasonType": season_type, "classification": "fbs"},
    )


def get_rankings(key, year, week, season_type="regular"):
    return cfbd_get("/rankings", key, {"year": year, "week": week, "seasonType": season_type})


def build_rank_lookup(rankings_payload, poll_name="AP Top 25"):
    """Return {team_name: rank} from the first matching poll release."""
    lookup = {}
    for release in rankings_payload:
        for poll in release.get("polls", []):
            if poll.get("poll") == poll_name:
                for entry in poll.get("ranks", []):
                    lookup[entry["school"]] = entry["rank"]
                return lookup  # first release for the requested week is what we want
    # Fallback: if AP Top 25 isn't present yet (early preseason), try any poll
    for release in rankings_payload:
        for poll in release.get("polls", []):
            for entry in poll.get("ranks", []):
                lookup.setdefault(entry["school"], entry["rank"])
        if lookup:
            return lookup
    return lookup


# ---------------------------------------------------------------------------
# SharpAPI calls
# ---------------------------------------------------------------------------

def fetch_all_ncaaf_odds(sharp_key, sportsbooks=("draftkings", "fanduel"),
                          markets=("spread", "moneyline")):
    """Pull every NCAAF odds row for the given books/markets, following pagination."""
    rows = []
    offset = 0
    while True:
        params = {
            "league": "ncaaf",
            "sportsbook": ",".join(sportsbooks),
            "market": ",".join(markets),
            "limit": SHARPAPI_PAGE_LIMIT,
            "offset": offset,
        }
        resp = requests.get(
            f"{SHARPAPI_BASE}/odds",
            headers={"X-API-Key": sharp_key},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("X-RateLimit-Reset", 5))
            log(f"SharpAPI rate limited, sleeping {wait}s")
            time.sleep(max(wait, 1))
            continue
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get("data", []))
        meta = payload.get("meta", {})
        pagination = meta.get("pagination", {})
        if not pagination.get("has_more"):
            break
        offset = pagination.get("next_offset", offset + SHARPAPI_PAGE_LIMIT)
        time.sleep(0.3)  # be polite to the free tier (12 req/min)
    return rows


def _normalize(name):
    return (
        name.lower()
        .replace("st.", "state")
        .replace("univ.", "")
        .replace("university", "")
        .strip()
    )


def match_odds_for_game(home_team, away_team, odds_rows, team_cache):
    """
    SharpAPI team names don't always match CFBD team names exactly
    (mascots, abbreviations, etc.), so fuzzy-match once per unique
    team pair and cache the result.
    """
    cache_key = (home_team, away_team)
    if cache_key in team_cache:
        return team_cache[cache_key]

    home_norm = _normalize(home_team)
    away_norm = _normalize(away_team)

    candidates = [
        r for r in odds_rows
        if _fuzzy_team(home_norm, r.get("home_team", "")) and _fuzzy_team(away_norm, r.get("away_team", ""))
    ]

    result = {"draftkings": {"spread": {}, "moneyline": {}}, "fanduel": {"spread": {}, "moneyline": {}}}

    for row in candidates:
        book = row.get("sportsbook")
        market = row.get("market_type")
        if book not in result or market not in ("spread", "moneyline"):
            continue
        selection = row.get("selection", "")
        side = "home" if _fuzzy_team(home_norm, selection) else (
            "away" if _fuzzy_team(away_norm, selection) else None
        )
        if side is None:
            continue
        result[book][market][side] = {
            "line": row.get("point", row.get("odds_point")),
            "american": row.get("odds_american"),
        }

    team_cache[cache_key] = result
    return result


def _fuzzy_team(normalized_target, candidate_raw):
    if not candidate_raw:
        return False
    candidate = _normalize(candidate_raw)
    if normalized_target in candidate or candidate in normalized_target:
        return True
    return difflib.SequenceMatcher(None, normalized_target, candidate).ratio() > 0.55


# ---------------------------------------------------------------------------
# Matchup ranking
# ---------------------------------------------------------------------------

def matchup_score(home_rank, away_rank):
    """Lower score = better/more marquee matchup (sum of ranks, unranked=26)."""
    h = home_rank if home_rank is not None else UNRANKED_VALUE
    a = away_rank if away_rank is not None else UNRANKED_VALUE
    return h + a


def derive_week(today, year):
    if today < WEEK1_START:
        return 1, True  # preseason: default to week 1, flag it
    days_since = (today - WEEK1_START).days
    return (days_since // 7) + 1, False


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build(year, week, cfbd_key, sharp_key, channels):
    log(f"Fetching games for {year} week {week}...")
    games = get_games(cfbd_key, year, week)
    log(f"  {len(games)} games")

    log("Fetching TV/media info...")
    media = get_media(cfbd_key, year, week)
    media_by_game = {m["id"]: m for m in media if m.get("id")}

    log("Fetching AP rankings...")
    rankings_payload = get_rankings(cfbd_key, year, week)
    rank_lookup = build_rank_lookup(rankings_payload)
    log(f"  {len(rank_lookup)} ranked teams found")

    log("Fetching DraftKings/FanDuel NCAAF odds from SharpAPI...")
    odds_rows = fetch_all_ncaaf_odds(sharp_key)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}

    days = {}
    skipped_no_tv = 0

    for g in games:
        game_id = g.get("id")
        media_info = media_by_game.get(game_id)
        outlet = (media_info or {}).get("outlet")
        if not outlet or outlet not in channels:
            skipped_no_tv += 1
            continue

        start_raw = g.get("startDate")
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        day_key = start_dt.date().isoformat()

        home_team, away_team = g.get("homeTeam"), g.get("awayTeam")
        home_rank = rank_lookup.get(home_team)
        away_rank = rank_lookup.get(away_team)

        odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache)

        game_entry = {
            "id": game_id,
            "start_time": start_raw,
            "start_time_tbd": g.get("startTimeTBD", False),
            "home_team": home_team,
            "home_conference": g.get("homeConference"),
            "home_rank": home_rank,
            "away_team": away_team,
            "away_conference": g.get("awayConference"),
            "away_rank": away_rank,
            "matchup_score": matchup_score(home_rank, away_rank),
            "channel": outlet,
            "venue": g.get("venue"),
            "neutral_site": g.get("neutralSite", False),
            "odds": odds,
        }
        days.setdefault(day_key, []).append(game_entry)

    log(f"  {skipped_no_tv} games skipped (not on a main channel)")

    day_list = []
    for day_key in sorted(days.keys()):
        games_for_day = sorted(days[day_key], key=lambda x: x["matchup_score"])
        weekday_name = datetime.fromisoformat(day_key).strftime("%A")
        day_list.append({"date": day_key, "weekday": weekday_name, "games": games_for_day})

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "week": week,
        "main_channels": sorted(channels),
        "total_games": sum(len(d["games"]) for d in day_list),
        "days": day_list,
    }
    return output


def parse_args():
    p = argparse.ArgumentParser(description="Build the CFB betting dashboard JSON.")
    p.add_argument("--year", type=int, default=None, help="Season year (default: auto)")
    p.add_argument("--week", type=int, default=None, help="CFBD week number (default: auto from Aug 22 start)")
    p.add_argument("--out", default=None, help="Output path (default: data/dashboard.json)")
    return p.parse_args()


def main():
    args = parse_args()

    cfbd_key = os.environ.get("CFBD_API_KEY")
    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not cfbd_key:
        sys.exit("Missing CFBD_API_KEY environment variable (get one at collegefootballdata.com/key)")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    today = datetime.now(timezone.utc).date()
    year = args.year or SEASON_YEAR_DEFAULT
    if args.week is not None:
        week, preseason = args.week, False
    else:
        week, preseason = derive_week(today, year)
        if preseason:
            log(f"Today ({today}) is before the {year} week-1 start ({WEEK1_START}); defaulting to week 1.")

    output = build(year, week, cfbd_key, sharp_key, MAIN_CHANNELS)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "dashboard.json")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    log(f"Wrote {output['total_games']} games across {len(output['days'])} day(s) to {out_path}")


if __name__ == "__main__":
    main()
