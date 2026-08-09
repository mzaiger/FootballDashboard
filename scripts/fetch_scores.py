#!/usr/bin/env python3
"""
Live score poller -- run hourly via .github/workflows/fetch-scores.yml.

Overlays home/away scores + game status onto the games already listed in
data/dashboard.json (CFB) and data/nfl_dashboard.json (NFL), which
build_dashboard.py / build_nfl_dashboard.py produce once a day. This
script is intentionally lightweight and kept separate from those daily
builds: it does NOT touch odds, AP rankings, or Gemini predictions -- it
just looks up each game's current score by the same game id the daily
build already assigned, and writes a small overlay file, data/scores.json,
that index.html / nfl.html / picks.html fetch and merge in client-side.
Keeping this separate means scores can refresh hourly (or more) without
hitting SharpAPI's or Gemini's much tighter rate limits.

Score sources (matched by the exact game id already in each dashboard):
    CFB - CollegeFootballData.com's /games endpoint (same CFBD_API_KEY
          the daily CFB build uses).
    NFL - ESPN's public scoreboard endpoint (same one build_nfl_dashboard.py
          uses for schedule; no key required).

Env vars required:
    CFBD_API_KEY - only used if data/dashboard.json has CFB games in it
                   (no key needed for the NFL/ESPN half)

Usage:
    python scripts/fetch_scores.py
    python scripts/fetch_scores.py --dashboard data/dashboard.json \\
        --nfl-dashboard data/nfl_dashboard.json --out data/scores.json
"""

import argparse
import json
import os
from datetime import datetime, timezone

import requests

from common import log

CFBD_BASE = "https://api.collegefootballdata.com"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Reading the existing dashboards to know which games/weeks to check
# ---------------------------------------------------------------------------

def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def iter_games(dashboard):
    """Yield (week_number, game_dict) for every game in a dashboard payload."""
    if not dashboard:
        return
    for week in dashboard.get("weeks", []):
        for day in week.get("days", []):
            for slot in day.get("time_slots", []):
                for g in slot.get("games", []):
                    yield week["week"], g


def distinct_weeks(dashboard):
    return sorted({week for week, _ in iter_games(dashboard)})


# ---------------------------------------------------------------------------
# CFB scores (CollegeFootballData.com)
# ---------------------------------------------------------------------------

def cfbd_get_games(key, year, week, season_type="regular"):
    resp = requests.get(
        f"{CFBD_BASE}/games",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        params={"year": year, "week": week, "seasonType": season_type, "classification": "fbs"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_cfb_scores(dashboard, cfbd_key):
    """Return {game_id: {home_score, away_score, status, status_detail}}."""
    scores = {}
    if not dashboard or not cfbd_key:
        return scores

    year = dashboard.get("season")
    for week in distinct_weeks(dashboard):
        log(f"CFB scores: fetching week {week}...")
        try:
            games = cfbd_get_games(cfbd_key, year, week)
        except requests.RequestException as e:
            log(f"  WARNING: couldn't fetch CFB scores for week {week}: {e}")
            continue

        for g in games:
            game_id = g.get("id")
            if game_id is None:
                continue
            home_pts = g.get("homePoints")
            away_pts = g.get("awayPoints")
            if home_pts is None and away_pts is None:
                continue  # game hasn't kicked off (or CFBD hasn't posted yet)
            completed = bool(g.get("completed"))
            scores[str(game_id)] = {
                "home_score": home_pts,
                "away_score": away_pts,
                "status": "final" if completed else "in_progress",
                "status_detail": "Final" if completed else None,
            }
    return scores


# ---------------------------------------------------------------------------
# NFL scores (ESPN public scoreboard)
# ---------------------------------------------------------------------------

def espn_get_scoreboard(year, week, season_type):
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": year, "week": week, "seasontype": season_type, "limit": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_nfl_scores(dashboard):
    """Return {game_id: {home_score, away_score, status, status_detail}}."""
    scores = {}
    if not dashboard:
        return scores

    year = dashboard.get("season")
    season_type = dashboard.get("season_type", 1)
    for week in distinct_weeks(dashboard):
        log(f"NFL scores: fetching week {week}...")
        try:
            payload = espn_get_scoreboard(year, week, season_type)
        except requests.RequestException as e:
            log(f"  WARNING: couldn't fetch NFL scores for week {week}: {e}")
            continue

        for event in payload.get("events", []):
            game_id = event.get("id")
            if game_id is None:
                continue
            comp = (event.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            status = event.get("status", {}).get("type", {})
            state = status.get("state")  # "pre" / "in" / "post"
            if state == "pre":
                continue  # hasn't started -- nothing to overlay yet

            home_score = home.get("score")
            away_score = away.get("score")
            scores[str(game_id)] = {
                "home_score": int(home_score) if home_score is not None else None,
                "away_score": int(away_score) if away_score is not None else None,
                "status": "final" if state == "post" else "in_progress",
                "status_detail": status.get("shortDetail") or status.get("detail"),
            }
    return scores


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch current scores and write an overlay file for the existing dashboards."
    )
    parser.add_argument("--dashboard", default=None, help="Path to data/dashboard.json (CFB)")
    parser.add_argument("--nfl-dashboard", default=None, help="Path to data/nfl_dashboard.json")
    parser.add_argument("--out", default=None, help="Output path (default: data/scores.json)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_path = os.path.abspath(args.dashboard or os.path.join(script_dir, "..", "data", "dashboard.json"))
    nfl_dashboard_path = os.path.abspath(
        args.nfl_dashboard or os.path.join(script_dir, "..", "data", "nfl_dashboard.json")
    )
    out_path = os.path.abspath(args.out or os.path.join(script_dir, "..", "data", "scores.json"))

    cfbd_key = os.environ.get("CFBD_API_KEY")
    if not cfbd_key:
        log("CFBD_API_KEY not set -- skipping CFB scores.")

    dashboard = load_json(dashboard_path)
    nfl_dashboard = load_json(nfl_dashboard_path)

    cfb_scores = fetch_cfb_scores(dashboard, cfbd_key)
    nfl_scores = fetch_nfl_scores(nfl_dashboard)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cfb": cfb_scores,
        "nfl": nfl_scores,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    log(f"Wrote {len(cfb_scores)} CFB score(s) and {len(nfl_scores)} NFL score(s) to {out_path}")


if __name__ == "__main__":
    main()
