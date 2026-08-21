#!/usr/bin/env python3
"""
NCAA Men's College Basketball (NCAAMB) Betting Dashboard builder.

Pulls the day's schedule + broadcast + team records + AP Top 25 rank
straight off ESPN's public (unofficial, no-key-required) men's college
basketball scoreboard API (each competitor in the scoreboard payload
already carries its own `curatedRank.current` AP rank -- no separate
rankings-endpoint call needed, unlike build_ncaaf_dashboard.py, which has
to fetch a week's poll separately), then attaches DraftKings / FanDuel
spread + moneyline odds from SharpAPI (via common.py). Exports everything
to data/ncaamb_dashboard.json for the static ncaamb.html front-end.

Like MLB (and NBA), college basketball plays most days of the week (no
real "week N" concept), so this script moves one calendar day at a time
instead of one week at a time -- see build_mlb_dashboard.py's own module
docstring for the full "week"=YYYYMMDD-int rationale, which this script
follows exactly.

Like build_ncaaf_dashboard.py, ONLY games broadcasting on a main national/
cable channel make the board (see MAIN_CHANNELS below) -- everything else
(ESPNU/ESPN+/conference-network/streaming-only games, which is most of a
given day's slate) is filtered out.

The matchup score blends the same three components build_ncaaf_dashboard.py
uses -- 50% combined AP Top 25 rank, 25% combined win-rank (teams ranked by
this season's record), 25% posted spread -- normalized 0-100 across the
whole day before blending.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/build_ncaamb_dashboard.py
    python scripts/build_ncaamb_dashboard.py --start-date 2026-11-15 --num-days 2
"""

import argparse
import os
import re
import sys
import json
from datetime import date, datetime, timedelta, timezone
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

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
REQUEST_TIMEOUT = 20

# groups=50 = Division I (all conferences); without it, and without a high
# limit, the scoreboard endpoint silently truncates to a top-25-ish subset
# of the day's games instead of the full D-I slate -- same convention
# build_ncaaf_dashboard.py uses (groups=80 there is FBS's own group id).
DIVISION_I_GROUP = "50"
SCOREBOARD_LIMIT = 500

NUM_DAYS_DEFAULT = 3  # "yesterday" + "today" + "tomorrow", same window MLB/NBA use.

# "Main channels" = national broadcast + flagship cable, same set
# build_ncaaf_dashboard.py uses. Games on ESPNU, ESPN+, conference
# networks (ACCN/SECN/BTN/etc.), streaming-only, etc. are filtered out.
MAIN_CHANNELS = {"ABC", "CBS", "NBC", "FOX", "ESPN", "ESPN2", "FS1"}

# ESPN reports curatedRank.current = 99 for an unranked team rather than
# omitting the field -- treat that (or anything past a real Top 25 spot)
# as "not ranked" rather than as a very bad-but-real rank.
UNRANKED_CURATED_RANK = 99

# A team with no AP-poll ranking gets this value -- one worse than the
# worst possible AP Top 25 rank -- so it never outranks a team that's
# actually ranked. Kept separate from win-rank's own "unranked" value
# below since these two components are normalized independently before
# being blended (same convention build_ncaaf_dashboard.py uses).
UNRANKED_VALUE = 26

# A team with no games played yet (or missing a record) gets this
# win-rank value -- one worse than the number of D-I teams that could
# realistically appear on one day's board -- so it never outranks a team
# that actually has a record.
UNRANKED_WIN_RANK = 200


def current_season_year():
    """Auto-derive the NCAAMB season's starting year from today's date,
    so this never needs a manual update when a new season starts. A
    college basketball season is named for the year it tips off in
    (November); games played January-July belong to the season that
    started the PREVIOUS calendar year (the season runs into April, with
    nothing else on the schedule until the following November). Games
    from August onward (exhibitions can start in late October, but
    August is a safe off-season cutover with nothing scheduled) belong
    to the season starting that same year -- mirrors
    build_ncaaf_dashboard.py's own current_season_year()."""
    today = datetime.now(timezone.utc).date()
    return today.year if today.month >= 8 else today.year - 1


# ---------------------------------------------------------------------------
# ESPN calls
# ---------------------------------------------------------------------------

def get_scoreboard(date_str):
    resp = requests.get(
        ESPN_SCOREBOARD_URL,
        params={"dates": date_str, "groups": DIVISION_I_GROUP, "limit": SCOREBOARD_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def broadcast_names(event):
    """All national broadcast name strings for a game, RAW (not yet split
    or joined) -- across every ESPN payload shape that can carry them.
    Used both for display (broadcast_label) and for the main-channel
    filter (on_main_channel, which further splits each string -- see
    there for why). Mirrors build_ncaaf_dashboard.py's own
    broadcast_names()."""
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
    names = broadcast_names(event)
    return ", ".join(names) if names else None


_CHANNEL_SPLIT_RE = re.compile(r"[/,]")


def _channel_tokens(names):
    """Split each raw broadcast name into individual channel tokens (ESPN
    sometimes packs more than one network into a single string, e.g.
    "ESPN2/ACCNX") -- see build_ncaaf_dashboard.py's identical helper."""
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
    """Extract (wins, losses, summary) from an ESPN scoreboard competitor's
    `records` list, or (None, None, None) if no overall record is present
    yet (e.g. before the season opener)."""
    for rec in competitor.get("records", []):
        if rec.get("type") == "total" or rec.get("name") == "overall":
            summary = rec.get("summary")
            if not summary:
                continue
            parts = summary.split("-")
            try:
                wins, losses = int(parts[0]), int(parts[1])
                return wins, losses, summary
            except (ValueError, IndexError):
                return None, None, summary
    return None, None, None


def _curated_rank(competitor):
    """AP-style current rank for this competitor, straight off the
    scoreboard payload's own curatedRank.current field, or None if
    unranked (ESPN reports 99 for unranked rather than omitting the
    field)."""
    rank = (competitor.get("curatedRank") or {}).get("current")
    if rank is None or rank >= UNRANKED_CURATED_RANK:
        return None
    return rank


# ---------------------------------------------------------------------------
# Matchup ranking -- same 3-component blend build_ncaaf_dashboard.py uses
# ---------------------------------------------------------------------------

def build_win_rank_lookup(team_records):
    """Rank every team with a known record by win percentage (rank 1 =
    best record on the board). Ties broken by raw win total, then by team
    name for a stable, deterministic order. `team_records` is
    {team_name: (wins, losses)}. Returns {team_name: rank}."""
    entries = []
    for team, (wins, losses) in team_records.items():
        games_played = wins + losses
        pct = wins / games_played if games_played else -1
        entries.append((team, pct, wins, team))
    entries.sort(key=lambda e: (-e[1], -e[2], e[3]))
    return {team: i + 1 for i, (team, pct, wins, _) in enumerate(entries)}


def _home_spread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line


def matchup_components(home_rank, away_rank, home_win_rank, away_win_rank, odds):
    """Per-game (ap_component, win_component, spread_component) tuple,
    collected across the whole day and normalized/blended at once -- see
    build_ncaaf_dashboard.py's identical helper."""
    ap_component = None
    if home_rank is not None or away_rank is not None:
        ap_component = (home_rank or UNRANKED_VALUE) + (away_rank or UNRANKED_VALUE)

    win_component = None
    if home_win_rank is not None or away_win_rank is not None:
        win_component = (home_win_rank or UNRANKED_WIN_RANK) + (away_win_rank or UNRANKED_WIN_RANK)

    spread = _home_spread(odds)
    spread_component = abs(spread) if spread is not None else None

    return ap_component, win_component, spread_component


# ---------------------------------------------------------------------------
# Main build -- one calendar day at a time
# ---------------------------------------------------------------------------

def build_day(day, sharp_key, channels, gemini_key=None, previous_odds_by_id=None,
              previous_entries_by_id=None, started_game_ids=None):
    """Build a single day's worth of main-channel games. Returns a
    "week"-shaped dict (week=YYYYMMDD int, days=[<this single day>]) so
    it slots into the same merge_weeks()/front-end code the other
    dashboards use."""
    date_str = day.strftime("%Y%m%d")
    log(f"Fetching NCAAMB schedule for {date_str}...")
    scoreboard = get_scoreboard(date_str)
    events = scoreboard.get("events", [])
    log(f"  {len(events)} games")

    log("Fetching DraftKings/FanDuel NCAAMB odds from SharpAPI...")
    # SharpAPI's own league code for college basketball is "ncaab" (not
    # "ncaamb" -- that's just this project's own page/file naming).
    day_str = day.isoformat()
    odds_rows = fetch_all_odds(sharp_key, league="ncaab", markets=("spread", "moneyline"),
                                date_from=day_str, date_to=day_str)
    log(f"  {len(odds_rows)} odds rows returned")
    team_cache = {}
    row_claims = {}

    slots = {}  # slot_name -> list of game entries
    all_games = []
    team_records = {}  # {team_name: (wins, losses)}
    skipped_no_tv = 0
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

        if not on_main_channel(event, channels):
            skipped_no_tv += 1
            continue

        outlet = broadcast_label(event)

        start_raw = event.get("date")
        try:
            start_dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        status = event.get("status", {}).get("type", {})
        is_tbd = "TBD" in (event.get("shortName") or "")
        local_dt = start_dt_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        slot = time_slot_for(local_dt, is_tbd)

        home_team = home["team"]["displayName"]
        away_team = away["team"]["displayName"]

        home_wins, home_losses, home_record = _parse_espn_record(home)
        away_wins, away_losses, away_record = _parse_espn_record(away)
        if home_wins is not None:
            team_records[home_team] = (home_wins, home_losses)
        if away_wins is not None:
            team_records[away_team] = (away_wins, away_losses)

        home_rank = _curated_rank(home)
        away_rank = _curated_rank(away)

        game_id = event.get("id")
        gid_str = str(game_id)
        already_started = gid_str in started_game_ids
        previous_entry = previous_entries_by_id.get(gid_str)

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
            "home_abbr": home["team"].get("abbreviation"),
            "home_rank": home_rank,
            "home_record": home_record or "0-0",
            "away_team": away_team,
            "away_abbr": away["team"].get("abbreviation"),
            "away_rank": away_rank,
            "away_record": away_record or "0-0",
            "matchup_score": None,  # filled in below, once every game's components are known
            "channel": outlet or "Not on Main TV",
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        if already_started and previous_entry is not None and previous_entry.get("gemini_prediction"):
            game_entry["gemini_prediction"] = previous_entry["gemini_prediction"]
        slots.setdefault(slot, []).append(game_entry)
        all_games.append(game_entry)

    log(f"  {skipped_no_tv} games skipped (not on a main channel)")

    # Blend 50% combined AP Top 25 rank + 25% combined win-rank + 25%
    # posted spread -- each normalized 0-100 across the whole day before
    # blending, same pattern build_ncaaf_dashboard.py uses per week.
    win_rank_lookup = build_win_rank_lookup(team_records)
    ap_components, win_components, spread_components = [], [], []
    for g in all_games:
        home_win_rank = win_rank_lookup.get(g["home_team"])
        away_win_rank = win_rank_lookup.get(g["away_team"])
        a, w, s = matchup_components(g["home_rank"], g["away_rank"], home_win_rank, away_win_rank, g["odds"])
        ap_components.append(a)
        win_components.append(w)
        spread_components.append(s)
    ap_norm = normalize_minmax(ap_components)
    win_norm = normalize_minmax(win_components)
    spread_norm = normalize_minmax(spread_components)
    for g, an, wn, sn in zip(all_games, ap_norm, win_norm, spread_norm):
        g["matchup_score"] = round(100 * (0.5 * an + 0.25 * wn + 0.25 * sn), 1)

    # Rank spans the WHOLE DAY, not just whichever time slot a game lands
    # in -- computed here, once per day, before games get split up into
    # time_slots below.
    assign_matchup_ranks(all_games)

    attach_gemini_predictions(all_games, sport="ncaamb", season=day.year,
                               week=day.isoformat(), gemini_key=gemini_key,
                               skip_ids=frozen_prediction_skip_ids)

    time_slots = []
    for slot_name in TIME_SLOT_ORDER:
        if slot_name not in slots:
            continue
        games_sorted = sorted(slots[slot_name], key=lambda x: x["matchup_score"])
        best_score = games_sorted[0]["matchup_score"] if games_sorted else None
        for i, g in enumerate(games_sorted):
            g["is_slot_pick"] = (i == 0)
        time_slots.append({
            "slot": slot_name,
            "best_matchup_score": best_score,
            "pick_reason": "ap_win_spread" if games_sorted else None,
            "games": games_sorted,
        })

    day_dict = {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "game_count": len(all_games),
        "time_slots": time_slots,
    }

    week_num = int(day.strftime("%Y%m%d"))
    return {
        "week": week_num,
        "total_games": len(all_games),
        "days": [day_dict],
    }


def build(sharp_key, channels, gemini_key=None, start_date=None, num_days=NUM_DAYS_DEFAULT,
          previous_odds_by_id=None, previous_entries_by_id=None, started_game_ids=None):
    """Build `num_days` consecutive calendar days centered on `start_date`
    (default: today, in DISPLAY_TIMEZONE) and wrap them into the full
    output payload, "week"-shaped the same way CFB/NFL/MLB/NBA are."""
    if start_date is None:
        start_date = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    window_start = start_date - timedelta(days=(num_days - 1) // 2)

    days_out = []
    for offset in range(num_days):
        d = window_start + timedelta(days=offset)
        days_out.append(build_day(d, sharp_key, channels, gemini_key, previous_odds_by_id=previous_odds_by_id,
                                   previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": current_season_year(),
        "main_channels": sorted(channels),
        "display_timezone": DISPLAY_TIMEZONE,
        "weeks": days_out,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Build the NCAAMB betting dashboard JSON.")
    p.add_argument("--start-date", default=None, help="The 'today' date to center the build window on, YYYY-MM-DD (default: today)")
    p.add_argument("--num-days", type=int, default=NUM_DAYS_DEFAULT,
                    help=f"How many consecutive days to build, centered on --start-date (default: {NUM_DAYS_DEFAULT})")
    p.add_argument("--out", default=None, help="Output path (default: data/ncaamb_dashboard.json)")
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

    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    resolved_today = start_date or datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(script_dir, "..", "data", "ncaamb_dashboard.json")
    out_path = os.path.abspath(out_path)
    scores_path = args.scores or os.path.join(script_dir, "..", "data", "scores.json")
    scores_path = os.path.abspath(scores_path)

    existing_data = load_existing_dashboard(out_path)
    previous_odds_by_id = load_previous_odds_by_game(out_path)
    if previous_odds_by_id:
        log(f"Loaded odds for {len(previous_odds_by_id)} game(s) from the previous build "
            f"to carry forward if today's fetch comes back blank for any of them.")

    previous_entries_by_id = load_previous_game_entries(out_path)
    started_game_ids = load_started_game_ids(scores_path, "ncaamb")
    if started_game_ids:
        log(f"{len(started_game_ids)} NCAAMB game(s) already have a score recorded in {scores_path} -- "
            f"freezing odds and Gemini predictions for those instead of updating them.")

    output = build(sharp_key, MAIN_CHANNELS, gemini_key, start_date=resolved_today, num_days=args.num_days,
                    previous_odds_by_id=previous_odds_by_id,
                    previous_entries_by_id=previous_entries_by_id, started_game_ids=started_game_ids)
    fresh_day_nums = [w["week"] for w in output["weeks"]]

    output["current_week"] = int(resolved_today.strftime("%Y%m%d"))
    output["weeks"] = merge_weeks(existing_data, output["weeks"])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    total_games = sum(w["total_games"] for w in output["weeks"])
    day_nums = [w["week"] for w in output["weeks"]]
    log(f"Wrote {total_games} games across {len(day_nums)} day(s) total ({day_nums}) to {out_path}; "
        f"freshly built this run: {fresh_day_nums}")


if __name__ == "__main__":
    main()
