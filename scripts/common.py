"""
Shared utilities for the sports betting dashboards (CFB + NFL).

Holds the SharpAPI odds fetching/matching logic and the day/time-slot
bucketing logic, so both scripts/build_dashboard.py (CFB) and
scripts/build_nfl_dashboard.py (NFL) use the exact same, tested matching
code rather than two copies that can drift out of sync.
"""

import difflib
import re
import sys
import time
from datetime import datetime

import requests

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1"
REQUEST_TIMEOUT = 20
SHARPAPI_PAGE_LIMIT = 200  # ask for big pages; we still follow pagination

# Timezone used to bucket games into Morning/Noon/Afternoon/Evening/Late
# Night windows. Central, per your preference. Change to e.g.
# "America/New_York" if you'd rather bucket by Eastern.
DISPLAY_TIMEZONE = "America/Chicago"

# Slot boundaries are the hour (in DISPLAY_TIMEZONE) each window ends at.
# A game exactly on a boundary falls into the earlier window.
TIME_SLOT_BOUNDARIES = [
    ("Morning", 12),
    ("Noon", 15),
    ("Afternoon", 18),
    ("Evening", 21),
    ("Late Night", 24),
]
TIME_SLOT_ORDER = [name for name, _ in TIME_SLOT_BOUNDARIES] + ["Time TBD"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def time_slot_for(local_dt, is_tbd):
    """Bucket a timezone-aware local datetime into a named kickoff window."""
    if is_tbd or local_dt is None:
        return "Time TBD"
    hour_frac = local_dt.hour + local_dt.minute / 60
    for name, upper_bound in TIME_SLOT_BOUNDARIES:
        if hour_frac < upper_bound:
            return name
    return "Late Night"


# ---------------------------------------------------------------------------
# SharpAPI calls
# ---------------------------------------------------------------------------

def fetch_all_odds(sharp_key, league, sportsbooks=("draftkings", "fanduel"),
                    markets=("spread", "moneyline")):
    """Pull every odds row for the given league/books/markets, following pagination."""
    rows = []
    offset = 0
    while True:
        params = {
            "league": league,
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


# SharpAPI does not return the spread number as its own field. For
# market_type "spread", the line is embedded in the `selection` string
# itself, e.g. "Georgia Bulldogs -7" or "Kansas City Chiefs -3.5".
# Moneyline `selection` is just the team name with no trailing number.
# See https://sharpapi.io/odds/ncaaf (sample response) and the
# opportunities/ev example in the SharpAPI quickstart docs.
_SPREAD_SELECTION_RE = re.compile(r"^(.*?)\s([+-]\d+(?:\.\d+)?)$")


def _parse_selection(selection, market):
    """Split a selection string into (team_name_part, line_or_None)."""
    if market == "spread":
        m = _SPREAD_SELECTION_RE.match(selection.strip())
        if m:
            return m.group(1), float(m.group(2))
    return selection, None


def _tokens(name):
    return set(re.findall(r"[a-z0-9]+", _normalize(name)))


def _fuzzy_team(normalized_target, candidate_raw):
    """
    True if `candidate_raw` (a SharpAPI team string) plausibly refers to the
    same team as `normalized_target` (an already-normalized target team string).

    Compares whole words, not raw substrings. A raw substring check (e.g.
    "st" in "state") lets short tokens on either side false-match inside
    unrelated longer names -- that's what let one SharpAPI odds row get
    attached to two different games in testing. Whole-word containment
    fixes that while still matching short real names like "USC" or "TCU"
    against their fuller SharpAPI form ("USC Trojans", "TCU Horned Frogs").
    """
    if not candidate_raw:
        return False
    target_words = set(re.findall(r"[a-z0-9]+", normalized_target))
    candidate_words = _tokens(candidate_raw)
    if not target_words or not candidate_words:
        return False
    shorter, longer = (target_words, candidate_words) if len(target_words) <= len(candidate_words) else (candidate_words, target_words)
    if shorter and shorter.issubset(longer):
        extra = longer - shorter
        # Words that turn one team/school into a genuinely different one,
        # not a mascot: "State" (Ohio vs Ohio State), "Tech" (Texas vs
        # Texas Tech), and short 1-2 letter tokens, which catch things like
        # the "A"/"M" split out of "A&M" (Texas vs Texas A&M) or a state
        # code like "OH" (Miami vs Miami (OH)). Mascots -- however many
        # words, e.g. "Horned Frogs", "Fighting Irish" -- are never this short.
        disqualifying = {"state", "tech", "international", "commonwealth"}
        if not any(w in disqualifying or len(w) <= 2 for w in extra):
            return True
    a = " ".join(sorted(target_words))
    b = " ".join(sorted(candidate_words))
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.72


def match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims):
    """
    SharpAPI team names don't always match exactly (mascots, abbreviations,
    etc.), so fuzzy-match once per unique team pair and cache the result.

    `row_claims` is a shared {sharpapi_row_id: (home_team, away_team)} dict
    used across ALL games in this run. If a given SharpAPI row ever gets
    claimed by two different (home, away) pairs, that's proof the matching
    was too loose for that row (a real odds row belongs to exactly one
    game) -- we drop it from both instead of silently duplicating it.
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
    rejected = 0

    for row in candidates:
        row_id = row.get("id")
        if row_id is not None:
            prior_claim = row_claims.get(row_id)
            if prior_claim is not None and prior_claim != cache_key:
                # Same SharpAPI row already attached to a different game --
                # the match was ambiguous for at least one of them. Don't
                # trust it for either.
                log(f"  WARNING: odds row {row_id} matched both {prior_claim} and "
                    f"{cache_key} -- dropping as ambiguous")
                rejected += 1
                continue
            row_claims[row_id] = cache_key

        book = row.get("sportsbook")
        market = row.get("market_type")
        if book not in result or market not in ("spread", "moneyline"):
            continue
        team_part, line_val = _parse_selection(row.get("selection", ""), market)
        side = "home" if _fuzzy_team(home_norm, team_part) else (
            "away" if _fuzzy_team(away_norm, team_part) else None
        )
        if side is None:
            continue
        result[book][market][side] = {
            "line": line_val,
            "american": row.get("odds_american"),
        }

    if rejected:
        log(f"  {cache_key}: rejected {rejected} ambiguous odds row(s)")

    team_cache[cache_key] = result
    return result
