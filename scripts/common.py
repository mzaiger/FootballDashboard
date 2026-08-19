"""
Shared utilities for the sports betting dashboards (CFB + NFL).

Holds the SharpAPI odds fetching/matching logic and the day/time-slot
bucketing logic, so both scripts/build_ncaaf_dashboard.py (CFB) and
scripts/build_nfl_dashboard.py (NFL) use the exact same, tested matching
code rather than two copies that can drift out of sync.
"""

import difflib
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

SHARPAPI_BASE = "https://api.sharpapi.io/api/v1"
REQUEST_TIMEOUT = 20
SHARPAPI_PAGE_LIMIT = 200  # ask for big pages; we still follow pagination

# Timezone used to bucket games into Morning/Noon/Afternoon/Prime Time/Late
# Night windows. Central, per your preference. Change to e.g.
# "America/New_York" if you'd rather bucket by Eastern.
DISPLAY_TIMEZONE = "America/Chicago"

# Slot boundaries are the hour (in DISPLAY_TIMEZONE) each window ends at.
# A game exactly on a boundary falls into the earlier window.
TIME_SLOT_BOUNDARIES = [
    ("Morning", 11),
    ("Noon", 14),
    ("Afternoon", 17),
    ("Prime Time", 21),
    ("Late Night", 24),
]
TIME_SLOT_ORDER = [name for name, _ in TIME_SLOT_BOUNDARIES] + ["Time TBD"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def normalize_minmax(values):
    """Min-max scale a list of numbers (or None) to [0, 1] (0 = best/lowest,
    1 = worst/highest). Shared by CFB's slot-pick blend and NFL's matchup
    score blend so both "50% X + 50% Y" calculations behave the same way.

    None entries (e.g. no spread posted yet) are scored as 1.0 -- worst on
    that metric -- so a game only wins on the strength of its other metric.
    If every value is None, or every present value is identical, everyone
    gets 0.5 on that metric so it doesn't swing the pick.
    """
    present = [v for v in values if v is not None]
    if not present:
        return [0.5] * len(values)
    lo, hi = min(present), max(present)
    if hi == lo:
        return [0.5 if v is not None else 1.0 for v in values]
    return [1.0 if v is None else (v - lo) / (hi - lo) for v in values]


def assign_matchup_ranks(games):
    """Set `matchup_rank` on every game in `games` (1 = best/most marquee
    matchup, N = worst, where N is the number of games passed in), based
    on ascending `matchup_score` -- the existing 0-100 blended score (AP
    rank/spread/win-rank, computed upstream per sport) still decides the
    ORDER; this just turns that continuous score into a plain integer
    rank across the whole group, which is what the board displays now
    ("Matchup: 3" instead of a raw "Matchup Score: 41.2").

    Call this once per NATURAL grouping -- once for all of a day's games
    (MLB) or once for all of a week's games across every day (NFL/CFB) --
    not per time slot; the rank is meant to span the whole day/week (e.g.
    1-16 across a full 16-game MLB day), not just whichever slot a game
    happens to be in. Games with no score at all (matchup_score is None)
    get matchup_rank None and sort after every ranked game.
    """
    ranked = sorted(
        (g for g in games if g.get("matchup_score") is not None),
        key=lambda g: g["matchup_score"],
    )
    for i, g in enumerate(ranked):
        g["matchup_rank"] = i + 1
    for g in games:
        if g.get("matchup_score") is None:
            g["matchup_rank"] = None



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

def _rate_limit_wait_seconds(header_value, default=5, max_wait=120):
    """Turn a 429 response's X-RateLimit-Reset header into a sane number
    of seconds to sleep.

    Observed in practice: this header comes back as an absolute Unix
    epoch timestamp (seconds since 1970) for WHEN the limit resets, not a
    countdown duration -- treating it directly as "seconds to sleep" (an
    earlier version of this function did) meant a value like
    "1786919580" (a real epoch timestamp in 2026) got slept AS 1786919580
    SECONDS, i.e. over 55 years. Any header value implausibly large to be
    a real countdown (more than a day) is treated as an epoch timestamp
    and converted to "seconds from now" instead. The result is clamped to
    [1, max_wait] either way, so a clock-skew edge case or a genuinely
    long reset window can't hang the build for hours.
    """
    try:
        raw = int(header_value)
    except (TypeError, ValueError):
        return default
    if raw > 86400:  # implausible as a plain countdown -- treat as an epoch timestamp
        raw = raw - int(time.time())
    return max(1, min(raw, max_wait))


MAX_PAGE_RETRIES = 3


def fetch_all_odds(sharp_key, league, sportsbooks=("draftkings", "fanduel"),
                    markets=("spread", "moneyline"), date_from=None, date_to=None):
    """Pull every odds row for the given league/books/markets, following
    pagination.

    `date_from`/`date_to` (YYYY-MM-DD strings) are optional but recommended
    when the caller knows the exact date range it's building -- narrowing
    the request server-side means far fewer total rows/pages to page
    through, which is both faster and less exposed to any pagination edge
    case than asking for everything currently posted and filtering
    client-side.

    Each individual page request gets up to MAX_PAGE_RETRIES attempts
    (short backoff between them) before giving up -- SharpAPI has been
    observed occasionally returning a 400 on a cursor-based page request
    (e.g. right after a rate-limit wait, or on an already-large query) that
    succeeds on retry. If every attempt for a page fails, this returns
    whatever rows were already collected instead of raising -- one bad
    page for one day/request shouldn't crash the whole build; the caller
    (each build script now fetches one day at a time) just moves on to its
    next request. A 429 (rate limited) doesn't count against the retry
    budget at all -- it's an expected throttle, not a failure, so it's
    retried until it clears rather than giving up after 3 tries.
    """
    rows = []
    offset = 0
    cursor = None
    while True:
        params = {
            # SharpAPI's own example requests use the upper-case league
            # code ("MLB"); we'd been passing whatever casing each build
            # script's own `league="mlb"` call happened to use.
            # Uppercasing defensively here means it no longer matters.
            "league": league.upper(),
            "sportsbook": ",".join(sportsbooks),
            "market": ",".join(markets),
            "limit": SHARPAPI_PAGE_LIMIT,
        }
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if cursor:
            params["cursor"] = cursor
        else:
            params["offset"] = offset

        payload = None
        attempt = 0
        while payload is None and attempt < MAX_PAGE_RETRIES:
            attempt += 1
            try:
                resp = requests.get(
                    f"{SHARPAPI_BASE}/odds",
                    headers={"X-API-Key": sharp_key},
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                log(f"  WARNING: odds page request failed (attempt {attempt}/{MAX_PAGE_RETRIES}): {e}")
                if attempt < MAX_PAGE_RETRIES:
                    time.sleep(1.5 * attempt)
                continue

            if resp.status_code == 429:
                wait = _rate_limit_wait_seconds(resp.headers.get("X-RateLimit-Reset"))
                log(f"SharpAPI rate limited, sleeping {wait}s")
                time.sleep(wait)
                attempt -= 1  # doesn't count against the retry budget -- not a real failure
                continue

            try:
                resp.raise_for_status()
                payload = resp.json()
            except requests.RequestException as e:
                log(f"  WARNING: odds page fetch failed (attempt {attempt}/{MAX_PAGE_RETRIES}): "
                    f"{e} -- {resp.text[:300]}")
                if attempt < MAX_PAGE_RETRIES:
                    time.sleep(1.5 * attempt)

        if payload is None:
            log(f"  WARNING: giving up on this odds page after {MAX_PAGE_RETRIES} attempts -- "
                f"{len(rows)} row(s) already collected will still be used; any remaining pages "
                f"for this request are skipped rather than crashing the whole build.")
            break

        rows.extend(payload.get("data", []))

        # Pagination info is a TOP-LEVEL field of the response, a sibling
        # of "data" and "meta" -- NOT nested inside "meta" the way an
        # earlier version of this function read it
        # (payload["meta"]["pagination"]). Since "meta" never actually
        # contains a "pagination" key, that always evaluated to an empty
        # dict, so has_more was always falsy and this loop silently
        # stopped after the very first page (up to SHARPAPI_PAGE_LIMIT
        # rows) on every single call, no matter how many more rows were
        # actually waiting. For a market/day with more rows than one page
        # holds, whichever games' odds rows happened to sort past that
        # cutoff would just never get fetched -- indistinguishable from
        # "SharpAPI hasn't posted them yet" from the matching code's
        # point of view.
        pagination = payload.get("pagination", {})
        if not pagination.get("has_more"):
            break
        next_cursor = pagination.get("next_cursor")
        if next_cursor:
            cursor = next_cursor
        else:
            offset = pagination.get("next_offset", offset + SHARPAPI_PAGE_LIMIT)
        time.sleep(0.3)  # be polite to the free tier (12 req/min)

    _log_odds_breakdown(rows, sportsbooks, markets)
    return rows

    _log_odds_breakdown(rows, sportsbooks, markets)
    return rows


def _log_odds_breakdown(rows, sportsbooks=("draftkings", "fanduel"), markets=("spread", "moneyline")):
    """Print a (sportsbook, market_type) row-count table to stderr.

    This is the fastest way to tell "SharpAPI genuinely hasn't posted these
    lines yet" apart from "we're getting rows back but dropping them during
    matching" -- if a combination is missing here, it never reached us in
    the first place, so nothing downstream can be at fault.

    Checks against whichever `markets` were actually REQUESTED for this
    call, not a hardcoded ("spread", "moneyline") pair -- MLB requests
    "run_line" (not "spread"; see build_mlb_dashboard.py), so a hardcoded
    check for "spread" would always log a false "0 rows" NOTE for MLB even
    when run-line rows came through completely fine, since they're tagged
    market_type "run_line" in the response, never literally "spread".
    """
    from collections import Counter
    counts = Counter((r.get("sportsbook"), r.get("market_type")) for r in rows)
    if not counts:
        log("  SharpAPI returned 0 odds rows total")
        return
    log(f"  odds rows by (sportsbook, market): {dict(counts)}")
    for book in sportsbooks:
        for market in markets:
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


# SharpAPI used to embed the spread number in the `selection` string itself,
# e.g. "Georgia Bulldogs -7" or "Kansas City Chiefs -3.5", with no separate
# numeric field. That's no longer true: SharpAPI now returns the spread as
# its own top-level `line` field on the row (e.g. {"selection": "Georgia",
# "line": -7.0}), and `selection` is just the bare team name with nothing
# trailing it. We read `line` directly and only fall back to parsing it out
# of `selection` for old-format rows, so this keeps working either way.
#
# Tolerant of: a unicode minus sign (some feeds use \u2212 instead of a
# plain hyphen), extra/odd whitespace, and "PK"/"PICK" for a pick'em game
# (treated as a 0 line) since none of those are guaranteed to show up but
# cost nothing to handle if they do.
_SPREAD_SELECTION_RE = re.compile(r"^(.*?)\s+([+-]\d+(?:\.\d+)?)$")
_SPREAD_PICKEM_RE = re.compile(r"^(.*?)\s+(?:PK|PICK)$", re.IGNORECASE)


def _parse_selection(selection, market, line_field=None):
    """Split a selection string into (team_name_part, line_or_None).

    `line_field` is the row's own `line` value (current SharpAPI format).
    When present for a spread row, it's used directly instead of parsing
    the (now plain team-name) `selection` string.
    """
    if market == "spread":
        if line_field is not None:
            return selection.strip(), float(line_field)

        cleaned = selection.strip().replace("\u2212", "-")
        m = _SPREAD_SELECTION_RE.match(cleaned)
        if m:
            return m.group(1), float(m.group(2))
        m = _SPREAD_PICKEM_RE.match(cleaned)
        if m:
            return m.group(1), 0.0
        log(f"  WARNING: couldn't parse spread selection {selection!r} -- treating as team name only, no line")
        return selection, None
        
    # THIS LINE MUST BE HERE FOR MONEYLINES:
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


# SharpAPI's spread-equivalent market is named differently per sport --
# football's is "point_spread", baseball's is "run_line" (the MLB run line
# is effectively always the fixed +/-1.5). Both map back to our internal
# "spread" bucket.
_SPREAD_MARKET_ALIASES = {"point_spread": "spread", "run_line": "spread", "spread": "spread"}


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
        market = _SPREAD_MARKET_ALIASES.get(row.get("market_type"), row.get("market_type"))

        if book not in result or market not in ("spread", "moneyline"):
            continue

        # A book typically posts many alternate lines alongside the main
        # one (e.g. every run line from +/-0.5 up to +/-8.5 for MLB, or a
        # handful of alternate point spreads for football) -- keep only
        # the primary line SharpAPI itself flags, so the board doesn't end
        # up randomly picking whichever alternate line happened to match
        # first. Moneyline has no alternates, so this only applies to spread.
        if market == "spread":
            if row.get("is_alternate_line"):
                continue
            if row.get("is_main_line") is False:
                continue

        # Prefer SharpAPI's own selection_type field ("home"/"away", always
        # relative to THIS row's own home_team/away_team) over parsing the
        # `selection` text -- selection is sometimes an abbreviated form
        # (e.g. "TEX Rangers", "Athletics" with no city) that doesn't always
        # fuzzy-match reliably against the full team name, which was
        # silently dropping some games' odds. Only fall back to the old
        # text-parsing approach for rows that don't have selection_type at
        # all, so nothing that used to match stops matching.
        row_side = row.get("selection_type")
        if row_side not in ("home", "away"):
            team_part, line_val = _parse_selection(row.get("selection", ""), market, row.get("line"))
            row_side = "home" if _fuzzy_team(home_norm, team_part) else (
                "away" if _fuzzy_team(away_norm, team_part) else None
            )
            if row_side is None:
                continue
            # This fallback path found `row_side` by matching directly
            # against OUR home/away already, so it's already in our terms
            # -- skip the row-side -> our-side remap below for it.
            r_home = row.get("home_team", "")
            is_flipped = _fuzzy_team(away_norm, r_home)
            final_line = line_val
            if market == "spread" and line_val is not None and is_flipped:
                final_line = -line_val
            result[book][market][row_side] = {"line": final_line, "american": row.get("odds_american")}
            continue

        # row_side is relative to the ROW's own home_team/away_team, which
        # may be flipped relative to ours (e.g. a neutral-site game where
        # SharpAPI lists the two teams in the opposite home/away order ESPN
        # does). Remap to OUR home/away -- the line value itself needs no
        # sign change, since it already reflects whichever row-side it
        # belongs to; we're only relabeling which of OUR teams that is.
        r_home = row.get("home_team", "")
        is_flipped = _fuzzy_team(away_norm, r_home)
        our_side = row_side
        if is_flipped:
            our_side = "away" if row_side == "home" else "home"

        _, line_val = _parse_selection(row.get("selection", ""), market, row.get("line"))
        result[book][market][our_side] = {
            "line": line_val,
            "american": row.get("odds_american"),
        }

    team_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Merging freshly-built weeks into whatever's already on disk (never delete
# old weeks -- picks can go back to week 1 forever)
# ---------------------------------------------------------------------------

def load_existing_dashboard(path):
    """Read a previous build's output JSON, or None if it doesn't exist yet
    (first-ever run) or can't be parsed."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  NOTE: couldn't read previous output at {path} ({e}) -- starting fresh, nothing to merge.")
        return None


def merge_weeks(existing_data, new_weeks):
    """Merge freshly-built weeks on top of whatever weeks are already on
    disk, so old weeks are never lost.

    We never want a day's build to be the *only* copy of the data -- once a
    week ages out of the "this week + next week" window a build script
    fetches, that week's games, odds, and Gemini predictions should still
    sit in the JSON forever (so Picks can grade a bet from week 1 in week
    12). Any previously-stored week whose number isn't part of this build's
    fresh output is carried forward untouched; a week number that *is* part
    of this build's output is fully replaced by the fresh version (newer
    odds/rankings/predictions win). Returns weeks sorted by week number.
    """
    new_week_nums = {w["week"] for w in new_weeks}
    old_weeks = [w for w in (existing_data or {}).get("weeks", []) if w.get("week") not in new_week_nums]
    merged = old_weeks + list(new_weeks)
    merged.sort(key=lambda w: w["week"])
    return merged


# ---------------------------------------------------------------------------
# Carrying odds forward across runs (books periodically pull lines, then
# repost them later -- don't let that show up as the board going blank)
# ---------------------------------------------------------------------------

def load_previous_odds_by_game(path):
    """Read a previous build's output JSON and return {game_id: odds_dict}
    for every game found in it.

    Used so a book temporarily pulling a line doesn't blank it out on the
    board -- see carry_forward_odds() below, which this feeds. Returns {}
    if the file doesn't exist yet (first-ever run) or can't be parsed,
    which just means there's nothing to carry forward this time.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  NOTE: couldn't read previous output at {path} ({e}) -- starting fresh, nothing to carry forward.")
        return {}

    lookup = {}
    for week in data.get("weeks", []):
        for day in week.get("days", []):
            for slot in day.get("time_slots", []):
                for g in slot.get("games", []):
                    gid = g.get("id")
                    if gid is not None:
                        lookup[gid] = g.get("odds")
    return lookup


def load_previous_game_entries(path):
    """Read a previous build's output JSON and return {game_id: game_dict}
    (keyed by the game id as a STRING, to match how scores.json keys its
    game ids) for every game found in it.

    Fuller sibling of load_previous_odds_by_game -- used specifically to
    freeze a started game's odds AND Gemini prediction at exactly whatever
    was last saved (see load_started_game_ids / freeze-once-started below),
    rather than just carrying forward odds. Returns {} if the file doesn't
    exist yet or can't be parsed.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  NOTE: couldn't read previous output at {path} ({e}) -- nothing to freeze against.")
        return {}

    lookup = {}
    for week in data.get("weeks", []):
        for day in week.get("days", []):
            for slot in day.get("time_slots", []):
                for g in slot.get("games", []):
                    gid = g.get("id")
                    if gid is not None:
                        lookup[str(gid)] = g
    return lookup


def load_started_game_ids(scores_path, sport_key):
    """Return a set of game ids (as strings) that already have a score
    recorded in data/scores.json for the given sport ('cfb'/'nfl'/'mlb').

    fetch_scores.py only ever writes an entry once a game's ESPN status
    leaves "pre" (see fetch_scores.py) -- so "has an entry here" is
    equivalent to "kickoff has already happened, game is live or final".
    Used to freeze odds and Gemini predictions once a game starts: a
    sportsbook's in-game line moves constantly and doesn't reflect the
    pregame market our picks/predictions were made against, and
    re-calling Gemini against a moving in-game line mid-game (or after
    the game's over) doesn't make sense either. Returns an empty set if
    scores.json doesn't exist yet or can't be parsed -- just means
    nothing gets frozen this run.
    """
    if not scores_path or not os.path.exists(scores_path):
        return set()
    try:
        with open(scores_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  NOTE: couldn't read {scores_path} ({e}) -- treating no games as started.")
        return set()
    return set((data.get(sport_key) or {}).keys())


def carry_forward_odds(new_odds, previous_odds):
    """
    Fill in any (book, market, side) odds entry that's missing from
    `new_odds` (today's fresh SharpAPI match) using whatever was captured
    for that same game last run, so a book temporarily pulling a line
    doesn't blank it out on the board.

    Whenever today's fetch DOES have a value for a given (book, market,
    side), that value always wins over the old one -- even if it's
    unchanged, since it's simply the freshest read. Only an entry's
    *absence* today gets patched from history, so a real line move still
    shows up immediately.
    """
    if not previous_odds:
        return new_odds
    merged = {}
    for book in ("draftkings", "fanduel"):
        merged[book] = {"spread": {}, "moneyline": {}}
        new_book = (new_odds or {}).get(book, {}) or {}
        old_book = (previous_odds or {}).get(book, {}) or {}
        for market in ("spread", "moneyline"):
            new_sides = new_book.get(market, {}) or {}
            old_sides = old_book.get(market, {}) or {}
            for side in ("home", "away"):
                if new_sides.get(side):
                    merged[book][market][side] = new_sides[side]
                elif old_sides.get(side):
                    merged[book][market][side] = old_sides[side]
    return merged
