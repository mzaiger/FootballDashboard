#!/usr/bin/env python3
"""
College Football Betting Dashboard builder.

Pulls this week's schedule, broadcast network, team records, and the AP
Top 25 poll from ESPN's public (unofficial, no-key-required) scoreboard
and rankings APIs, ranks each matchup by a blend of combined AP rank,
combined win-rank, and posted spread, then attaches DraftKings / FanDuel
spread + moneyline odds from SharpAPI (via common.py). Exports everything
to data/ncaaf_dashboard.json for the static index.html front-end to
consume.

Previously this pulled from CollegeFootballData.com (CFBD), which
required its own API key and has a much tighter rate limit than ESPN's
public endpoint. CFBD is no longer used anywhere in this script.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard/rankings endpoints)

Usage:
    python scripts/build_ncaaf_dashboard.py
    python scripts/build_ncaaf_dashboard.py --week 1 --year 2026
"""

import argparse
import os
import re
import sys
import json
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import requests

from common import (
    DISPLAY_TIMEZONE,
    TIME_SLOT_ORDER,
    assign_matchup_ranks,
    carry_forward_odds,
    fetch_all_odds,
    load_existing_dashboard,
    load_previous_game_entries,
    load_previous_odds_by_game,
    load_started_game_ids,
    log,
    match_odds_for_game,
    merge_weeks,
    normalize_minmax,
    time_slot_for,
)
from gemini_predictions import attach_gemini_predictions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
REQUEST_TIMEOUT = 20

# groups=80 = FBS only (see https://github.com/pseudo-r/Public-ESPN-API);
# without it, and without a high limit, the scoreboard endpoint silently
# truncates to a top-25-ish subset of games instead of the full slate.
FBS_GROUP = "80"
SCOREBOARD_LIMIT = 500

# seasontype: 1=preseason, 2=regular season, 3=postseason. FBS doesn't
# really have a preseason slate the way the NFL does -- ESPN just serves
# the upcoming week's games under season_type 2 even before week 1 has
# kicked off -- so 2 is the sane default here (only used as a last-resort
# fallback if ESPN's response is ever missing a season type entirely; see
# get_espn_current_state()).
SEASON_TYPE_FALLBACK = 2
SEASON_YEAR_DEFAULT = 2026

# "Main channels" = national broadcast + flagship cable. Games on ESPNU,
# SECN, ESPN+, streaming-only, etc. are filtered out. Edit this set to
# widen or narrow what counts as a "main channel" game.
MAIN_CHANNELS = {"ABC", "CBS", "NBC", "FOX", "ESPN", "ESPN2", "FS1"}

# Nebraska always gets pulled onto the board and always wins its time
# slot's "Time Slot Most Watchable Game" pick, no matter its AP rank or
# spread relative to anything else in that window.
NEBRASKA_TEAM = "Nebraska"

# A team with no AP-poll ranking gets this value -- one worse than the
# worst possible AP Top 25 rank -- so it never outranks a team that's
# actually ranked. Kept separate from win-rank's own "unranked" value
# below since these two components are normalized independently before
# being blended.
UNRANKED_VALUE = 50

# A team with no games played yet (or missing a record) gets this
# win-rank value -- one worse than the most FBS teams that could
# realistically appear on one board -- so it never outranks a team that
# actually has a record.
UNRANKED_WIN_RANK = 135


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(year, week, season_type):
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": year, "week": week, "seasontype": season_type,
                "groups": FBS_GROUP, "limit": SCOREBOARD_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_rankings(year, week, season_type):
    resp = requests.get(
        ESPN_RANKINGS_URL,
        params={"year": year, "week": week, "seasontype": season_type},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def broadcast_names(event):
    """All national broadcast name strings for a game, RAW (not yet split
    or joined) -- across every ESPN payload shape that can carry them.
    Used both for display (broadcast_label, joined with ", ") and for the
    main-channel filter (on_main_channel, which further splits each
    string -- see there for why)."""
    names = []

    for b in event.get("broadcasts", []):
        for n in b.get("names", []):
            if n and n not in names:
                names.append(n)

    for comp in event.get("competitions", []):
        for b in comp.get("broadcasts", []):
            for n in b.get("names", []):
                if n and n not in names:
                    names.append(n)
            media_name = b.get("media", {}).get("shortName")
            if media_name and media_name not in names:
                names.append(media_name)

    for gb in event.get("geoBroadcasts", []):
        short = gb.get("media", {}).get("shortName")
        if short and short not in names:
            names.append(short)

    return names


def broadcast_label(event):
    """Display string for a game's TV pill -- ", " instead of "/" between
    multiple networks (matches NFL/MLB's broadcast_label)."""
    names = broadcast_names(event)
    return ", ".join(names) if names else None


_CHANNEL_SPLIT_RE = re.compile(r"[/,]")


def _channel_tokens(names):
    """Split each raw broadcast name into individual channel tokens.

    ESPN sometimes packs more than one network into a SINGLE string
    inside one broadcast object (e.g. "ESPN2/ACCNX" as one entry in
    `names`) instead of giving each network its own list entry. A plain
    `outlet in MAIN_CHANNELS` check (which works fine for NFL, where this
    doesn't happen) would silently miss those combined-string games, so
    every raw name here gets split on "/" and "," before being checked
    against MAIN_CHANNELS.
    """
    tokens = []
    for n in names:
        if not n:
            continue
        for part in _CHANNEL_SPLIT_RE.split(n):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return tokens


def on_main_channel(event, channels):
    return any(tok in channels for tok in _channel_tokens(broadcast_names(event)))


def _parse_espn_record(competitor):
    """Extract (wins, losses, ties, summary) from an ESPN scoreboard
    competitor's `records` list, or (None, None, None, None) if no
    overall record is present yet (e.g. before that team's first game)."""
    for rec in competitor.get("records", []):
        if rec.get("type") == "total" or rec.get("name") == "overall":
            summary = rec.get("summary")
            if not summary:
                continue
            parts = summary.split("-")
            try:
                wins, losses = int(parts[0]), int(parts[1])
                ties = int(parts[2]) if len(parts) > 2 else 0
                return wins, losses, ties, summary
            except (ValueError, IndexError):
                return None, None, None, summary
    return None, None, None, None


def build_win_rank_lookup(team_records):
    """Rank every team with a known record by win percentage this season
    (rank 1 = best record on the board), the same convention NFL's/MLB's
    win-rank lookups use. Ties broken by raw win total, then by team id
    for a stable, deterministic order. `team_records` is
    {team_id: (wins, losses, ties)}. Returns {team_id: rank}."""
    entries = []
    for team_id, (wins, losses, ties) in team_records.items():
        games_played = wins + losses + ties
        pct = (wins + 0.5 * ties) / games_played if games_played else -1
        entries.append((team_id, pct, wins))
    entries.sort(key=lambda e: (-e[1], -e[2], e[0]))
    return {team_id: i + 1 for i, (team_id, pct, wins) in enumerate(entries)}


def build_rank_lookup(rankings_payload, poll_name="AP Top 25"):
    """{team_id: rank} from the first matching poll ("AP Top 25" by
    default). Falls back to any poll present if AP specifically isn't
    found (mirrors the old CFBD fallback -- AP is occasionally slower to
    post than the Coaches poll early in the week)."""
    lookup = {}
    for release in rankings_payload.get("rankings", []):
        if release.get("name") == poll_name or release.get("shortName") == "AP Poll":
            for entry in release.get("ranks", []):
                team_id = entry.get("team", {}).get("id")
                if team_id is not None:
                    lookup[team_id] = entry.get("current")
            if lookup:
                return lookup
    for release in rankings_payload.get("rankings", []):
        for entry in release.get("ranks", []):
            team_id = entry.get("team", {}).get("id")
            if team_id is not None:
                lookup.setdefault(team_id, entry.get("current"))
        if lookup:
            return lookup
    return lookup


def get_rank_lookup_with_fallback(year, week, cache=None):
    """{team_id: rank} for `week`, carrying forward from the most recent
    earlier week if `week`'s poll hasn't been released yet (e.g. building
    "this week + next week" before this week has kicked off, when next
    week's poll genuinely doesn't exist yet).

    `cache` is an optional {week: rankings_payload} dict so build() can
    share fetched weeks across build_week() calls instead of re-fetching
    the same week's rankings more than once.
    """
    if cache is None:
        cache = {}

    def payload_for(w):
        if w not in cache:
            cache[w] = get_rankings(year, w, 2)
        return cache[w]

    lookup = build_rank_lookup(payload_for(week))
    if lookup:
        return lookup, week

    for earlier in range(week - 1, 0, -1):
        lookup = build_rank_lookup(payload_for(earlier))
        if lookup:
            log(f"  No rankings published yet for week {week}; using week {earlier}'s poll instead.")
            return lookup, earlier

    log(f"  No rankings found for week {week} or any earlier week -- all teams will show as unranked.")
    return {}, None


# ---------------------------------------------------------------------------
# Matchup ranking
# ---------------------------------------------------------------------------

def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


def matchup_components(home_rank, away_rank, home_win_rank, away_win_rank, odds):
    """Per-game (ap_component, win_component, spread_component) tuple for
    build_week to collect across the whole week and normalize/blend at
    once (see build_week's matchup_score assignment below) -- mirrors
    NFL's/MLB's matchup_score pattern. Any component with no data for
    this particular game (e.g. neither team ranked, or no line posted
    yet) comes back None and normalize_minmax() treats it as a neutral
    mid-scale value rather than penalizing the game for missing data.
    """
    ap_component = None
    if home_rank is not None or away_rank is not None:
        ap_component = (home_rank or UNRANKED_VALUE) + (away_rank or UNRANKED_VALUE)

    win_component = None
    if home_win_rank is not None or away_win_rank is not None:
        win_component = (home_win_rank or UNRANKED_WIN_RANK) + (away_win_rank or UNRANKED_WIN_RANK)

    spread = _home_spread(odds)
    spread_component = abs(spread) if spread is not None else None

    return ap_component, win_component, spread_component


def _closest_spread_abs(game_entry):
    """Smallest absolute spread line found across books/sides for this game,
    or None if no spread has been posted anywhere yet."""
    best = None
    odds = game_entry.get("odds") or {}
    for book in ("draftkings", "fanduel"):
        spread = odds.get(book, {}).get("spread", {})
        for side in ("home", "away"):
            entry = spread.get(side)
            line = entry.get("line") if entry else None
            if line is None:
                continue
            val = abs(line)
            if best is None or val < best:
                best = val
    return best


def choose_slot_pick(games_sorted):
    """Pick the "Time Slot Most Watchable Game".

    Priority:
      1. Nebraska is in this slot -> Nebraska is the pick, always.
      2. Otherwise, score every game in the slot on two metrics and blend
         them 50/50:
           - combined AP rank (matchup_score; unranked teams count as
             UNRANKED_VALUE), lower = more marquee
           - closest posted spread (abs value across books), lower = more
             competitive game
         Each metric is min-max normalized across just this slot's games
         (0 = best in the slot, 1 = worst), then averaged 50/50. The game
         with the lowest blended score is the most watchable pick.

    Returns (index_into_games_sorted, reason) or (None, None) if empty.
    """
    if not games_sorted:
        return None, None

    for i, g in enumerate(games_sorted):
        if g.get("is_nebraska"):
            return i, "nebraska"

    ap_values = [g["matchup_score"] for g in games_sorted]
    spread_values = [_closest_spread_abs(g) for g in games_sorted]

    ap_norm = normalize_minmax(ap_values)
    spread_norm = normalize_minmax(spread_values)

    blended = [0.5 * a + 0.5 * s for a, s in zip(ap_norm, spread_norm)]
    idx = min(range(len(blended)), key=lambda i: blended[i])
    return idx, "watchability"


# ---------------------------------------------------------------------------
# "Current week" resolution -- same pattern as build_nfl_dashboard.py
# ---------------------------------------------------------------------------

def _extract_season_type(payload):
    raw = payload.get("season", {}).get("type")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict):
        val = raw.get("type", raw.get("id"))
        try:
            return int(val)
        except (TypeError, ValueError):
            pass

    leagues = payload.get("leagues") or [{}]
    raw2 = (leagues[0].get("season") or {}).get("type")
    if isinstance(raw2, int):
        return raw2
    if isinstance(raw2, dict):
        val = raw2.get("type", raw2.get("id"))
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    return None


def get_espn_current_state():
    """Ask ESPN what CFB week AND season type "today" falls under, off the
    real calendar date -- same approach as build_nfl_dashboard.py's own
    get_espn_current_state(). Returns (week_number, season_type)."""
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": today_str, "groups": FBS_GROUP, "limit": SCOREBOARD_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    week = payload.get("week", {}).get("number", 1)
    season_type = _extract_season_type(payload)
    if season_type is None:
        log(f"  NOTE: ESPN's response didn't include a season type for {today_str} -- "
            f"falling back to season_type={SEASON_TYPE_FALLBACK}.")
        season_type = SEASON_TYPE_FALLBACK
    if season_type == 1:
        # FBS doesn't have a real "preseason" slate the way the NFL does --
        # if ESPN ever does report 1 (preseason) for a given date, treat it
        # the same as the "no season type in the response at all" case
        # above and fall back straight to the regular season.
        log("  NOTE: ESPN reported season_type=1 (preseason), which doesn't apply to "
            "college football -- using season_type=2 (regular season) instead.")
        season_type = SEASON_TYPE_FALLBACK
    log(f"ESPN reports current week {week}, season_type {season_type} for {today_str}.")
    return week, season_type


def get_espn_current_week_for(season_type):
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"seasontype": season_type, "dates": today_str, "groups": FBS_GROUP, "limit": SCOREBOARD_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    week = resp.json().get("week", {}).get("number", 1)
    log(f"ESPN reports current week as {week} for {today_str} (forced season_type {season_type}).")
    return week


def highest_stored_week_info(existing_data):
    best_week, best_kickoff = None, None
    for w in (existing_data or {}).get("weeks", []):
        games = [g for day in w.get("days", []) for slot in day.get("time_slots", []) for g in slot.get("games", [])]
        kickoffs = []
        for g in games:
            raw = g.get("start_time")
            if not raw:
                continue
            try:
                kickoffs.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
        last_kickoff = max(kickoffs) if kickoffs else None
        if best_week is None or w.get("week", 0) > best_week:
            best_week, best_kickoff = w.get("week"), last_kickoff
    return best_week, best_kickoff


def earliest_unelapsed_stored_week(existing_data):
    now = datetime.now(timezone.utc)
    for w in sorted((existing_data or {}).get("weeks", []), key=lambda w: w.get("week", 0)):
        games = [g for day in w.get("days", []) for slot in day.get("time_slots", []) for g in slot.get("games", [])]
        kickoffs = []
        for g in games:
            raw = g.get("start_time")
            if not raw:
                continue
            try:
                kickoffs.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
        if kickoffs and max(kickoffs) >= now:
            return w.get("week")
    return None


def resolve_current(existing_data, forced_season_type=None):
    """Same logic as build_nfl_dashboard.py's resolve_current() -- ESPN's
    own answer, guarded by a floor check (don't sit behind an
    already-built future week) and an anti-regression check (weeks only
    move forward during a season)."""
    if forced_season_type is not None:
        espn_week = get_espn_current_week_for(forced_season_type)
        season_type = forced_season_type
    else:
        espn_week, season_type = get_espn_current_state()

    floor_week = earliest_unelapsed_stored_week(existing_data)
    if floor_week is not None and espn_week < floor_week - 1:
        fallback_week = floor_week - 1
        log(f"  NOTE: ESPN reports week {espn_week} as current, but week {floor_week} is "
            f"already built with games still ahead of us -- using week {fallback_week} "
            f"instead so both show.")
        return fallback_week, season_type

    stored_current_week = (existing_data or {}).get("current_week")
    stored_season_type = (existing_data or {}).get("season_type")
    if (stored_current_week is not None and stored_season_type == season_type
            and espn_week < stored_current_week):
        log(f"  NOTE: ESPN reports week {espn_week} as current, but we already advanced to "
            f"week {stored_current_week} on a previous run (season_type {season_type}) -- "
            f"keeping {stored_current_week} instead of regressing.")
        return stored_current_week, season_type

    highest_week, highest_last_kickoff = highest_stored_week_info(existing_data)
    now = datetime.now(timezone.utc)
    if (highest_week is not None and highest_last_kickoff is not None
            and highest_last_kickoff < now and espn_week <= highest_week):
        fallback_week = highest_week + 1
        log(f"  NOTE: every game in stored week {highest_week} has already kicked off, but ESPN "
            f"still reports week {espn_week} as current -- using week {fallback_week} instead.")
        return fallback_week, season_type

    return espn_week, season_type


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build_week(year, week, season_type, sharp_key, channels, gemini_key=None, rankings_cache=None,
               previous_odds_by_id=None, previous_entries_by_id=None, started_game_ids=None):
    """Build a single week's worth of games. Returns the per-week dict
    (no generated_at/season wrapper -- that's added once, by build())."""
    log(f"Fetching CFB schedule for {year}, week {week}, seasontype {season_type}...")
    scoreboard = get_scoreboard(year, week, season_type)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching AP rankings...")
    rank_lookup, rank_source_week = get_rank_lookup_with_fallback(year, week, cache=rankings_cache)
    log(f"  {len(rank_lookup)} ranked teams found" + (
        f" (from week {rank_source_week}'s poll)" if rank_source_week not in (None, week) else ""
    ))

    log("Fetching DraftKings/FanDuel NCAAF odds from SharpAPI...")
    # Fetch ONE DAY AT A TIME across this week's actual game dates, rather
    # than a single date_from/date_to spanning the whole week -- CFB has
    # far more games per week than NFL (60+ some weeks), so a full week's
    # spread+moneyline rows, including every alternate line SharpAPI
    # posts per game, can run into the thousands in one request.
    game_dates = set()
    for event in events:
        raw = event.get("date")
        if not raw:
            continue
        try:
            game_dates.add(datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d"))
        except ValueError:
            continue
    odds_rows = []
    for d in sorted(game_dates):
        day_rows = fetch_all_odds(sharp_key, league="ncaaf", markets=("spread", "moneyline"),
                                   date_from=d, date_to=d)
        odds_rows.extend(day_rows)
    log(f"  {len(odds_rows)} odds rows returned across {len(game_dates)} day(s)")
    team_cache = {}
    row_claims = {}

    days = {}
    all_games = []  # flat list, mirrors what's in `days`, for the Gemini pass below
    skipped_no_tv = 0
    team_records = {}  # {team_id: (wins, losses, ties)} -- accumulated as we go
    started_game_ids = started_game_ids or set()
    previous_entries_by_id = previous_entries_by_id or {}
    frozen_prediction_skip_ids = set()  # passed to attach_gemini_predictions below

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

        home_team = home["team"].get("displayName")
        away_team = away["team"].get("displayName")
        home_id = home["team"].get("id")
        away_id = away["team"].get("id")
        is_nebraska = NEBRASKA_TEAM in (home_team or "", away_team or "")

        outlet = broadcast_label(event)
        main_channel = on_main_channel(event, channels)
        # Nebraska always makes the board, even if it's on a non-main
        # channel (or nothing found in the broadcast feed at all) --
        # everything else still requires a main-channel broadcast.
        if not main_channel and not is_nebraska:
            skipped_no_tv += 1
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

        home_wins, home_losses, home_ties, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_ties, away_record = _parse_espn_record(away)
        if home_wins is not None and home_id is not None:
            team_records[home_id] = (home_wins, home_losses, home_ties)
        if away_wins is not None and away_id is not None:
            team_records[away_id] = (away_wins, away_losses, away_ties)

        home_rank = rank_lookup.get(home_id)
        away_rank = rank_lookup.get(away_id)

        game_id = event.get("id")
        gid_str = str(game_id)
        already_started = gid_str in started_game_ids
        previous_entry = previous_entries_by_id.get(gid_str)

        # Once a game has ANY score recorded in scores.json (live or
        # final -- see load_started_game_ids), freeze its odds and Gemini
        # prediction at exactly whatever was last saved instead of
        # re-fetching/re-matching/re-calling: an in-game line moves
        # constantly and doesn't reflect the pregame market the pick/
        # prediction was actually made against.
        if already_started and previous_entry is not None:
            odds = previous_entry.get("odds") or {}
            frozen_prediction_skip_ids.add(gid_str)
        else:
            odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
            if previous_odds_by_id:
                odds = carry_forward_odds(odds, previous_odds_by_id.get(event.get("id")))

        game_entry = {
            "id": game_id,
            "start_time": start_raw,
            "start_time_tbd": is_tbd,
            "home_team": home_team,
            "home_rank": home_rank,
            "home_record": home_record or "0-0",
            "away_team": away_team,
            "away_rank": away_rank,
            "away_record": away_record or "0-0",
            "matchup_score": None,  # filled in below, once every game's components are known
            "channel": outlet or "Not on Main TV",
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
            "is_nebraska": is_nebraska,
            "_home_id": home_id,
            "_away_id": away_id,
        }
        if already_started and previous_entry is not None and previous_entry.get("gemini_prediction"):
            game_entry["gemini_prediction"] = previous_entry["gemini_prediction"]
        days.setdefault(day_key, {}).setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    log(f"  {skipped_no_tv} games skipped (not on a main channel)")

    # Matchup score blends three components -- 50% combined AP Top 25
    # rank, 25% combined win-rank (teams ranked by this season's record,
    # like an AP-style poll), 25% posted spread -- each normalized 0-100
    # across the whole week before blending, same pattern NFL/MLB use for
    # their own blends.
    win_rank_lookup = build_win_rank_lookup(team_records)
    ap_components, win_components, spread_components = [], [], []
    for g in all_games:
        home_win_rank = win_rank_lookup.get(g["_home_id"])
        away_win_rank = win_rank_lookup.get(g["_away_id"])
        a, w, s = matchup_components(g["home_rank"], g["away_rank"], home_win_rank, away_win_rank, g["odds"])
        ap_components.append(a)
        win_components.append(w)
        spread_components.append(s)
    ap_norm = normalize_minmax(ap_components)
    win_norm = normalize_minmax(win_components)
    spread_norm = normalize_minmax(spread_components)
    for g, an, wn, sn in zip(all_games, ap_norm, win_norm, spread_norm):
        g["matchup_score"] = round(100 * (0.5 * an + 0.25 * wn + 0.25 * sn), 1)
        # Internal-only fields, not part of the public JSON shape.
        del g["_home_id"]
        del g["_away_id"]

    # Rank spans the WHOLE WEEK, across every day in it, not just
    # whichever time slot or day a game lands in.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="cfb", season=year, week=week, gemini_key=gemini_key,
                               skip_ids=frozen_prediction_skip_ids)

    day_list = []
    for day_key in sorted(days.keys()):
        slots_for_day = days[day_key]
        time_slots = []
        for slot_name in TIME_SLOT_ORDER:
            if slot_name not in slots_for_day:
                continue
            games_sorted = sorted(slots_for_day[slot_name], key=lambda x: x["matchup_score"])
            best_score = games_sorted[0]["matchup_score"] if games_sorted else None
            pick_idx, pick_reason = choose_slot_pick(games_sorted)
            for i, g in enumerate(games_sorted):
                g["is_slot_pick"] = (i == pick_idx)
            time_slots.append({
                "slot": slot_name,
                "best_matchup_score": best_score,
                "pick_reason": pick_reason,
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

    return {
        "week": week,
        "total_games": sum(d["game_count"] for d in day_list),
        "days": day_list,
    }


def build(year, week_start, season_type, sharp_key, channels, gemini_key=None, num_weeks=2, previous_odds_by_id=None,
          previous_entries_by_id=None, started_game_ids=None):
    """Build `num_weeks` consecutive weeks starting at week_start (default:
    this week + next week) and wrap them into the full output payload."""
    weeks = []
    rankings_cache = {}
    for offset in range(num_weeks):
        weeks.append(build_week(year, week_start + offset, season_type, sharp_key, channels,
                                 gemini_key, rankings_cache=rankings_cache, previous_odds_by_id=previous_odds_by_id,
                                 previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "season_type": season_type,
        "main_channels": sorted(channels),
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": weeks,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NCAAF betting dashboard JSON.")
    p.add_argument("--year", type=int, default=SEASON_YEAR_DEFAULT, help="Season year")
    p.add_argument("--week", type=int, default=None, help="Starting week number (default: ESPN's current week); this week and the following week are both built")
    p.add_argument("--num-weeks", type=int, default=2, help="How many consecutive weeks to build starting at --week (default: 2)")
    p.add_argument("--season-type", type=int, default=None,
                    help="1=preseason, 2=regular season, 3=postseason (default: auto-detected from ESPN each run)")
    p.add_argument("--out", default=None, help="Output path (default: data/ncaaf_dashboard.json)")
    p.add_argument("--scores", default=None, help="Path to scores.json, used to freeze odds/Gemini predictions for started games (default: data/scores.json)")
    return p.parse_args()


def main():
    args = parse_args()

    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("GEMINI_KEY not set -- building without Gemini predictions.")

    week = args.week
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "ncaaf_dashboard.json")
    out_path = os.path.abspath(out_path)
    scores_path = args.scores or os.path.join(script_dir, "..", "data", "scores.json")
    scores_path = os.path.abspath(scores_path)

    existing_data = load_existing_dashboard(out_path)

    season_type = args.season_type
    if week is None:
        week, season_type = resolve_current(existing_data, forced_season_type=args.season_type)
    elif season_type is None:
        _, season_type = get_espn_current_state()

    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    previous_entries_by_id = load_previous_game_entries(out_path)
    started_game_ids = load_started_game_ids(scores_path, "cfb")
    if started_game_ids:
        log(f"{len(started_game_ids)} NCAAF game(s) already have a score recorded in {scores_path} -- "
            f"freezing odds and Gemini predictions for those instead of updating them.")

    output = build(args.year, week, season_type, sharp_key, MAIN_CHANNELS, gemini_key,
                    num_weeks=args.num_weeks, previous_odds_by_id=previous_odds_by_id,
                    previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids)
    fresh_week_nums = [w["week"] for w in output["weeks"]]

    # Record which week THIS build resolved as "current" -- College/NFL use
    # this (plus current_week + 1) to decide what to display, instead of
    # re-deriving "current" client-side from individual game timestamps.
    output["current_week"] = week

    # Never drop old weeks -- merge today's freshly-built weeks on top of
    # whatever weeks were already on disk instead of replacing the file
    # wholesale, so lines/scores/predictions from every past week stay
    # available (Picks and Accuracy show all of them; College only shows
    # current_week and current_week + 1).
    output["weeks"] = merge_weeks(existing_data, output["weeks"])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    week_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across {len(week_nums)} week(s) total ({week_nums}) to {out_path}; "
        f"freshly built this run: {fresh_week_nums} (season_type={season_type})")


if __name__ == "__main__":
    main()
