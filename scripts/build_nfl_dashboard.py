#!/usr/bin/env python3
"""
NFL Betting Dashboard builder.
Pulls this week's schedule + national broadcast network from ESPN's public
(unofficial, no-key-required) scoreboard API, then attaches DraftKings /
FanDuel spread + moneyline odds from SharpAPI (via common.py). Exports
everything to data/nfl_dashboard.json for the static nfl.html front-end.

When --week is omitted the script asks ESPN (no week param) which season /
week is current and follows ESPN's own calendar -- preseason in August,
regular season Sept-Jan, playoffs Jan-Feb -- instead of relying on hardcoded
Week 1 dates. See autodetect_season() below.

IMPORTANT LIMITATION -- read before trusting the "regional_pick" field:
This script does NOT scrape 506sports.com's regional coverage maps. Those
pages build their market-by-market data client-side in JavaScript (a plain
HTTP GET returns an empty shell -- confirmed by hand while building this),
so a requests-based script can't read them, and neither could a plain
Python script Marc runs elsewhere. See README.md for what was tried and
what a real fix would require (a headless-browser scraper).
Instead, regional_pick is a HEURISTIC guess at what airs in the Omaha /
Lincoln, NE market: when a CBS or FOX window has more than one game, it
picks whichever game features the Kansas City Chiefs, then the Denver
Broncos (both have historically been the closest teams to that market).
This is NOT authoritative -- always cross-check at 506sports.com before
relying on it.

Env vars required:
    SHARPAPI_KEY - key from https://sharpapi.io
    (no key needed for ESPN's public scoreboard endpoint)

Usage:
    python scripts/buildnfldashboard.py
    python scripts/buildnfldashboard.py --week 1 --year 2026 --season-type 2
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
    TIMESLOTORDER,
    fetchallodds,
    log,
    matchoddsfor_game,
    timeslotfor,
)

Config

ESPNSCOREBOARDURL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
REQUEST_TIMEOUT = 20
seasontype: 1=preseason, 2=regular season, 3=postseason
SEASONTYPEDEFAULT = 2
SEASONYEARDEFAULT = 2026
Last-resort fallback only. The primary week/season resolution is
autodetect_season(), which reads ESPN's live calendar (no hardcoded
dates). WEEK1START / deriveweek() are used ONLY when autodetect can't
help at all -- e.g. the ESPN request fails, or today falls outside the
league year ESPN returned (the few days before preseason kicks off).
2026 regular-season Week 1 runs Sept 9-15 (confirmed against ESPN's
calendar). Mirrors builddashboard.py's WEEK1START/derive_week for CFB.
WEEK1_START = date(2026, 9, 9)
Regional-pick heuristic priority for the Omaha/Lincoln, NE market (no home
NFL team). Checked in order; first match in a multi-game window wins.
REGIONALTEAMPRIORITY = ["Kansas City Chiefs", "Denver Broncos"]
"Main channels" here just means "has a national broadcast at all" -- ESPN's
API already only returns games with a real network/streaming assignment
(CBS, FOX, NBC, ESPN, ABC, Amazon, Netflix, NFL Network, Peacock), so
there's no separate filter needed the way CFB needed one for ESPNU/ACCN/etc.

ESPN calls

def getscoreboard(year, week, seasontype):
    resp = requests.get(
        ESPNSCOREBOARDURL,
        params={"dates": year, "week": week, "seasontype": season_type, "limit": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raiseforstatus()
    return resp.json()

def broadcast_label(event):
    """Join all national broadcast names for a game, e.g. 'CBS' or 'FOX/CBS'."""
    names = []
    for b in event.get("broadcasts", []):
        for n in b.get("names", []):
            if n not in names:
                names.append(n)
    if names:
        return "/".join(names)
    # geoBroadcasts is the fallback some events use instead of broadcasts[]
    for gb in event.get("geoBroadcasts", []):
        short = gb.get("media", {}).get("shortName")
        if short and short not in names:
            names.append(short)
    return "/".join(names) if names else None

Matchup ranking

def matchupscore(dkspread, fd_spread):
    """
    Lower score = closer / more competitive game = better matchup for a
    betting dashboard. Unlike CFB (which has AP rankings to lean on), the
    NFL doesn't have a clean, universally-agreed "how good is this team"
    number this early in a season, so this uses the market's own judgment
    instead: the smaller the point spread, the more competitive Vegas
    expects the game to be. Falls back to FanDuel's spread if DraftKings
    hasn't posted one yet; games with no spread posted from either book
    sort last (they're usually just further out from kickoff).
    """
    spread = dkspread if dkspread is not None else fd_spread
    if spread is None:
        return 999
    return abs(spread)

def homespread(odds):
    line = odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")
    if line is None:
        line = odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")
    return line

def dkhome_spread(odds):
    return odds.get("draftkings", {}).get("spread", {}).get("home", {}).get("line")

def fdhome_spread(odds):
    return odds.get("fanduel", {}).get("spread", {}).get("home", {}).get("line")

Week / season autodetection (follows ESPN's live calendar)

def deriveweek(today, seasontype):
    """
    Deterministically pick a week when --week isn't given, instead of
    trusting ESPN's undocumented "current week" inference. Only meaningful
    for regular season (seasontype 2) -- preseason/postseason callers should
    pass --week explicitly. Now used ONLY as the last-resort fallback inside
    autodetect_season() when ESPN's calendar can't be read at all.
    """
    if seasontype != SEASONTYPE_DEFAULT:
        return 1, False
    if today = today_iso:
                if best is None or start  season_type={st}, week={wk}")
        return st, wk

    # 3) Before the league year: preview the next upcoming window.
    st, wk, label = nextupcomingwindow(calendar, todayiso)
    if st is not None and wk is not None:
        log(f"  before league year; next window {label!r} -> season_type={st}, week={wk}")
        return st, wk

    # 4) Date-based last resort.
    log("  no usable ESPN calendar; using date-based fallback")
    fallbackweek, preseason = deriveweek(
        datetime.now(timezone.utc).date(), SEASONTYPEDEFAULT
    )
    fallbacktype = 1 if preseason else SEASONTYPE_DEFAULT
    log(f"  fallback -> seasontype={fallbacktype}, week={fallback_week}")
    return fallbacktype, fallbackweek

Regional pick heuristic (Omaha / Lincoln, NE)

def pickregionalgame(gamesinwindow):
    """
    Given all games sharing one network + kickoff window, guess which one
    the Omaha/Lincoln market gets. Only meaningful when there's more than
    one game in the window (if there's only one, everyone gets it -- no
    guess needed).
    """
    if len(gamesinwindow)  list of game entries
    days = {}
    skippednobroadcast = 0

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

        outlet = broadcast_label(event)
        if not outlet:
            skippednobroadcast += 1
            continue

        start_raw = event.get("date")
        try:
            startdtutc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue

        status = event.get("status", {}).get("type", {})
        is_tbd = bool(status.get("isTBDFlex")) or "TBD" in (event.get("shortName") or "")

        localdt = startdtutc.astimezone(ZoneInfo(DISPLAYTIMEZONE))
        daykey = localdt.date().isoformat()
        slot = timeslotfor(localdt, istbd)

        home_team = home["team"]["displayName"]
        away_team = away["team"]["displayName"]
        odds = matchoddsforgame(hometeam, awayteam, oddsrows, teamcache, rowclaims)
        dkspread = dkhomespread(odds)
        fdspread = fdhomespread(odds)

        game_entry = {
            "id": event.get("id"),
            "starttime": startraw,
            "starttimetbd": is_tbd,
            "hometeam": hometeam,
            "home_abbr": home["team"].get("abbreviation"),
            "awayteam": awayteam,
            "away_abbr": away["team"].get("abbreviation"),
            "matchupscore": matchupscore(dkspread, fdspread),
            "channel": outlet,
            "venue": comp.get("venue", {}).get("fullName"),
            "neutral_site": comp.get("neutralSite", False),
            "odds": odds,
        }
        days.setdefault(daykey, {}).setdefault(slot, []).append(gameentry)

    log(f"  {skippednobroadcast} games skipped (no broadcast info yet)")

    day_list = []
    for day_key in sorted(days.keys()):
        slotsforday = days[day_key]
        time_slots = []
        for slotname in TIMESLOT_ORDER:
            if slotname not in slotsfor_day:
                continue
            games_sorted = sorted(
                slotsforday[slotname], key=lambda x: x["matchupscore"]
            )
            bestscore = gamessorted[0]["matchupscore"] if gamessorted else None
            # Regional pick heuristic: group this window's games by channel
            # (a "CBS" window and a "FOX" window are simultaneous but
            # separate -- Omaha gets one game from each, not one overall).
            by_channel = {}
            for g in games_sorted:
                by_channel.setdefault(g["channel"], []).append(g)
            regional_picks = []
            for channel, channelgames in bychannel.items():
                pick = pickregionalgame(channel_games)
                if pick:
                    regional_picks.append({
                        "channel": channel,
                        "game_id": pick["id"],
                        "matchup": f"{pick['awayteam']} @ {pick['hometeam']}",
                    })
            time_slots.append({
                "slot": slot_name,
                "bestmatchupscore": bestscore if bestscore != 999 else None,
                "regionalpicksomahalincoln": regionalpicks,
                "games": games_sorted,
            })
        weekdayname = date.fromisoformat(daykey).strftime("%A")
        daygamecount = sum(len(ts["games"]) for ts in time_slots)
        day_list.append({
            "date": day_key,
            "weekday": weekday_name,
            "gamecount": daygame_count,
            "timeslots": timeslots,
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": year,
        "week": week,
        "seasontype": seasontype,
        "displaytimezone": DISPLAYTIMEZONE,
        "totalgames": sum(d["gamecount"] for d in day_list),
        "regionalmarketnote": (
            "regionalpicksomaha_lincoln is an UNOFFICIAL heuristic guess "
            "(Kansas City Chiefs, then Denver Broncos, when a network window "
            "has multiple games) -- not scraped from an actual coverage map. "
            "Verify at https://506sports.com/nfl.php before relying on it."
        ),
        "days": day_list,
    }
    return output

def parse_args():
    p = argparse.ArgumentParser(description="Build the NFL betting dashboard JSON.")
    p.addargument("--year", type=int, default=SEASONYEAR_DEFAULT, help="Season year")
    p.add_argument(
        "--week",
        type=int,
        required=False,
        help="NFL week number (default: autodetect from ESPN's live calendar)",
    )
    p.add_argument(
        "--season-type",
        type=int,
        default=SEASONTYPEDEFAULT,
        help="1=preseason, 2=regular season, 3=postseason (used only when --week is given)",
    )
    p.addargument("--out", default=None, help="Output path (default: data/nfldashboard.json)")
    return p.parse_args()

def main():
    args = parse_args()

    sharpkey = os.environ.get("SHARPAPIKEY")
    if not sharp_key:
        sys.exit("Missing SHARPAPI_KEY environment variable (get one at sharpapi.io)")

    if args.week is None:
        seasontype, week = autodetectseason(args.year)
    else:
        seasontype = args.seasontype
        week = args.week

    output = build(args.year, week, seasontype, sharpkey)

    script_dir = os.path.dirname(os.path.abspath(file))
    outpath = args.out or os.path.join(scriptdir, "..", "data", "nfl_dashboard.json")
    outpath = os.path.abspath(outpath)
    os.makedirs(os.path.dirname(outpath), existok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    log(f"Wrote {output['totalgames']} games across {len(output['days'])} day(s) to {outpath}")

if name == "main":
    main()
