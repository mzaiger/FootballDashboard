# CFB Betting Dashboard

A daily-refreshed college football dashboard: this week's games on **main TV
channels**, grouped by day and ranked by best matchup (combined AP rank of
both teams), with **DraftKings** and **FanDuel** spreads + moneylines
attached to each game.

- **Schedule, TV outlet, and AP rankings** → [CollegeFootballData.com](https://collegefootballdata.com/key) (CFBD)
- **Spreads & moneylines** → [SharpAPI](https://sharpapi.io) (DraftKings + FanDuel)
- **Backend**: `scripts/build_dashboard.py` — pulls both APIs, ranks matchups, writes `data/dashboard.json`
- **Automation**: `.github/workflows/update-dashboard.yml` — runs the script daily via GitHub Actions and commits the refreshed JSON
- **Front-end**: `index.html` — static page that reads `data/dashboard.json`, no build step

# CFB Betting Dashboard

A daily-refreshed college football dashboard: this week's games on **main TV
channels**, grouped by day and then by kickoff window (Morning / Noon /
Afternoon / Evening / Late Night), ranked within each window by best matchup
(combined AP rank of both teams), with **DraftKings** and **FanDuel**
spreads + moneylines attached to each game.

- **Schedule, TV outlet, and AP rankings** → [CollegeFootballData.com](https://collegefootballdata.com/key) (CFBD)
- **Spreads & moneylines** → [SharpAPI](https://sharpapi.io) (DraftKings + FanDuel)
- **Backend**: `scripts/build_dashboard.py` — pulls both APIs, ranks matchups, writes `data/dashboard.json`
- **Automation**: `.github/workflows/update-dashboard.yml` — runs the script daily via GitHub Actions and commits the refreshed JSON
- **Front-end**: `index.html` — static page that reads `data/dashboard.json`, no build step

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

Windows are bucketed in **US/Central**. Change `DISPLAY_TIMEZONE` near the
top of `scripts/build_dashboard.py` (e.g. to `"America/New_York"`) if you'd
rather bucket by Eastern or another zone. A window only shows up in the
output if it actually has a game in it — no empty sections.

Within each window, games are sorted by `matchup_score = home_team_AP_rank +
away_team_AP_rank` (unranked teams count as 26). **Lower score = more
marquee matchup**, so a #1 vs #2 game (score 3) leads a #15-vs-unranked game
(score 41) within the same window.

"Main channels" is currently `ABC, CBS, NBC, FOX, ESPN, ESPN2, FS1` — edit
the `MAIN_CHANNELS` set near the top of `scripts/build_dashboard.py` to add
or remove networks (e.g. add `ESPNU`, `SECN`, `ACCN` if you want conference
networks included).

## 1. Get your API keys

- CFBD: [collegefootballdata.com/key](https://collegefootballdata.com/key) — free, sign up with email
- SharpAPI: [sharpapi.io](https://sharpapi.io) — free tier covers DraftKings + FanDuel at 12 requests/min

## 2. Run it locally

```bash
pip install -r requirements.txt

export CFBD_API_KEY="your_cfbd_key"
export SHARPAPI_KEY="your_sharpapi_key"

python scripts/build_dashboard.py
```

This writes `data/dashboard.json`. Then just open `index.html` in a browser
(or run `python -m http.server` from the project root and visit
`http://localhost:8000`) — opening the file directly with `file://` also
works since the fetch is a relative path in the same folder.

Optional flags:

```bash
python scripts/build_dashboard.py --year 2026 --week 3   # force a specific week
python scripts/build_dashboard.py --out /tmp/test.json   # write somewhere else
```

If no `--week` is given, the script auto-derives the current CFBD week from
the season-1 kickoff date (`WEEK1_START = Aug 22, 2026`, matching how CFBD
numbers weeks). Before that date it defaults to week 1.

## 3. Deploy: GitHub Pages + daily Actions run

1. Push this folder to a GitHub repo (e.g. `mzaiger/cfb-betting-dashboard`).
2. **Add secrets**: repo → Settings → Secrets and variables → Actions → New repository secret
   - `CFBD_API_KEY`
   - `SHARPAPI_KEY`
3. **Enable Pages**: repo → Settings → Pages → Source: `Deploy from a branch` → branch `main`, folder `/ (root)`.
4. **Enable Actions writes**: the workflow already requests `contents: write`
   permission, but if your org has a stricter default, go to Settings →
   Actions → General → Workflow permissions → "Read and write permissions".
5. Trigger the first run manually: Actions tab → "Update CFB Betting Dashboard" → Run workflow.
   After that it runs automatically every day at 10:00 UTC (edit the `cron`
   line in `.github/workflows/update-dashboard.yml` to change the time).

Your site will be live at `https://<username>.github.io/<repo>/`.

## Project structure

```
cfb-betting-dashboard/
├── index.html                       # static front-end
├── requirements.txt
├── scripts/
│   └── build_dashboard.py           # CFBD + SharpAPI → data/dashboard.json
├── data/
│   └── dashboard.json               # generated output (placeholder sample checked in)
└── .github/workflows/
    └── update-dashboard.yml         # daily cron + manual trigger
```

## Notes / things worth knowing

- **Team name matching**: CFBD and SharpAPI don't always use identical team
  name strings (e.g. mascots vs. school names only). `match_odds_for_game()`
  in the script does fuzzy matching; if a book hasn't posted lines yet for a
  game (common days before kickoff for smaller games), the card just shows
  "Odds not yet posted" instead of guessing.
- **Rate limits**: SharpAPI's free tier is 12 requests/minute. The script
  pulls all NCAAF odds in one paginated sweep (a handful of requests) rather
  than one call per game, so a full week's slate stays well under that.
- **AP Top 25 only**: rankings use the AP poll. Swap `poll_name` in
  `build_rank_lookup()` to `"Coaches Poll"` or (once available) the CFP
  rankings if you'd rather rank by those instead.
