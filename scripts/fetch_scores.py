#!/usr/bin/env python3
"""
Live score poller -- run hourly via .github/workflows/fetch-scores.yml.

Overlays home/away scores + game status onto the games already listed in
data/dashboard.json (CFB), data/nfl_dashboard.json (NFL), and
data/mlb_dashboard.json (MLB), which build_dashboard.py /
build_nfl_dashboard.py / build_mlb_dashboard.py produce once a day. This
script is intentionally lightweight and kept separate from those daily
builds: it does NOT touch odds, AP rankings, probable pitchers, or Gemini
predictions -- it just looks up each game's current score by the same
game id the daily build already assigned, and writes a small overlay
file, data/scores.json, that index.html / nfl.html / mlb.html /
picks.html fetch and merge in client-side. Keeping this separate means
scores can refresh hourly (or more) without hitting SharpAPI's or
Gemini's much tighter rate limits.

Score sources (matched by the exact game id already in each dashboard for
NFL and MLB; matched by team name + date for CFB -- see note below):
    CFB - ESPN's public college-football scoreboard endpoint (no key
          required). Matched back to our CFBD-numbered game ids by fuzzy
          team-name matching (same matcher common.py uses for odds)
          plus date, since ESPN's own event ids don't line up with
          CFBD's. Switched from CFBD's /games endpoint specifically
          because CFBD only ever reports final scores -- no in-progress
          quarter/clock -- while ESPN's status.type.shortDetail gives a
          real "8:42 - 3rd" while a game is live.
    NFL - ESPN's public scoreboard endpoint (same one build_nfl_dashboard.py
          uses for schedule; no key required).
    MLB - ESPN's public baseball scoreboard endpoint (same one
          build_mlb_dashboard.py uses for schedule; no key required).
          Matched directly by event id, same as NFL, since
          build_mlb_dashboard.py's game ids already come from ESPN.

Env vars required: none -- all three sources use ESPN's public endpoints.

Usage:
    python scripts/fetch_scores.py
    python scripts/fetch_scores.py --dashboard data/dashboard.json \\
        --nfl-dashboard data/nfl_dashboard.json \\
        --mlb-dashboard data/mlb_dashboard.json --out data/scores.json
"""

import argparse
import json
import os
from datetime import datetime, timezone

import requests

from common import log, _normalize, _fuzzy_team

ESPN_CFB_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_MLB_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Reading the existing dashboards to know which games/weeks to check
# ---------------------------------------------------------------------------

def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def load_previous_scores(path):
    """Read the existing scores.json (previous run's output), or empty
    cfb/nfl dicts if it doesn't exist yet or can't be parsed.

    Used so scores for a week that's aged out of the current dashboard's
    rolling 2-week window (and is therefore no longer requested from
    CFBD/ESPN this run) don't just vanish from scores.json -- see the
    merge in main() below.
    """
    data = load_json(path)
    if not data:
        return {"cfb": {}, "nfl": {}, "mlb": {}}
    return {
        "cfb": data.get("cfb", {}) or {},
        "nfl": data.get("nfl", {}) or {},
        "mlb": data.get("mlb", {}) or {},
    }


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


def weeks_needing_refresh(dashboard, previous_scores):
    """Which week numbers are still worth asking CFBD/ESPN about.

    Now that dashboard.json/nfl_dashboard.json keep every week ever built
    (see merge_weeks() in common.py) instead of aging old ones out, a naive
    "refetch every week in the file" would re-poll the entire season's
    worth of already-final games every single hour forever -- wasted CFBD
    calls and an ever-growing runtime for zero benefit, since a final score
    doesn't change. A week is skipped only when every game in it already
    has a "final" status recorded in the previous run's scores.json; any
    week with an unplayed/in-progress game, or a game we've never fetched a
    score for at all, still gets checked every run.
    """
    by_week = {}
    for week, g in iter_games(dashboard):
        by_week.setdefault(week, []).append(g)

    weeks = []
    for week, games in by_week.items():
        all_final = games and all(
            (previous_scores.get(str(g.get("id")), {}) or {}).get("status") == "final"
            for g in games
        )
        if not all_final:
            weeks.append(week)
    return sorted(weeks)


# ---------------------------------------------------------------------------
# CFB scores (ESPN public college-football scoreboard)
# ---------------------------------------------------------------------------

def espn_get_cfb_scoreboard(dates_param):
    resp = requests.get(
        ESPN_CFB_SCOREBOARD_URL,
        params={"dates": dates_param, "groups": 80, "limit": 500},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _cfb_games_needing_scores(dashboard, weeks_to_fetch):
    """Yield (date_str "YYYYMMDD", game_dict) for every game in the given
    week numbers of the CFB dashboard (every week if weeks_to_fetch is
    None)."""
    if not dashboard:
        return
    wanted = set(weeks_to_fetch) if weeks_to_fetch is not None else None
    for week in dashboard.get("weeks", []):
        if wanted is not None and week["week"] not in wanted:
            continue
        for day in week.get("days", []):
            date_str = (day.get("date") or "").replace("-", "")
            for slot in day.get("time_slots", []):
                for g in slot.get("games", []):
                    yield date_str, g


def _match_espn_cfb_event(g, events):
    """Find the ESPN event (if any) whose competitors match this
    dashboard game's home/away teams, checking both straight and
    flipped (neutral-site) orientation -- same approach
    match_odds_for_game() in common.py uses for odds. Returns
    (event, home_competitor, away_competitor) or None.
    """
    home_norm = _normalize(g.get("home_team", ""))
    away_norm = _normalize(g.get("away_team", ""))

    for event in events:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue
        home_name = (home_c.get("team") or {}).get("displayName", "")
        away_name = (away_c.get("team") or {}).get("displayName", "")

        if _fuzzy_team(home_norm, home_name) and _fuzzy_team(away_norm, away_name):
            return event, home_c, away_c
        if _fuzzy_team(home_norm, away_name) and _fuzzy_team(away_norm, home_name):
            # ESPN has the two teams flipped relative to our dashboard --
            # keep returning (home_c, away_c) in OUR home/away order.
            return event, away_c, home_c
    return None


def fetch_cfb_scores(dashboard, weeks_to_fetch=None):
    """Return {game_id: {home_score, away_score, status, status_detail}}.

    Sourced from ESPN's public college-football scoreboard rather than
    CFBD, specifically so a live game's status_detail carries a real
    quarter/clock (e.g. "8:42 - 3rd") -- CFBD's /games endpoint only
    ever reports final scores, never in-game state. ESPN's own event
    ids don't match CFBD's, so games are matched back to our
    CFBD-numbered game ids by team name (fuzzy, both orientations) plus
    the game's calendar date.

    `weeks_to_fetch` restricts which week numbers are pulled from the
    dashboard (see weeks_needing_refresh()); defaults to every week in
    the dashboard if not given. One ESPN request covers the whole date
    range spanned by those weeks.
    """
    scores = {}
    if not dashboard:
        return scores

    games_by_date = {}
    for date_str, g in _cfb_games_needing_scores(dashboard, weeks_to_fetch):
        if not date_str:
            continue
        games_by_date.setdefault(date_str, []).append(g)
    if not games_by_date:
        return scores

    dates_sorted = sorted(games_by_date)
    dates_param = dates_sorted[0] if len(dates_sorted) == 1 else f"{dates_sorted[0]}-{dates_sorted[-1]}"

    log(f"CFB scores: fetching ESPN scoreboard for {dates_param}...")
    try:
        payload = espn_get_cfb_scoreboard(dates_param)
    except requests.RequestException as e:
        log(f"  WARNING: couldn't fetch CFB scores from ESPN ({dates_param}): {e}")
        return scores

    # Bucket ESPN events by their own calendar date so a dashboard game
    # only ever gets matched against an ESPN event from the same day.
    events_by_date = {}
    for event in payload.get("events", []):
        event_date = (event.get("date") or "")[:10].replace("-", "")
        events_by_date.setdefault(event_date, []).append(event)

    for date_str, games in games_by_date.items():
        events = events_by_date.get(date_str, [])
        for g in games:
            game_id = g.get("id")
            if game_id is None or not events:
                continue

            match = _match_espn_cfb_event(g, events)
            if not match:
                continue
            event, home_c, away_c = match

            status = event.get("status", {}).get("type", {})
            state = status.get("state")  # "pre" / "in" / "post"
            if state == "pre":
                continue  # hasn't started -- nothing to overlay yet

            home_score = home_c.get("score")
            away_score = away_c.get("score")
            scores[str(game_id)] = {
                "home_score": int(home_score) if home_score is not None else None,
                "away_score": int(away_score) if away_score is not None else None,
                "status": "final" if state == "post" else "in_progress",
                "status_detail": status.get("shortDetail") or status.get("detail"),
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


def fetch_nfl_scores(dashboard, weeks_to_fetch=None):
    """Return {game_id: {home_score, away_score, status, status_detail}}.

    `weeks_to_fetch` restricts which week numbers are actually requested
    from ESPN (see weeks_needing_refresh()); defaults to every week in the
    dashboard if not given.
    """
    scores = {}
    if not dashboard:
        return scores

    year = dashboard.get("season")
    season_type = dashboard.get("season_type", 1)
    weeks = weeks_to_fetch if weeks_to_fetch is not None else distinct_weeks(dashboard)
    for week in weeks:
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
# MLB scores (ESPN public scoreboard)
# ---------------------------------------------------------------------------

def espn_get_mlb_scoreboard(date_str):
    resp = requests.get(
        ESPN_MLB_SCOREBOARD_URL,
        params={"dates": date_str, "limit": 200},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_mlb_scores(dashboard, weeks_to_fetch=None):
    """Return {game_id: {home_score, away_score, status, status_detail}}.

    `weeks_to_fetch` here is really a list of dates (build_mlb_dashboard.py
    uses each day's YYYYMMDD as its "week" number -- see that script's
    module docstring), so this fetches one ESPN scoreboard request per day
    that still needs a refresh, matched directly by event id (build_mlb_
    dashboard.py's game ids already come from ESPN, so no fuzzy matching
    is needed here, same as NFL).
    """
    scores = {}
    if not dashboard:
        return scores

    days = weeks_to_fetch if weeks_to_fetch is not None else distinct_weeks(dashboard)
    for day_num in days:
        date_str = str(day_num)
        log(f"MLB scores: fetching {date_str}...")
        try:
            payload = espn_get_mlb_scoreboard(date_str)
        except requests.RequestException as e:
            log(f"  WARNING: couldn't fetch MLB scores for {date_str}: {e}")
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
    parser.add_argument("--mlb-dashboard", default=None, help="Path to data/mlb_dashboard.json")
    parser.add_argument("--out", default=None, help="Output path (default: data/scores.json)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_path = os.path.abspath(args.dashboard or os.path.join(script_dir, "..", "data", "dashboard.json"))
    nfl_dashboard_path = os.path.abspath(
        args.nfl_dashboard or os.path.join(script_dir, "..", "data", "nfl_dashboard.json")
    )
    mlb_dashboard_path = os.path.abspath(
        args.mlb_dashboard or os.path.join(script_dir, "..", "data", "mlb_dashboard.json")
    )
    out_path = os.path.abspath(args.out or os.path.join(script_dir, "..", "data", "scores.json"))

    dashboard = load_json(dashboard_path)
    nfl_dashboard = load_json(nfl_dashboard_path)
    mlb_dashboard = load_json(mlb_dashboard_path)

    previous = load_previous_scores(out_path)

    cfb_weeks = weeks_needing_refresh(dashboard, previous["cfb"])
    nfl_weeks = weeks_needing_refresh(nfl_dashboard, previous["nfl"])
    mlb_days = weeks_needing_refresh(mlb_dashboard, previous["mlb"])
    log(f"CFB weeks needing a refresh: {cfb_weeks} (skipping any week where every game is already final)")
    log(f"NFL weeks needing a refresh: {nfl_weeks} (skipping any week where every game is already final)")
    log(f"MLB days needing a refresh: {mlb_days} (skipping any day where every game is already final)")

    cfb_scores = fetch_cfb_scores(dashboard, weeks_to_fetch=cfb_weeks)
    nfl_scores = fetch_nfl_scores(nfl_dashboard, weeks_to_fetch=nfl_weeks)
    mlb_scores = fetch_mlb_scores(mlb_dashboard, weeks_to_fetch=mlb_days)

    # Merge onto the previous file rather than replacing it -- a game whose
    # week/day has aged out of the dashboard's rolling window isn't
    # re-fetched this run, but its last-known score (almost always "final"
    # by then) stays in scores.json instead of disappearing. Freshly
    # fetched entries always win over old ones for any game id present in
    # both.
    merged_cfb = {**previous["cfb"], **cfb_scores}
    merged_nfl = {**previous["nfl"], **nfl_scores}
    merged_mlb = {**previous["mlb"], **mlb_scores}

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cfb": merged_cfb,
        "nfl": merged_nfl,
        "mlb": merged_mlb,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    log(f"Wrote {len(merged_cfb)} CFB score(s) ({len(cfb_scores)} fresh), "
        f"{len(merged_nfl)} NFL score(s) ({len(nfl_scores)} fresh), and "
        f"{len(merged_mlb)} MLB score(s) ({len(mlb_scores)} fresh) to {out_path}")


if __name__ == "__main__":
    main()
