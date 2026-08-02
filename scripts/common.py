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

    _log_odds_breakdown(rows)
    return rows


def _log_odds_breakdown(rows):
    """Print a (sportsbook, market_type) row-count table to stderr.

    This is the fastest way to tell "SharpAPI genuinely hasn't posted these
    lines yet" apart from "we're getting rows back but dropping them during
    matching" -- if a combination is missing here, it never reached us in
    the first place, so nothing downstream can be at fault.
    """
    from collections import Counter
    counts = Counter((r.get("sportsbook"), r.get("market_type")) for r in rows)
    if not counts:
        log("  SharpAPI returned 0 odds rows total")
        return
    log(f"  odds rows by (sportsbook, market): {dict(counts)}")
    for book in ("draftkings", "fanduel"):
        for market in ("spread", "moneyline"):
            if counts.get((book, market), 0) == 0:
                log(f"  NOTE: 0 rows for ({book}, {market}) -- SharpAPI hasn't posted these yet, "
                    f"or (book, market) label differs from what we expect. Not a matching bug if "
                    f"the count is 0 here; something to check upstream if it's nonzero but games "
                    f"still show no data for it.")


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
#
# Tolerant of: a unicode minus sign (some feeds use \u2212 instead of a
# plain hyphen), extra/odd whitespace, and "PK"/"PICK" for a pick'em game
# (treated as a 0 line) since none of those are guaranteed to show up but
# cost nothing to handle if they do.
_SPREAD_SELECTION_RE = re.compile(r"^(.*?)\s+([+-]\d+(?:\.\d+)?)$")
_SPREAD_PICKEM_RE = re.compile(r"^(.*?)\s+(?:PK|PICK)$", re.IGNORECASE)


def _parse_selection(selection, market):
    """Split a selection string into (team_name_part, line_or_None)."""
    if market == "spread":
        cleaned = selection.strip().replace("\u2212", "-")  # unicode minus -> ASCII hyphen
        m = _SPREAD_SELECTION_RE.match(cleaned)
        if m:
            return m.group(1), float(m.group(2))
        m = _SPREAD_PICKEM_RE.match(cleaned)
        if m:
            return m.group(1), 0.0
        log(f"  WARNING: couldn't parse spread selection {selection!r} -- treating as team name only, no line")
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

    disqualifying = {
        # Original
        "state", "tech", "international", "commonwealth",

        # Directional & Regional
        "western", "eastern", "northern", "southern", "central",
        "north", "south", "east", "west",
        "northeastern", "southeastern", "northwestern", "southwestern",
        "middle", "coastal", "atlantic", "pacific", "gulf",

        # Institutional Types
        "polytechnic", "poly", "institute", "military", "academy",
        "college", "valley", "city",

        # Denominational / Religious Affiliations
        "christian", "methodist", "baptist", "presbyterian", "lutheran", "wesleyan",

        # Specific Campus Modifiers
        "bluff", "pine",
    }

    shorter, longer = (target_words, candidate_words) if len(target_words) <= len(candidate_words) else (candidate_words, target_words)
    if shorter and shorter.issubset(longer):
        extra = longer - shorter
        # Words that turn one team/school into a genuinely different one,
        # not a mascot: "State" (Ohio vs Ohio State), "Tech" (Texas vs
        # Texas Tech), and short 1-2 letter tokens, which catch things like
        # the "A"/"M" split out of "A&M" (Texas vs Texas A&M) or a state
        # code like "OH" (Miami vs Miami (OH)). Mascots -- however many
        # words, e.g. "Horned Frogs", "Fighting Irish" -- are never this short.
        if not any(w in disqualifying or len(w) <= 2 for w in extra):
            return True
        # Falls through to the ratio check below only if not disqualified;
        # a disqualified containment match (e.g. Washington vs Washington
        # State) must not be rescued by the fuzzy ratio either -- see the
        # symmetric-difference guard just below, which covers this case
        # since "state" would show up there too.

    # Non-subset comparison (different word sets entirely, e.g. a genuine
    # spelling variation). A high raw character-overlap ratio can still
    # false-positive here purely because two DIFFERENT schools share a
    # word, e.g. "Washington State" vs "Washington Huskies" -- both contain
    # "washington" and are similar length, so the ratio alone clears 0.72.
    # If the words that AREN'T shared between the two sides include a
    # disqualifying word, that's a strong signal they're different schools
    # no matter how high the character-overlap ratio comes out -- skip the
    # ratio fallback entirely in that case.
    symmetric_diff = target_words ^ candidate_words
    if any(w in disqualifying for w in symmetric_diff):
        return False

    a = " ".join(sorted(target_words))
    b = " ".join(sorted(candidate_words))
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.72


def match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims):
    cache_key = (home_team, away_team)
    if cache_key in team_cache:
        return team_cache[cache_key]

    home_norm = _normalize(home_team)
    away_norm = _normalize(away_team)

    # 1. Match candidates checking both straight and flipped home/away
    candidates = []
    for r in odds_rows:
        r_home = r.get("home_team", "")
        r_away = r.get("away_team", "")
        
        # Check standard orientation
        straight_match = _fuzzy_team(home_norm, r_home) and _fuzzy_team(away_norm, r_away)
        # Check flipped neutral-site orientation
        flipped_match = _fuzzy_team(home_norm, r_away) and _fuzzy_team(away_norm, r_home)
        
        if straight_match or flipped_match:
            candidates.append(r)

    result = {"draftkings": {"spread": {}, "moneyline": {}}, "fanduel": {"spread": {}, "moneyline": {}}}
    rejected = 0

    for row in candidates:
        row_id = row.get("id")
        if row_id is not None:
            prior_claim = row_claims.get(row_id)
            if prior_claim is not None and prior_claim != cache_key:
                log(f"  WARNING: odds row {row_id} matched both {prior_claim} and {cache_key} -- dropping")
                rejected += 1
                continue
            row_claims[row_id] = cache_key

        book = row.get("sportsbook")
        market = row.get("market_type")
        if book not in result or market not in ("spread", "moneyline"):
            continue
            
        team_part, line_val = _parse_selection(row.get("selection", ""), market)
        
        # Determine side dynamically
        side = "home" if _fuzzy_team(home_norm, team_part) else (
            "away" if _fuzzy_team(away_norm, team_part) else None
        )
        if side is None:
            continue
            
        # Invert spread sign if the sportsbook's home team is flipped relative to ESPN
        r_home = row.get("home_team", "")
        is_flipped = _fuzzy_team(away_norm, r_home)
        
        final_line = line_val
        if market == "spread" and line_val is not None and is_flipped:
            final_line = -line_val  # Flip spread direction to match ESPN's home team

        result[book][market][side] = {
            "line": final_line,
            "american": row.get("odds_american"),
        }

    team_cache[cache_key] = result
    return result
