# Football Dashboard

Two daily-refreshed betting dashboards, College and NFL, each showing
**this week and next week's** games grouped by week, then by day, then by
kickoff window (Morning / Noon / Afternoon / Prime Time / Late Night), ranked
within each window by best matchup, with **DraftKings** and **FanDuel**
spreads + moneylines attached to each game.

Every page has a Tiles/Rows toggle in the nav -- Tiles is the original card
grid, Rows lays the same games out as a compact list. It defaults to Rows
on desktop and Tiles on mobile, but remembers whichever you pick after
that (stored in the browser, per device).

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

Every game with a posted DraftKings or FanDuel line also gets a **Gemini
Prediction Summary** — a collapsible button on the card showing an AI
confidence score, which expands to a winner pick, ATS pick, and a
five-sentence explanation. See "Gemini predictions" below for how it works.

- **Automation**: `.github/workflows/update-dashboard.yml` — runs both
  scripts daily via GitHub Actions and commits the refreshed JSON files
- **Front-end**: static HTML, no build step, no JS framework

## How grouping and ranking work

Games are grouped **by day**, then **by kickoff window**:

| Window | Range (Central) |
|---|---|
| Morning | before 11:00 AM |
| Noon | 11:00 AM – 1:59 PM |
| Afternoon | 2:00 PM – 4:59 PM |
| Prime Time | 5:00 PM – 8:59 PM |
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

## How weeks work

Each build produces a `weeks` array holding **two consecutive week numbers**
(this week + next week, per the `--num-weeks` flag described above) — both
pages show that whole array at once, under a "Week N" (or "P1"/"Weeks
N–M") heading per week.

**NFL preseason labeling**: ESPN numbers preseason weeks 1–4 the same as it
numbers regular-season weeks 1–4, so a bare "Week 1" would be ambiguous
between the preseason opener and the real season opener. To avoid that,
the NFL page and the Picks page render preseason weeks (`season_type: 1`
in `nfl_dashboard.json`) as **P1, P2, P3, P4**, and switch to plain **1, 2,
3, ... 18** once `--season-type 2` (regular season) is built. This is a
front-end label only — `week.week` in the JSON is still just the raw
integer either way; `formatWeekLabel()` in `picks-store.js` is what adds
the "P" prefix, shared by `nfl.html` and `picks.html`. CFB never sets
`season_type`, so its weeks always render as plain "Week N".

**What happens when the week rolls over**: nothing is deleted anywhere —
the *board* just moves forward with it. Each day's build (whether run
manually with `--week N` or via the daily GitHub Action with no `--week`
flag, which asks CFBD/ESPN for "the current week") regenerates
`dashboard.json` / `nfl_dashboard.json` from scratch with whatever the
*current* two weeks are. So once real Week 2 games exist, the file simply
no longer contains Week 1's games — they age out of the JSON, not because
anything explicitly wiped them, but because the build only ever asks for
"this week and next."

**Your picks are unaffected by this** — they're stored as one cookie per
game id (`pick_<sport>_<gameId>`, see `picks-store.js`), not tied to a
week number at all, and don't expire until ~210 days pass. A Week 1 pick's
cookie stays right where it is. What changes is that once Week 1's games
drop out of the JSON, `picks.html` has no game data left to match that
cookie against, so that pick simply stops appearing on the Picks page (it
isn't cleared, it's just invisible until/unless that same game id ever
reappears in a build). Practically: **make sure you've reviewed how last
week's picks did before the week rolls over** — the win/loss coloring on
the odds cells only renders while the game is still present in the current
dashboard JSON.

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

## Live scores

`scripts/fetch_scores.py` overlays live/final scores onto games already
present in `data/dashboard.json` and `data/nfl_dashboard.json` — it doesn't
touch odds, rankings, or Gemini predictions, so it's meant to run far more
often than the once-a-day builds (hourly is the intent) without hitting
SharpAPI's or Gemini's tighter rate limits.

- **Sources**: CollegeFootballData.com's `/games` endpoint for CFB (same
  `CFBD_API_KEY`), ESPN's public scoreboard for NFL (no key needed).
- **Output**: `data/scores.json`, shaped as
  `{"generated_at": ..., "cfb": {"<game_id>": {...}}, "nfl": {"<game_id>": {...}}}`.
  Each game entry is `home_score`, `away_score`, `status`
  (`"in_progress"` or `"final"`), and `status_detail` (e.g. `"Final"` or a
  live clock string from ESPN).
- **How the front-end uses it**: `picks-store.js` fetches this file on
  index.html/nfl.html/picks.html and merges it in client-side by game id —
  a small score badge appears next to each team once their game has
  started, plus a "LIVE · ..." status line while in progress. Games with no
  entry yet (not kicked off) render exactly as before, with no score line.
- **Picks lock automatically**: once a game's status is `final` (or
  `in_progress`/`live`/`halftime`), its pick buttons on the board disable —
  the pick you made still shows, but can no longer be changed. Once final,
  each odds cell (DraftKings/FanDuel, spread/moneyline) also gets colored
  green ("hit") or red ("miss") based on the actual final score, including
  proper push/tie handling.
- **Automation**: run it on its own schedule separate from the daily
  builds — e.g. a second GitHub Actions workflow on an hourly cron — since
  it's cheap (no SharpAPI or Gemini calls) and benefits from refreshing much
  more often than the odds/schedule data does.

## Gemini predictions

For every game that has at least one posted DraftKings or FanDuel line
(spread or moneyline), the build scripts call the Gemini API for a
current-season-only prediction: straight-up winner, ATS pick, a 1-100
confidence score, and a five-sentence explanation. That's attached to the
game as a `gemini_prediction` field, which the front-end renders as the
expandable "✨ Gemini Prediction Summary" button.

- **Model**: `gemini-3.6-flash`, called via `scripts/gemini_predictions.py`
  (imported by both `build_dashboard.py` and `build_nfl_dashboard.py`).
- **Caching**: `data/gemini_predictions_cache.json` keys each prediction by
  a hash of the matchup + that game's DK/FD spread and moneyline numbers.
  Re-running the same week with unchanged odds is always a cache hit — no
  API call, no wasted quota. A call only happens the first time a game is
  seen, or after its odds move, so in practice this runs about once a week
  per game plus the occasional re-price. Commit the cache file back to the
  repo (the GitHub Actions workflow does this automatically alongside the
  dashboard JSON) so it persists between runs.
- **Concurrency**: new/changed games are called in parallel (thread pool,
  6 at a time) rather than one at a time, so a full CFB slate doesn't take
  forever to build.
- **Optional**: if `GEMINI_KEY` isn't set, the builders log a note and skip
  predictions entirely rather than failing the build.

## 1. Get your API keys

- CFBD: [collegefootballdata.com/key](https://collegefootballdata.com/key) — free, sign up with email
- SharpAPI: [sharpapi.io](https://sharpapi.io) — free tier covers DraftKings + FanDuel at 12 requests/min
- Gemini: [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free tier available; optional, only needed for prediction summaries
- ESPN's scoreboard API needs no key (public, unofficial, no auth)

## 2. Run it locally

```bash
pip install -r requirements.txt

export CFBD_API_KEY="your_cfbd_key"
export SHARPAPI_KEY="your_sharpapi_key"
export GEMINI_KEY="your_gemini_key"   # optional -- skips prediction summaries if unset

python scripts/build_dashboard.py       # CFB -> data/dashboard.json
python scripts/build_nfl_dashboard.py   # NFL -> data/nfl_dashboard.json (only needs SHARPAPI_KEY)
```

Then open `index.html` or `nfl.html` in a browser (or run
`python -m http.server` from the project root and visit
`http://localhost:8000`) — opening the files directly with `file://` also
works since the fetch is a relative path in the same folder.

Optional flags:

```bash
python scripts/build_dashboard.py --year 2026 --week 3       # force a specific starting CFB week (builds 3 & 4)
python scripts/build_nfl_dashboard.py --year 2026 --week 2   # force a specific starting NFL week (builds 2 & 3)
python scripts/build_dashboard.py --week 3 --num-weeks 1     # build just one week instead of two
python scripts/build_nfl_dashboard.py --season-type 1        # 1=preseason, 2=regular, 3=postseason
python scripts/build_dashboard.py --out /tmp/test.json       # write somewhere else
```

Each script builds `--week` and the following week (2 weeks total by
default; change with `--num-weeks`) into a single JSON file with a `weeks`
array, so both pages always show the current and upcoming week together.

If no `--week` is given, both scripts default deterministically to week 1
before their season starts, rather than relying on an API's implicit
"current week" behavior: CFB from `WEEK1_START = Aug 22, 2026` in
`scripts/build_dashboard.py`, NFL from `WEEK1_START = Sept 9, 2026` in
`scripts/build_nfl_dashboard.py`. Run it any time before kickoff and you'll
still get the real Week 1 schedule and broadcast info — odds will just be
sparse until DraftKings/FanDuel post lines closer to game day.

## 3. Deploy: GitHub Pages + daily Actions run

1. Push this folder to a GitHub repo (e.g. `mzaiger/cfb-betting-dashboard`).
2. **Add secrets**: repo → Settings → Secrets and variables → Actions → New repository secret
   - `CFBD_API_KEY`
   - `SHARPAPI_KEY`
   - `GEMINI_KEY` (optional — omit to build without prediction summaries)
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
│   ├── gemini_predictions.py        # shared: Gemini prediction calls + caching
│   ├── build_dashboard.py           # CFBD + SharpAPI -> data/dashboard.json
│   └── build_nfl_dashboard.py       # ESPN + SharpAPI -> data/nfl_dashboard.json
├── data/
│   ├── dashboard.json               # generated CFB output (placeholder sample checked in)
│   ├── nfl_dashboard.json           # generated NFL output (placeholder sample checked in)
│   └── gemini_predictions_cache.json # cached Gemini predictions, keyed by matchup + odds hash
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
