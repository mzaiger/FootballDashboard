"""
Gemini prediction helper, shared by build_dashboard.py (CFB) and
build_nfl_dashboard.py (NFL).

For every game that has at least one posted line (DraftKings or FanDuel,
spread or moneyline), asks Gemini for:
- a straight-up winner pick
- an ATS pick
- a 1-100 confidence score for the straight-up winner
- a 0-100 confidence score for the ATS pick
- a five-sentence explanation

Uses only current-season data.

Model: gemini-3.5-flash-lite (its quota pool is separate from 3.6 flash).

Rate limiting:
- 1 API call every 10 seconds (6/minute)
- retries wait a full minute
- games are processed sequentially, one at a time
- if Gemini reports the *daily* quota (RequestsPerDay) is exhausted, the
  run stops calling immediately; uncalled games are picked up on the next
  run (the cache preserves progress).

Caching:
Predictions are cached in data/gemini_predictions_cache.json, keyed by a
hash of (model, sport, season, week, matchup, DK odds, FD odds). If none
of those change between runs, the cached prediction is reused and no API
call is made.

Env var required: GEMINI_KEY.
If missing, predictions are skipped entirely.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests

from common import log


GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_TIMEOUT = 30

# One request every 10 seconds (6/minute).
MIN_CALL_INTERVAL = 10.0

# Retries wait a full minute.
RETRY_DELAY_SECONDS = 60.0
MAX_RETRIES = 5

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "data", "gemini_predictions_cache.json"))


class DailyQuotaExceeded(RuntimeError):
    """Gemini's per-day request quota (RPD) is exhausted; retrying today is pointless."""


_daily_quota_exhausted = threading.Event()


class _RateLimiter:
    """Spaces out calls to at most one every `min_interval` seconds."""

    def __init__(self, min_interval):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_allowed)
            self._next_allowed = start_at + self._min_interval

        sleep_for = start_at - now
        if sleep_for > 0:
            time.sleep(sleep_for)


_rate_limiter = _RateLimiter(MIN_CALL_INTERVAL)


#---------------------------------------------------------------------------
# Cache
#---------------------------------------------------------------------------

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
    """
    Cache key that changes iff the model, matchup, or its odds change.
    Including the model means switching models gets fresh predictions
    instead of reusing another model's cached output.
    """
    dk_spread = (odds.get("draftkings", {}).get("spread", {}).get("home") or {})
    dk_ml_away = (odds.get("draftkings", {}).get("moneyline", {}).get("away") or {})
    dk_ml_home = (odds.get("draftkings", {}).get("moneyline", {}).get("home") or {})

    fd_spread = (odds.get("fanduel", {}).get("spread", {}).get("home") or {})
    fd_ml_away = (odds.get("fanduel", {}).get("moneyline", {}).get("away") or {})
    fd_ml_home = (odds.get("fanduel", {}).get("moneyline", {}).get("home") or {})

    key_material = "|".join(str(x) for x in [
        GEMINI_MODEL,
        sport,
        season,
        week,
        away_team,
        home_team,
        "DK",
        dk_spread.get("line"),
        dk_ml_away.get("american"),
        dk_ml_home.get("american"),
        "FD",
        fd_spread.get("line"),
        fd_ml_away.get("american"),
        fd_ml_home.get("american"),
    ])

    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


#---------------------------------------------------------------------------
# Prompt helpers
#---------------------------------------------------------------------------

def _fmt_odds_value(value):
    try:
        return f"{float(value):+g}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_book(book):
    spread_home = (book.get("spread", {}).get("home") or {})
    ml_away = (book.get("moneyline", {}).get("away") or {})
    ml_home = (book.get("moneyline", {}).get("home") or {})

    lines = []

    line = spread_home.get("line")
    if line is not None:
        lines.append(f"Home spread: {_fmt_odds_value(line)}")

    if ml_away.get("american") is not None:
        lines.append(f"Away moneyline: {_fmt_odds_value(ml_away['american'])}")

    if ml_home.get("american") is not None:
        lines.append(f"Home moneyline: {_fmt_odds_value(ml_home['american'])}")

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

How to read "Home spread": it is the home team's own line, signed for the
home team. A negative number means the home team is favored by that many
points; a positive number means the home team is the underdog by that many
points. The away team's line is always the exact opposite sign of the same
number.

Example: "Home spread: -28" means the home team is favored by 28 points.
The home team covers ONLY if it wins by MORE than 28 points (e.g. Alabama
-28 covers only by winning 29+ points -- winning by exactly 28 is a push,
winning by 1-27 points or losing outright means the away team covers
instead, at +28).

Do not default to picking the favorite to cover just because it is
favored. Weigh current-season performance to judge whether the expected
margin is actually larger or smaller than the number above -- the
underdog covers any time the actual margin comes in under that number,
including if the underdog wins outright.

Using ONLY statistics, injuries, roster status, and performance from the
{season} season, determine:

1. Who wins outright.
2. Who covers the spread (only if a spread is posted above; otherwise null).
3. A confidence score from 1-100 for the outright winner pick.
4. A confidence score from 0-100 for the ATS pick.
5. A five-sentence explanation of the reasoning.

Ignore previous seasons, franchise history, and reputation -- current-season
data only.

Return ONLY a valid JSON object with exactly these keys:
- "winner": string
- "confidence": integer 1-100
- "ats_pick": string or null
- "ats_confidence": integer 0-100
- "analysis": string with exactly five sentences

If no spread is posted, set "ats_pick" to JSON null and "ats_confidence" to 0.
Do not put the word "null" in quotes.

Example shape:
{{"winner": "Team A", "confidence": 72, "ats_pick": "Team B +3.5", "ats_confidence": 64, "analysis": "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five."}}"""


#---------------------------------------------------------------------------
# Response normalization
#---------------------------------------------------------------------------

def _as_int(value, default):
    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        cleaned = value.strip().replace("%", "")
        try:
            return int(float(cleaned))
        except ValueError:
            return default

    return default


def _clean_nullable_string(value):
    if value is None:
        return None

    s = str(value).strip()
    if not s or s.lower() in {"null", "none", "n/a", "na"}:
        return None

    return s


def _normalize_prediction(raw):
    """Forces the prediction into the expected shape with both confidence fields."""
    if not isinstance(raw, dict):
        raise ValueError("Gemini response was not a JSON object")

    winner = _clean_nullable_string(raw.get("winner"))
    if not winner:
        raise ValueError("Gemini response missing winner")

    confidence = max(1, min(100, _as_int(raw.get("confidence"), 50)))

    ats_pick = _clean_nullable_string(raw.get("ats_pick"))
    if ats_pick is None:
        ats_confidence = 0
    else:
        ats_confidence = max(1, min(100, _as_int(raw.get("ats_confidence"), 50)))

    analysis = str(raw.get("analysis", "")).strip()

    return {
        "winner": winner,
        "confidence": confidence,
        "ats_pick": ats_pick,
        "ats_confidence": ats_confidence,
        "analysis": analysis,
    }


#---------------------------------------------------------------------------
# Gemini API call
#---------------------------------------------------------------------------

def _call_gemini(prompt, gemini_key):
    last_err = None

    for attempt in range(MAX_RETRIES):
        _rate_limiter.wait()

        try:
            resp = requests.post(
                GEMINI_API_URL,
                params={"key": gemini_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.4,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=GEMINI_TIMEOUT,
            )
        except requests.RequestException as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break

            log(
                f"  Gemini request error -- retrying in {RETRY_DELAY_SECONDS:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            time.sleep(RETRY_DELAY_SECONDS)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            # Daily quota exhausted -> retrying today is pointless. Stop now;
            # the cache keeps progress and the next run finishes the slate.
            if resp.status_code == 429 and "RequestsPerDay" in resp.text:
                _daily_quota_exhausted.set()
                raise DailyQuotaExceeded(
                    "daily request quota (RPD) exhausted -- remaining games "
                    "will be picked up on a future run"
                )

            last_err = requests.exceptions.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}",
                response=resp,
            )

            if attempt == MAX_RETRIES - 1:
                break

            delay = RETRY_DELAY_SECONDS
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    delay = max(RETRY_DELAY_SECONDS, float(retry_after))
                except ValueError:
                    pass

            log(
                f"  Gemini {resp.status_code} -- retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(delay)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise requests.exceptions.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}: {resp.text[:300]}",
                response=resp,
            ) from e

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return _normalize_prediction(parsed)
        except Exception as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break

            log(
                f"  Gemini returned an unusable response -- retrying in "
                f"{RETRY_DELAY_SECONDS:.0f}s (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            time.sleep(RETRY_DELAY_SECONDS)
            continue

    if last_err is not None:
        raise last_err

    raise RuntimeError("Gemini call failed for unknown reason")


#---------------------------------------------------------------------------
# Public entry point
#---------------------------------------------------------------------------

def attach_gemini_predictions(games, sport, season, week, gemini_key):
    """
    Mutates each dict in `games` by adding a "gemini_prediction" key for any
    game with at least one posted line.

    Reuses cached predictions when the hash hasn't changed; calls Gemini
    sequentially (~1/min) for new/changed games, then saves the cache.
    If the daily quota runs out mid-slate, stops calling and leaves the
    rest for a future run.
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

    log(
        f"Gemini predictions: calling for {len(to_call)} game(s) with new/changed odds "
        f"on {GEMINI_MODEL} (~{60.0 / MIN_CALL_INTERVAL:.0f}/min, so this may take a while)..."
    )

    called, failed, skipped = 0, 0, 0

    for g, h, prompt in to_call:
        label = f"{g['away_team']} @ {g['home_team']}"

        if _daily_quota_exhausted.is_set():
            skipped += 1
            log(f"  Skipping {label} -- daily quota exhausted; will call on a future run.")
            continue

        try:
            result = _call_gemini(prompt, gemini_key)
        except DailyQuotaExceeded as e:
            skipped += 1
            log(f"  Gemini daily quota hit at {label}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 -- one bad game shouldn't kill the build
            failed += 1
            log(f"  Gemini call failed for {label}: {e}")
            continue

        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["odds_hash"] = h
        result["model"] = GEMINI_MODEL
        g["gemini_prediction"] = result
        cache[h] = result
        called += 1

        # Save incrementally so a long slow run doesn't lose progress.
        if called % 5 == 0:
            _save_cache(cache)

    log(f"Gemini predictions: {called} succeeded, {failed} failed, {skipped} skipped (daily quota).")

    if called:
        _save_cache(cache)
