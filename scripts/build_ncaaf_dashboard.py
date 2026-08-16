#!/usr/bin/env python3
"""
College Football Betting Dashboard builder.

Pulls this week's TV schedule + AP rankings from CollegeFootballData.com (CFBD),
ranks each matchup by combined team rank (lower = marquee game), then attaches
DraftKings / FanDuel spread + moneyline odds from SharpAPI (via common.py).
Exports everything to data/ncaaf_dashboard.json for the static index.html front-end
to consume.

Env vars required:
    CFBD_API_KEY     - key from https://collegefootballdata.com/key
    SHARPAPI_KEY      - key from https://sharpapi.io

Usage:
    python scripts/build_ncaaf_dashboard.py
    python scripts/build_ncaaf_dashboard.py --week 1 --year 2026
"""

import argparse
import json
import os
import sys
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
    load_previous_odds_by_game,
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

CFBD_BASE = "https://api.collegefootballdata.com"

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
UNRANKED_VALUE = 50

# Nebraska always gets pulled onto the board and always wins its time slot's
# "Time Slot Most Watchable Game" pick, no matter its AP rank or spread
# relative to anything else in that window.
NEBRASKA_TEAM = "Nebraska"

REQUEST_TIMEOUT = 20


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


def get_records(key, year):
    return cfbd_get("/records", key, {"year": year})


def build_record_lookup(records_payload):
    """{team_name: 'W-L' string} (or 'W-L-T' if the team has a tie) from
    CFBD's /records payload, for displaying under each team's name on the
    board. Season-to-date, so it fills in as the year progresses."""
    lookup = {}
    for entry in records_payload:
        team = entry.get("team")
        if not team:
            continue
        total = entry.get("total", {}) or {}
        wins = total.get("wins") if total.get("wins") is not None else 0
        losses = total.get("losses") if total.get("losses") is not None else 0
        ties = total.get("ties") or 0

        lookup[team] = f"{wins}-{losses}-{ties}" if ties else f"{wins}-{losses}"
    return lookup


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


def get_rank_lookup_with_fallback(cfbd_key, year, week, cache=None):
    """Get {team_name: rank} for `week`, carrying forward from the most
    recent earlier week if `week`'s poll hasn't been released yet.

    CFBD only publishes each week's AP poll after that week's games are
    played (e.g. the "Week 2" poll comes out once Week 1 wraps up). When
    we're building a future week ahead of time -- like showing "this week
    + next week" before this week has kicked off -- the later week's poll
    genuinely doesn't exist yet. Rather than show no rankings at all, this
    steps backward (week-1, week-2, ... down to week 1) and reuses the
    most recent released poll as a best-available approximation.

    `cache` is an optional {week: rankings_payload} dict so build() can
    share fetched weeks across build_week() calls instead of re-fetching
    the same week's rankings more than once.
    """
    if cache is None:
        cache = {}

    def payload_for(w):
        if w not in cache:
            cache[w] = get_rankings(cfbd_key, year, w)
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
# Main build
# ---------------------------------------------------------------------------

def build_week(year, week, cfbd_key, sharp_key, channels, gemini_key=None, rankings_cache=None, records_cache=None, previous_odds_by_id=None):
    """Build a single week's worth of games. Returns the per-week dict
    (no generated_at/season wrapper -- that's added once, by build())."""
    log(f"Fetching games for {year} week {week}...")
    games = get_games(cfbd_key, year, week)
    log(f"  {len(games)} games")

    log("Fetching TV/media info...")
    media = get_media(cfbd_key, year, week)
    media_by_game = {m["id"]: m for m in media if m.get("id")}

    log("Fetching AP rankings...")
    rank_lookup, rank_source_week = get_rank_lookup_with_fallback(cfbd_key, year, week, cache=rankings_cache)
    log(f"  {len(rank_lookup)} ranked teams found" + (
        f" (from week {rank_source_week}'s poll)" if rank_source_week not in (None, week) else ""
    ))

    if records_cache is not None and year in records_cache:
        record_lookup = records_cache[year]
    else:
        log("Fetching team records...")
        record_lookup = build_record_lookup(get_records(cfbd_key, year))
        log(f"  {len(record_lookup)} team records found")
        if records_cache is not None:
            records_cache[year] = record_lookup

    log("Fetching DraftKings/FanDuel NCAAF odds from SharpAPI...")
    # date_from/date_to scope the request to this week's actual games
    # instead of asking for everything currently posted league-wide --
    # fewer rows/pages to page through, and less exposed to any
    # pagination edge case.
    game_dates = []
    for g in games:
        raw = g.get("startDate")
        if not raw:
            continue
        try:
            game_dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    date_from = min(game_dates).strftime("%Y-%m-%d") if game_dates else None
    date_to = max(game_dates).strftime("%Y-%m-%d") if game_dates else None
    odds_rows = fetch_all_odds(sharp_key, league="ncaaf", date_from=date_from, date_to=date_to)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    days = {}
    all_games = []  # flat list, mirrors what's in `days`, for the Gemini pass below
    skipped_no_tv = 0

    for g in games:
        game_id = g.get("id")
        media_info = media_by_game.get(game_id)
        outlet = (media_info or {}).get("outlet")
        if outlet:
            # ", " instead of "/", matching NFL/MLB's broadcast_label --
            # CFBD's own outlet field is normally a single network, but
            # this is a no-op unless it ever isn't.
            outlet = outlet.replace("/", ", ")
        home_team, away_team = g.get("homeTeam"), g.get("awayTeam")
        is_nebraska = NEBRASKA_TEAM in (home_team, away_team)

        on_main_channel = bool(outlet) and outlet in channels
        # Nebraska always makes the board, even if it's on a non-main
        # channel (or nothing found in the media feed at all) -- everything
        # else still requires a main-channel broadcast.
        if not on_main_channel and not is_nebraska:
            skipped_no_tv += 1
            continue

        start_raw = g.get("startDate")
        try:
            start_dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        is_tbd = g.get("startTimeTBD", False)
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        day_key = local_dt.date().isoformat()
        slot = time_slot_for(local_dt, is_tbd)

        home_rank = rank_lookup.get(home_team)
        away_rank = rank_lookup.get(away_team)

        odds = match_odds_for_game(home_team, away_team, odds_rows, team_cache, row_claims)
        if previous_odds_by_id:
            odds = carry_forward_odds(odds, previous_odds_by_id.get(game_id))

        game_entry = {
            "id": game_id,
            "start_time": start_raw,
            "start_time_tbd": is_tbd,
            "home_team": home_team,
            "home_conference": g.get("homeConference"),
            "home_rank": home_rank,
            "home_record": record_lookup.get(home_team, "0-0"),
            "away_team": away_team,
            "away_conference": g.get("awayConference"),
            "away_rank": away_rank,
            "away_record": record_lookup.get(away_team, "0-0"),
            "matchup_score": matchup_score(home_rank, away_rank),
            "channel": outlet or "Not on Main TV",
            "venue": g.get("venue"),
            "neutral_site": g.get("neutralSite", False),
            "odds": odds,
            "is_nebraska": is_nebraska,
        }
        days.setdefault(day_key, {}).setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    log(f"  {skipped_no_tv} games skipped (not on a main channel)")

    # Rank spans the WHOLE WEEK, across every day in it, not just
    # whichever time slot or day a game lands in.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="cfb", season=year, week=week, gemini_key=gemini_key)

    day_list = []
    for day_key in sorted(days.keys()):
        slots_for_day = days[day_key]
        time_slots = []
        for slot_name in TIME_SLOT_ORDER:
            if slot_name not in slots_for_day:
                continue
            # "Best ranking of each group": sort each window's games by
            # matchup_score ascending, so the most marquee game in that
            # window (lowest combined AP rank) leads.
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


def build(year, week_start, cfbd_key, sharp_key, channels, gemini_key=None, num_weeks=2, previous_odds_by_id=None):
    """Build `num_weeks` consecutive weeks starting at week_start (default:
    this week + next week) and wrap them into the full output payload."""
    weeks = []
    rankings_cache = {}
    records_cache = {}
    for offset in range(num_weeks):
        weeks.append(build_week(year, week_start + offset, cfbd_key, sharp_key, channels, gemini_key, rankings_cache=rankings_cache, records_cache=records_cache, previous_odds_by_id=previous_odds_by_id))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "main_channels": sorted(channels),
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": weeks,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NCAAF betting dashboard JSON.")
    p.add_argument("--year", type=int, default=None, help="Season year (default: auto)")
    p.add_argument("--week", type=int, default=None, help="Starting CFBD week number (default: auto from Aug 22 start); this week and the following week are both built")
    p.add_argument("--num-weeks", type=int, default=2, help="How many consecutive weeks to build starting at --week (default: 2)")
    p.add_argument("--out", default=None, help="Output path (default: data/ncaaf_dashboard.json)")
    return p.parse_args()


def main():
    args = parse_args()

    cfbd_key = os.environ.get("CFBD_API_KEY")
    sharp_key = os.environ.get("SHARPAPI_KEY")
    if not cfbd_key:
        sys.exit("Missing CFBD_API_KEY environment variable (get one at collegefootballdata.com/key)")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    gemini_key = os.environ.get("GEMINI_KEY")
    if not gemini_key:
        log("GEMINI_KEY not set -- building without Gemini predictions.")

    today = datetime.now(timezone.utc).date()
    year = args.year or SEASON_YEAR_DEFAULT
    if args.week is not None:
        week, preseason = args.week, False
    else:
        week, preseason = derive_week(today, year)
        if preseason:
            log(f"Today ({today}) is before the {year} week-1 start ({WEEK1_START}); defaulting to week 1.")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "ncaaf_dashboard.json")
    out_path = os.path.abspath(out_path)

    existing_data = load_existing_dashboard(out_path)

    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    output = build(year, week, cfbd_key, sharp_key, MAIN_CHANNELS, gemini_key,
                    num_weeks=args.num_weeks, previous_odds_by_id=previous_odds_by_id)

    # Record which week THIS build resolved as "current" -- College/NFL use
    # this (plus current_week + 1) to decide what to display, instead of
    # re-deriving "current" client-side from individual game timestamps.
    # It's just whatever `week` above ended up being (either --week, or
    # derive_week()'s date-based answer), so it always reflects the same
    # source of truth the actual fetch used.
    output["current_week"] = week

    # Never drop old weeks -- merge today's freshly-built weeks on top of
    # whatever weeks were already on disk instead of replacing the file
    # wholesale, so lines/scores/predictions from every past week stay
    # available (Picks shows all of them; College only shows current_week
    # and current_week + 1).
    all_weeks = merge_weeks(existing_data, output["weeks"])
    output["weeks"] = all_weeks

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    week_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across {len(week_nums)} week(s) total ({week_nums}) to {out_path}")


if __name__ == "__main__":
    main()
