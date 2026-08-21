"""
Gemini prediction helper, shared by build_ncaaf_dashboard.py (CFB),
build_nfl_dashboard.py (NFL), build_mlb_dashboard.py (MLB),
build_nba_dashboard.py (NBA), and build_ncaamb_dashboard.py (NCAAMB).

For every game that has at least one posted line (DraftKings or FanDuel,
spread or moneyline), asks Gemini for:
- a straight-up winner pick
- an ATS pick
- a 1-100 confidence score for the straight-up winner
- a 0-100 confidence score for the ATS pick
- a five-sentence explanation

Uses only current-season data.

Model fallback chain (tried top to bottom, falling back immediately
whenever a model reports itself rate-limited, over quota, or otherwise
unavailable -- see GEMINI_MODELS / _call_gemini):
    1. gemini-3.5-flash-lite
    2. gemini-3.1-flash-lite
    3. gemini-2.5-flash-lite
    4. gemini-2.0-flash-lite

Rate limiting:
- 1 API call every 10 seconds (6/minute)
- non-rate-limit errors (network errors, 5xx, an unusable response) retry
  the SAME model, waiting a full minute between attempts
- a rate limit / quota / not-found response (4xx) on any given model is
  NOT retried -- it falls straight through to the next model in the
  chain instead
- games are processed sequentially, one at a time
- if every model in the chain is rate-limited/unavailable for a given
  call, the run stops calling immediately; uncalled games are picked up
  on the next run (the cache preserves progress)

Caching:
Predictions are cached in data/gemini_predictions_cache.json, keyed by a
hash of (fallback-chain version, sport, season, week, matchup, DK odds,
FD odds) -- NOT by which specific model in the chain actually answered a
given call, so a game that happened to fall back to gemini-2.5-flash-lite
one run still hits the cache normally on the next run even if
gemini-3.5-flash-lite is healthy again by then. Changing the chain itself
(GEMINI_MODELS) invalidates every cached prediction, same as changing
models used to under the old single-model scheme.

Weekly full-refresh schedule:
- NFL: every Wednesday (UTC), the cache is ignored entirely for every
  NFL game that HASN'T already started -- each gets a fresh call
  regardless of whether its odds hash matches an existing cache entry.
  Games already underway or final are left completely alone (see
  skip_ids below), so a Wednesday refresh mid-week never touches a
  game that's already been decided.
- CFB: same, but on Thursday (UTC) instead of Wednesday.
- MLB/NBA/NCAAMB: no scheduled full-refresh day -- caching behaves as
  described above on every day of the week for these three.
Any day that isn't that sport's refresh day, caching works as described
above (reuse whenever the odds hash matches).

Env var required: GEMINI_KEY.
If missing, predictions are skipped entirely.
"""

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests

from common import log


# Tried top to bottom; falls back to the next entry the instant a model
# reports itself rate-limited, over quota, or unavailable (any 4xx --
# including a 404 for a model Google has since discontinued, e.g.
# gemini-2.0-flash-lite was retired on June 1, 2026, so that last rung
# may always 404 depending on when this runs; it's kept in the chain
# anyway as a last-ditch attempt in case it's ever restored or the
# account still has grandfathered access).
GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
)
GEMINI_TIMEOUT = 30

# One request every 10 seconds (6/minute).
MIN_CALL_INTERVAL = 10.0

# Retries (same model, non-rate-limit errors only) wait a full minute.
RETRY_DELAY_SECONDS = 60.0
MAX_RETRIES = 5

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "data", "gemini_predictions_cache.json"))


class DailyQuotaExceeded(RuntimeError):
    """Every model in GEMINI_MODELS is rate-limited, over quota, or
    unavailable right now; retrying today is pointless."""


class _ModelUnavailable(Exception):
    """Raised internally when a single model in the fallback chain
    returns a 4xx response -- rate limited (429), daily quota exhausted,
    the model name doesn't exist / has been retired (404), or any other
    client-side rejection. Signals _call_gemini to move on to the next
    model in GEMINI_MODELS immediately rather than burning retries on a
    model that just told us no."""


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


def _odds_hash(sport, season, week, away_team, home_team, odds, away_pitcher=None, home_pitcher=None):
    """
    Cache key that changes iff the fallback-chain version, matchup, its
    odds, or (for MLB) the probable starting pitchers change. Using the
    whole GEMINI_MODELS tuple (rather than one specific model) as the
    version marker means a game that fell back to a lower model on a
    previous run still hits the cache normally -- only actually changing
    the chain (adding/removing/reordering a model) invalidates the cache,
    same as switching models did under the old single-model scheme. For
    MLB, `week` is actually the game's date (see attach_gemini_predictions)
    so two different days' games between the same two teams still hash
    differently, and a late pitcher swap triggers a fresh call instead of
    serving a stale cached prediction.
    """
    dk_spread = (odds.get("draftkings", {}).get("spread", {}).get("home") or {})
    dk_ml_away = (odds.get("draftkings", {}).get("moneyline", {}).get("away") or {})
    dk_ml_home = (odds.get("draftkings", {}).get("moneyline", {}).get("home") or {})

    fd_spread = (odds.get("fanduel", {}).get("spread", {}).get("home") or {})
    fd_ml_away = (odds.get("fanduel", {}).get("moneyline", {}).get("away") or {})
    fd_ml_home = (odds.get("fanduel", {}).get("moneyline", {}).get("home") or {})

    key_material = "|".join(str(x) for x in [
        "|".join(GEMINI_MODELS),
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
        "SP",
        away_pitcher,
        home_pitcher,
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



# Sports whose builder passes the game's actual calendar date as `week`
# (day-based boards -- MLB/NBA/NCAAMB all play/schedule day-to-day rather
# than in a real "week N" sense), as opposed to CFB/NFL's real week
# numbers.
DATE_BASED_SPORTS = {"mlb", "nba", "ncaamb"}


def _build_prompt(sport, season, week, away_team, home_team, odds, away_pitcher=None, home_pitcher=None):
    league_label = {
        "nfl": "NFL", "mlb": "MLB", "nba": "NBA", "ncaamb": "college basketball",
    }.get(sport, "college football")

    # For date-based sports, `week` is the game's actual calendar date (see
    # attach_gemini_predictions) -- these play/schedule day-to-day, so
    # "week 5" is meaningless; the model needs the specific date to know
    # which of possibly several games between these two teams this is.
    period_label = f"on {week}" if sport in DATE_BASED_SPORTS else f"(week {week})"

    margin_unit = "runs" if sport == "mlb" else "points"

    pitcher_block = ""
    if sport == "mlb" and (away_pitcher or home_pitcher):
        pitcher_block = f"""
Probable Starting Pitchers:
Away: {away_pitcher or 'Not yet announced'}
Home: {home_pitcher or 'Not yet announced'}
"""

    dk = odds.get("draftkings", {})
    fd = odds.get("fanduel", {})

    return f"""You are analyzing an upcoming {season} {league_label} game {period_label}.

Away Team: {away_team}
Home Team: {home_team}
{pitcher_block}
DraftKings:
{_fmt_book(dk)}

FanDuel:
{_fmt_book(fd)}

How to read "Home spread": it is the home team's own line, signed for the
home team (for MLB this is the run line, almost always +/-1.5 runs). A
negative number means the home team is favored by that many {margin_unit};
a positive number means the home team is the underdog by that many
{margin_unit}. The away team's line is always the exact opposite sign of
the same number.

Example: "Home spread: -1.5" means the home team is favored by 1.5 {margin_unit}.
The home team covers ONLY if it wins by MORE than that margin -- winning by
exactly that margin is a push (rare for a MLB run line, since half-run
lines can't push), winning by less or losing outright means the away team
covers instead, at the positive number.

Do not default to picking the favorite to cover just because it is
favored. Weigh current-season performance to judge whether the expected
margin is actually larger or smaller than the number above -- the
underdog covers any time the actual margin comes in under that number,
including if the underdog wins outright.

Using ONLY statistics, injuries, roster status, and performance from the
{season} season, determine:

1. Who wins outright.
2. Who covers the spread (only if a spread is posted above; otherwise
   null). Answer with the exact team name only, written exactly as it
   appears above under "Away Team"/"Home Team" -- do not append the line
   number or a "+"/"-" sign to it.
3. A confidence score from 1-100 for the outright winner pick.
4. A confidence score from 0-100 for the ATS pick.
5. A five-sentence explanation of the reasoning.
6. What percentage of bets (or bet count) is going to each side of the
   spread and each side of the moneyline, and which direction the spread
   and moneyline have moved from their opening numbers -- in exactly two
   sentences.

Ignore previous seasons, franchise history, and reputation -- current-season
data only.

Return ONLY a valid JSON object with exactly these keys:
- "winner": string
- "confidence": integer 1-100
- "ats_pick": string or null -- the exact team name only (as written
  above under "Away Team"/"Home Team"), never the team name plus the
  line number
- "ats_confidence": integer 0-100
- "analysis": string with exactly five sentences
- "splits_and_direction": string with exactly two sentences covering the
  spread/moneyline betting percentage splits between the two teams and
  which direction each line has moved

If no spread is posted, set "ats_pick" to JSON null and "ats_confidence" to 0.
Do not put the word "null" in quotes.
If betting-splits data is not available to you, give your best estimate
based on the odds and public tendencies and say so briefly rather than
leaving the field empty.

Example shape:
{{"winner": "Team A", "confidence": 72, "ats_pick": "Team B", "ats_confidence": 64, "analysis": "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five.", "splits_and_direction": "Sentence one about the split. Sentence two about line direction."}}"""


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


# Strips a trailing spread-line-looking suffix ("+3.5", "-1.5", "+7") off
# an ats_pick string. The prompt now tells Gemini to return the bare team
# name, but this is a defensive net in case a response ever still comes
# back with the line attached -- accuracy grading (both here and on the
# front end) compares ats_pick against a plain team name, so a trailing
# number would silently make every ATS grade wrong.
_ATS_PICK_LINE_SUFFIX_RE = re.compile(r"\s*[+-]\d+(?:\.\d+)?\s*$")


def _strip_ats_line_suffix(pick):
    if pick is None:
        return None
    return _ATS_PICK_LINE_SUFFIX_RE.sub("", pick).strip() or None


def _normalize_prediction(raw):
    """Forces the prediction into the expected shape with both confidence fields."""
    if not isinstance(raw, dict):
        raise ValueError("Gemini response was not a JSON object")

    winner = _clean_nullable_string(raw.get("winner"))
    if not winner:
        raise ValueError("Gemini response missing winner")

    confidence = max(1, min(100, _as_int(raw.get("confidence"), 50)))

    ats_pick = _strip_ats_line_suffix(_clean_nullable_string(raw.get("ats_pick")))
    if ats_pick is None:
        ats_confidence = 0
    else:
        ats_confidence = max(1, min(100, _as_int(raw.get("ats_confidence"), 50)))

    analysis = str(raw.get("analysis", "")).strip()
    splits_and_direction = str(raw.get("splits_and_direction", "")).strip()

    display_parts = [f"Summary: {analysis}"]
    if splits_and_direction:
        display_parts.append(f"Splits and Direction: {splits_and_direction}")
    display_text = "\n\n".join(display_parts)

    return {
        "winner": winner,
        "confidence": confidence,
        "ats_pick": ats_pick,
        "ats_confidence": ats_confidence,
        "analysis": analysis,
        "splits_and_direction": splits_and_direction,
        "display_text": display_text,
    }


#---------------------------------------------------------------------------
# Gemini API call
#---------------------------------------------------------------------------

def _call_gemini_single(prompt, gemini_key, model):
    """Try exactly one model. Raises _ModelUnavailable immediately (no
    retry) on any 4xx response -- rate limited, quota exhausted, or the
    model itself doesn't exist/was retired -- so the caller can fall
    back to the next model in the chain without burning this model's
    retry budget on a request that's never going to succeed. Network
    errors, 5xx, and unusable responses still retry the SAME model up to
    MAX_RETRIES times, same as before."""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last_err = None

    for attempt in range(MAX_RETRIES):
        _rate_limiter.wait()

        try:
            resp = requests.post(
                api_url,
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
                f"  {model}: request error -- retrying in {RETRY_DELAY_SECONDS:.0f}s "
                f"(attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            time.sleep(RETRY_DELAY_SECONDS)
            continue

        if 400 <= resp.status_code < 500:
            # Rate limited (429, RPM or RPD), some other quota/permission
            # rejection, or a 404 for a model name that no longer exists
            # (e.g. gemini-2.0-flash-lite, retired June 2026) -- none of
            # these get better by retrying THIS model, so bail out
            # immediately and let _call_gemini() move on to the next one.
            raise _ModelUnavailable(f"{resp.status_code} {resp.reason}: {resp.text[:200]}")

        if resp.status_code >= 500:
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
                f"  {model}: {resp.status_code} -- retrying in {delay:.0f}s "
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
                f"  {model}: returned an unusable response -- retrying in "
                f"{RETRY_DELAY_SECONDS:.0f}s (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            time.sleep(RETRY_DELAY_SECONDS)
            continue

    if last_err is not None:
        raise last_err

    raise RuntimeError(f"Gemini call to {model} failed for unknown reason")


def _call_gemini(prompt, gemini_key):
    """Tries each model in GEMINI_MODELS in order, falling back to the
    next one immediately whenever a model raises _ModelUnavailable (see
    _call_gemini_single). Returns (prediction_dict, model_used). Only
    once EVERY model in the chain has been tried and rejected does this
    raise DailyQuotaExceeded -- at that point there's genuinely nothing
    left to fall back to for the rest of today's run."""
    last_unavailable = None

    for model in GEMINI_MODELS:
        try:
            result = _call_gemini_single(prompt, gemini_key, model)
            return result, model
        except _ModelUnavailable as e:
            last_unavailable = e
            log(f"  {model} unavailable ({e}) -- falling back to next model in the chain.")
            continue

    _daily_quota_exhausted.set()
    raise DailyQuotaExceeded(
        f"every model in the fallback chain ({', '.join(GEMINI_MODELS)}) is rate-limited, "
        f"over quota, or unavailable right now -- remaining games will be picked up on a "
        f"future run (last error: {last_unavailable})"
    )


#---------------------------------------------------------------------------
# Public entry point
#---------------------------------------------------------------------------

def attach_gemini_predictions(games, sport, season, week, gemini_key, skip_ids=None):
    """
    Mutates each dict in `games` by adding a "gemini_prediction" key for any
    game with at least one posted line.

    Reuses cached predictions when the hash hasn't changed; calls Gemini
    sequentially (~1/min per model, falling back through GEMINI_MODELS on
    a rate limit -- see _call_gemini) for new/changed games, then saves
    the cache. If every model in the chain is rate-limited/unavailable
    mid-slate, stops calling and leaves the rest for a future run.

    skip_ids, if given, is a set of game ids (as strings) to leave
    completely untouched -- no cache lookup, no Gemini call, no
    force-refresh override even on that sport's scheduled refresh day.
    The caller is expected to have already set g["gemini_prediction"] on
    these (typically to whatever was frozen from the previous build)
    before calling this, for a game that's already started: its pregame
    line is frozen, so there's nothing new to grade the prediction
    against, and a moving in-game line shouldn't trigger a fresh call
    anyway. This is also exactly what makes the weekly full-refresh below
    safe -- it forces a refresh for NFL/CFB's not-yet-started games only,
    since already-started/finished games are always in skip_ids by the
    time this function sees them.
    """
    if not gemini_key:
        log("No GEMINI_KEY set -- skipping Gemini predictions.")
        return

    skip_ids = skip_ids or set()

    # Weekly full-refresh, one sport at a time so the two busiest boards
    # each get a clean slate on a different day rather than piling both
    # onto the same run: NFL every Wednesday (UTC), CFB every Thursday
    # (UTC). Every other sport, and every other day for NFL/CFB, behaves
    # as before -- reuse a cached prediction whenever the odds hash
    # hasn't changed. Games that are already underway or final never see
    # this override regardless of sport/day, since those game ids are
    # already excluded via skip_ids above.
    today_utc = datetime.now(timezone.utc).weekday()  # Mon=0 ... Wed=2, Thu=3
    force_refresh = (sport == "nfl" and today_utc == 2) or (sport == "cfb" and today_utc == 3)

    cache = _load_cache()
    to_call = []

    for g in games:
        if str(g.get("id")) in skip_ids:
            continue

        odds = g.get("odds") or {}

        has_odds = any(
            (odds.get(book, {}).get("spread") or odds.get(book, {}).get("moneyline"))
            for book in ("draftkings", "fanduel")
        )

        if not has_odds:
            continue

        away_pitcher = g.get("away_pitcher") if sport == "mlb" else None
        home_pitcher = g.get("home_pitcher") if sport == "mlb" else None

        h = _odds_hash(sport, season, week, g["away_team"], g["home_team"], odds,
                        away_pitcher=away_pitcher, home_pitcher=home_pitcher)
        cached = cache.get(h)

        if cached and not force_refresh:
            g["gemini_prediction"] = cached
            continue

        prompt = _build_prompt(sport, season, week, g["away_team"], g["home_team"], odds,
                                away_pitcher=away_pitcher, home_pitcher=home_pitcher)
        to_call.append((g, h, prompt))

    if not to_call:
        log("Gemini predictions: nothing new to call (all cached, or no odds posted yet).")
        return

    if force_refresh:
        refresh_day = "Wednesday" if sport == "nfl" else "Thursday"
        log(f"Gemini predictions: today is {refresh_day} -- forcing a refresh for every "
            f"not-yet-started {sport.upper()} game with posted odds, cache or not.")

    log(
        f"Gemini predictions: calling for {len(to_call)} game(s) with new/changed odds, "
        f"starting with {GEMINI_MODELS[0]} and falling back through "
        f"{', '.join(GEMINI_MODELS[1:])} if rate-limited "
        f"(~{60.0 / MIN_CALL_INTERVAL:.0f}/min per model, so this may take a while)..."
    )

    called, failed, skipped = 0, 0, 0

    for g, h, prompt in to_call:
        label = f"{g['away_team']} @ {g['home_team']}"

        if _daily_quota_exhausted.is_set():
            skipped += 1
            log(f"  Skipping {label} -- every model in the fallback chain is exhausted; will call on a future run.")
            continue

        try:
            result, model_used = _call_gemini(prompt, gemini_key)
        except DailyQuotaExceeded as e:
            skipped += 1
            log(f"  Gemini fallback chain exhausted at {label}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 -- one bad game shouldn't kill the build
            failed += 1
            log(f"  Gemini call failed for {label}: {e}")
            continue

        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["odds_hash"] = h
        result["model"] = model_used
        g["gemini_prediction"] = result
        cache[h] = result
        called += 1

        # Save incrementally so a long slow run doesn't lose progress.
        if called % 5 == 0:
            _save_cache(cache)

    log(f"Gemini predictions: {called} succeeded, {failed} failed, {skipped} skipped (fallback chain exhausted).")

    if called:
        _save_cache(cache)
