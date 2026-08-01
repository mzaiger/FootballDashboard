# Sports Betting Dashboards

Two daily-refreshed betting dashboards, College Football and NFL, each
showing this week's games grouped by day and then by kickoff window
(Morning / Noon / Afternoon / Evening / Late Night), ranked within each
window by best matchup, with **DraftKings** and **FanDuel** spreads +
moneylines attached to each game.

| | College Football | NFL |
|---|---|---|
| Page | `index.html` | `nfl.html` |
| Data | `data/dashboard.json` | `data/nfl_dashboard.json` |
| Builder | `scripts/build_dashboard.py` | `scripts/build_nfl_dashboard.py` |
| Schedule/TV source | [CollegeFootballData.com](https://collegefootballdata.com/key) (CFBD) | ESPN's public scoreboard API (no key needed) |
| Matchup ranking | Combined AP Top 25 rank of both teams | Smaller DraftKings/FanDuel point spread (closer game = better) |
| Odds | [SharpAPI](https://sharpapi.io) (DraftKings + FanDuel) | same |

Both pages share a nav bar at the top, and both builders share matching /
odds-fetching / time-slot-bucketing logic from `scripts/common.py` so a fix
in one (like the odds-matching hardening below) benefits both.

- **Automation**: `.github/workflows/update-dashboard.yml` — runs both
  scripts daily via GitHub Actions and commits the refreshed JSON files
- **Front-end**: static HTML, no build step, no JS framework

## How grouping and ranking work

Games are grouped **by day**, then **by kickoff window**:

| Window | Range (Central) |
|---|---|
| Morning | before 12:00 PM |
| Noon | 12:00 PM – 2:59 PM |
| Afternoon | 3:00 PM – 5:59 PM |
| Evening | 6:00 PM – 8:59 PM |
| Late Night | 9:00 PM and later |
| Time TBD | kickoff not yet announced |

Windows are bucketed in **US/Central**. Change `DISPLAY_TIMEZONE` in
`scripts/common.py` (e.g. to `"America/New_York"`) if you'd rather bucket by
Eastern or another zone — both sports pick this up automatically since they
share the constant. A window only shows up in the output if it actually has
a game in it — no empty sections.

**CFB matchup ranking**: `matchup_score = home_team_AP_rank +
away_team_AP_rank` (unranked teams count as 26). Lower score = more marquee
matchup, so a #1 vs #2 game (score 3) leads a #15-vs-unranked game (score
41) within the same window.

**NFL matchup ranking**: the NFL doesn't have an AP-poll equivalent this
early in a season, so `matchup_score` uses the market's own judgment
instead — the smaller of DraftKings'/FanDuel's point spread, i.e. how close
Vegas expects the game to be. A pick-em game (spread ~0) leads a 14-point
blowout within the same window. Games with no spread posted yet sort last.

"Main channels" for CFB is currently `ABC, CBS, NBC, FOX, ESPN, ESPN2, FS1`
— edit the `MAIN_CHANNELS` set near the top of `scripts/build_dashboard.py`
to add or remove networks. The NFL page doesn't need an equivalent filter:
ESPN's scoreboard API only returns games that already have a real broadcast
assignment (CBS, FOX, NBC, ESPN, ABC, Amazon, Netflix, NFL Network, Peacock).

## Omaha / Lincoln regional game (NFL page) — read this before trusting it

You asked for the actual per-market regional coverage (which specific CBS
or FOX game airs in the Omaha/Lincoln DMA) pulled from 506sports.com's maps.
Here's what I found trying to build that:

**506sports.com's per-game map pages (`nfl.php?yr=X&wk=Y`) build their
market-by-market data client-side in JavaScript.** A plain HTTP GET (what
`requests`, `curl`, or any non-browser script does) returns an empty page
shell — I confirmed this directly, fetching several different week pages
and getting nothing back, while the same tool fetched 506sports' *season
schedule index* page (`506sports.com/nfl/`) just fine, because that page's
content is plain server-rendered HTML. The per-market station lists (e.g.
"KETV/KMTV/KPTM — Omaha") only showed up because Google's crawler executes
JavaScript when indexing and I could see fragments of it in search results
— but a script can't do that without a real browser engine.

**What this means for automation**: a `requests`-based Python script (what
you asked for, and what runs in GitHub Actions without extra setup) cannot
reliably scrape 506sports' actual regional routing. The two honest paths
forward:

1. **Headless-browser scraper (accurate, heavier)** — use Playwright to
   actually render the page's JavaScript and read the DOM/legend it
   produces. This works in GitHub Actions but adds a real browser
   installation to the workflow (bigger, slower runs, more moving parts to
   break when 506sports changes their page). I didn't build this — happy to
   if you want it, but wanted to flag the tradeoff first rather than quietly
   add a much heavier workflow.
2. **Heuristic guess (what's implemented now, lightweight but unofficial)**
   — `regional_pick_omaha_lincoln` in the NFL builder: when a CBS or FOX
   window has more than one game, it guesses whichever game features the
   **Kansas City Chiefs**, then the **Denver Broncos** (both have
   historically been the closest teams to that market, since neither Omaha
   nor Lincoln has a home NFL team). It's clearly labeled "unofficial" on
   the page with a link back to 506sports.com to verify. This is *not*
   pulled from any real coverage map — it's a plausible guess based on
   geography, nothing more.

If you want the accurate version, say the word and I'll build the
Playwright path — just flagging that it's a meaningfully bigger lift (and a
heavier, slower CI job) than the rest of this project.

## 1. Get your API keys

- CFBD: [collegefootballdata.com/key](https://collegefootballdata.com/key) — free, sign up with email
- SharpAPI: [sharpapi.io](https://sharpapi.io) — free tier covers DraftKings + FanDuel at 12 requests/min
- ESPN's scoreboard API needs no key (public, unofficial, no auth)

## 2. Run it locally

```bash
pip install -r requirements.txt

export CFBD_API_KEY="your_cfbd_key"
export SHARPAPI_KEY="your_sharpapi_key"

python scripts/build_dashboard.py       # CFB -> data/dashboard.json
python scripts/build_nfl_dashboard.py   # NFL -> data/nfl_dashboard.json (only needs SHARPAPI_KEY)
```

Then open `index.html` or `nfl.html` in a browser (or run
`python -m http.server` from the project root and visit
`http://localhost:8000`) — opening the files directly with `file://` also
works since the fetch is a relative path in the same folder.

Optional flags:

```bash
python scripts/build_dashboard.py --year 2026 --week 3       # force a specific CFB week
python scripts/build_nfl_dashboard.py --year 2026 --week 2   # force a specific NFL week
python scripts/build_nfl_dashboard.py --season-type 1        # 1=preseason, 2=regular, 3=postseason
python scripts/build_dashboard.py --out /tmp/test.json       # write somewhere else
```

If no `--week` is given: CFB auto-derives the current week from the
season-1 kickoff date (`WEEK1_START = Aug 22, 2026` in
`scripts/build_dashboard.py`); NFL just asks ESPN what week it currently is.

## 3. Deploy: GitHub Pages + daily Actions run

1. Push this folder to a GitHub repo (e.g. `mzaiger/cfb-betting-dashboard`).
2. **Add secrets**: repo → Settings → Secrets and variables → Actions → New repository secret
   - `CFBD_API_KEY`
   - `SHARPAPI_KEY`
3. **Enable Pages**: repo → Settings → Pages → Source: `Deploy from a branch` → branch `main`, folder `/ (root)`.
4. **Enable Actions writes**: the workflow already requests `contents: write`
   permission, but if your org has a stricter default, go to Settings →
   Actions → General → Workflow permissions → "Read and write permissions".
5. Trigger the first run manually: Actions tab → "Update Betting Dashboards" → Run workflow.
   After that it runs automatically every day at 10:00 UTC (edit the `cron`
   line in `.github/workflows/update-dashboard.yml` to change the time).

Your site will be live at `https://<username>.github.io/<repo>/` (CFB) and
`https://<username>.github.io/<repo>/nfl.html` (NFL).

## Project structure

```
cfb-betting-dashboard/
├── index.html                       # CFB front-end
├── nfl.html                         # NFL front-end
├── requirements.txt
├── scripts/
│   ├── common.py                    # shared: SharpAPI fetch/matching, time-slot bucketing
│   ├── build_dashboard.py           # CFBD + SharpAPI -> data/dashboard.json
│   └── build_nfl_dashboard.py       # ESPN + SharpAPI -> data/nfl_dashboard.json
├── data/
│   ├── dashboard.json               # generated CFB output (placeholder sample checked in)
│   └── nfl_dashboard.json           # generated NFL output (placeholder sample checked in)
└── .github/workflows/
    └── update-dashboard.yml         # daily cron + manual trigger, builds both
```

## Notes / things worth knowing

- **Team name matching**: CFBD/ESPN and SharpAPI don't always use identical
  team name strings (e.g. mascots vs. school names only, "USC" vs "USC
  Trojans"). `match_odds_for_game()` in `common.py` does whole-word fuzzy
  matching with a few specific safeguards — it was hardened after testing
  turned up two real bugs: (1) a raw substring check let one real odds row
  attach itself to two different games sharing a home team, fixed by
  tracking and rejecting any SharpAPI row claimed by more than one game;
  (2) naive word matching let unrelated schools that share a first word
  cross-match (Texas ↔ Texas A&M, Miami ↔ Miami (OH), Ohio ↔ Ohio State),
  fixed with a short list of disqualifying words. If a book hasn't posted
  lines yet for a game, the card just shows "Odds not yet posted" instead
  of guessing.
- **Rate limits**: SharpAPI's free tier is 12 requests/minute. Both scripts
  pull the full week's odds in one paginated sweep (a handful of requests)
  rather than one call per game, so a full week's slate stays well under that.
- **AP Top 25 only** (CFB): rankings use the AP poll. Swap `poll_name` in
  `build_rank_lookup()` to `"Coaches Poll"` or (once available) the CFP
  rankings if you'd rather rank by those instead.
- **ESPN's API is unofficial** (NFL): it's the same public endpoint ESPN's
  own site and app use, widely relied on by hobby projects, but it's not a
  documented/supported product — it could change without notice. No API key
  is required.
