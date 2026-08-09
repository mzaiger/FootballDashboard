"""
Gemini prediction helper, shared by build_dashboard.py (CFB) and
build_nfl_dashboard.py (NFL).

For every game that has at least one posted line (DraftKings or FanDuel,
spread or moneyline), asks Gemini for a straight-up winner pick, an ATS
pick, a 1-100 confidence score, and a five-sentence explanation -- using
only current-season data. The result is written onto the game dict as
"gemini_prediction" so it flows straight into nfl_dashboard.json /
dashboard.json.

Caching: predictions are cached in data/gemini_predictions_cache.json,
keyed by a hash of (sport, season, week, matchup, DK odds, FD odds). If
none of those change between runs -- which is the normal case when the
build script runs daily but the odds haven't moved -- the cached
prediction is reused and no API call is made. A call only happens the
first time a game is seen, or after its odds change. That naturally
satisfies "once a week, or when odds move," since re-running the same
week with the same odds is always a cache hit.

Env var required: GEMINI_KEY (a GitHub Actions secret / local env var).
If it's missing, predictions are skipped entirely -- the rest of the
build still runs normally.
"""

import concurrent.futures
import hashlib
import json
import os
from datetime import datetime, timezone

import requests

from common import log

GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_TIMEOUT = 30
MAX_WORKERS = 6

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "data", "gemini_predictions_cache.json"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def _odds_hash(sport, season, week, away_team, home_team, odds):
    """Cache key that changes iff the matchup or its odds change. Same
    matchup + same odds on a later run -> same hash -> cache hit -> no
    Gemini call."""
    dk_spread = (odds.get("draftkings", {}).get("spread", {}).get("home") or {})
    dk_ml_away = (odds.get("draftkings", {}).get("moneyline", {}).get("away") or {})
    dk_ml_home = (odds.get("draftkings", {}).get("moneyline", {}).get("home") or {})
    fd_spread = (odds.get("fanduel", {}).get("spread", {}).get("home") or {})
    fd_ml_away = (odds.get("fanduel", {}).get("moneyline", {}).get("away") or {})
    fd_ml_home = (odds.get("fanduel", {}).get("moneyline", {}).get("home") or {})

    key_material = "|".join(str(x) for x in [
        sport, season, week, away_team, home_team,
        "DK", dk_spread.get("line"), dk_ml_away.get("american"), dk_ml_home.get("american"),
        "FD", fd_spread.get("line"), fd_ml_away.get("american"), fd_ml_home.get("american"),
    ])
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Prompt + API call
# ---------------------------------------------------------------------------

def _fmt_book(book):
    spread_home = (book.get("spread", {}).get("home") or {})
    ml_away = (book.get("moneyline", {}).get("away") or {})
    ml_home = (book.get("moneyline", {}).get("home") or {})
    lines = []
    line = spread_home.get("line")
    if line is not None:
        lines.append(f"Home spread: {line:+g}")
    if ml_away.get("american") is not None:
        lines.append(f"Away moneyline: {ml_away['american']:+g}")
    if ml_home.get("american") is not None:
        lines.append(f"Home moneyline: {ml_home['american']:+g}")
    return "\n".join(lines) if lines else "No odds posted yet"


def _build_prompt(sport, season, week, away_team, home_team, odds):
    league_label = "NFL" if sport == "nfl" else "college football"
    dk = odds.get("draftkings", {})
    fd = odds.get("fanduel", {})
    return f"""You are analyzing an upcoming {season} {league_label} game (week {week}).

Away Team: {away_team}
Home Team: {home_team}

DraftKings:
{_fmt_book(dk)}

FanDuel:
{_fmt_book(fd)}

Using ONLY statistics, injuries, roster status, and performance from the \
{season} season, determine:
1. Who wins outright.
2. Who covers the spread (only if a spread is posted above; otherwise null).
3. A confidence score from 1-100 for the outright winner pick.
4. A five-sentence explanation of the reasoning.

Ignore previous seasons, franchise history, and reputation -- current-season \
data only.

Respond with ONLY this JSON object, no other text:
{{"winner": "<team name>", "ats_pick": "<team name and line, or null>", "confidence": <integer 1-100>, "analysis": "<exactly five sentences>"}}"""


def _call_gemini(prompt, gemini_key):
    resp = requests.post(
        GEMINI_API_URL,
        params={"key": gemini_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        },
        timeout=GEMINI_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def attach_gemini_predictions(games, sport, season, week, gemini_key):
    """Mutates each dict in `games` (a flat list of game_entry dicts, each
    already carrying an "odds" field) by adding a "gemini_prediction" key,
    for any game with at least one posted line. Reuses cached predictions
    when the odds hash hasn't changed; only calls Gemini for new/changed
    games, concurrently, then saves the updated cache once at the end.
    """
    if not gemini_key:
        log("No GEMINI_KEY set -- skipping Gemini predictions.")
        return

    cache = _load_cache()
    to_call = []

    for g in games:
        odds = g.get("odds") or {}
        has_odds = any(
            (odds.get(book, {}).get("spread") or odds.get(book, {}).get("moneyline"))
            for book in ("draftkings", "fanduel")
        )
        if not has_odds:
            continue

        h = _odds_hash(sport, season, week, g["away_team"], g["home_team"], odds)
        cached = cache.get(h)
        if cached:
            g["gemini_prediction"] = cached
            continue

        prompt = _build_prompt(sport, season, week, g["away_team"], g["home_team"], odds)
        to_call.append((g, h, prompt))

    if not to_call:
        log("Gemini predictions: nothing new to call (all cached, or no odds posted yet).")
        return

    log(f"Gemini predictions: calling for {len(to_call)} game(s) with new/changed odds...")

    def _worker(item):
        g, h, prompt = item
        try:
            result = _call_gemini(prompt, gemini_key)
            result["generated_at"] = datetime.now(timezone.utc).isoformat()
            result["odds_hash"] = h
            return g, h, result, None
        except Exception as e:  # noqa: BLE001 -- one bad game shouldn't kill the build
            return g, h, None, str(e)

    called, failed = 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for g, h, result, err in ex.map(_worker, to_call):
            if err:
                failed += 1
                log(f"  Gemini call failed for {g['away_team']} @ {g['home_team']}: {err}")
                continue
            g["gemini_prediction"] = result
            cache[h] = result
            called += 1

    log(f"Gemini predictions: {called} succeeded, {failed} failed.")
    if called:
        _save_cache(cache)
