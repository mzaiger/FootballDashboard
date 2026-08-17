# Sports Dashboard

Daily-refreshed betting dashboards across four sports (NCAAF, NFL, MLB, and
a placeholder NCAAMB page), plus a Picks page that tracks every pick you've
made across all of them. Each sport's board groups games by week (or, for
MLB, by day) → day → kickoff window (Morning / Noon / Afternoon / Prime
Time / Late Night), ranked within each window by best matchup, with
**DraftKings** and **FanDuel** spreads/run-lines + moneylines attached to
every game.

The underlying JSON files keep **every week/day ever built, forever** —
nothing is ever deleted, and a SharpAPI fetch that comes back blank never
overwrites a previously-fetched line (see "Odds never get erased" below).
Each board only *displays* a small rolling window (current + next week for
NCAAF/NFL, yesterday–today–tomorrow for MLB); the [Picks page](#the-picks-page)
shows every pick you've ever made, no matter how long ago.

Every page has:
- A **hamburger menu** on mobile (upper-left) for the NCAAF / NCAAMB / NFL
  / MLB / Picks nav links; shown inline on desktop.
- **Open / Closed / All pills**, showing counts, next to the nav — Open =
  game hasn't started, Closed = game has started or finished. Defaults to
  **All** on every page; your choice is remembered per-page.
- A **Tiles/Rows toggle** — Tiles is the original card grid, Rows lays the
  same games out as a compact list. Defaults to Rows on desktop and Tiles
  on mobile, remembers whichever you pick after that (stored per device).

| | NCAAF | NFL | MLB | NCAAMB |
|---|---|---|---|---|
| Page | `index.html` | `nfl.html` | `mlb.html` | `ncaamb.html` |
| Data | `data/ncaaf_dashboard.json` | `data/nfl_dashboard.json` | `data/mlb_dashboard.json` | — |
| Builder | `scripts/build_ncaaf_dashboard.py` | `scripts/build_nfl_dashboard.py` | `scripts/build_mlb_dashboard.py` | — |
| Schedule/TV source | [CollegeFootballData.com](https://collegefootballdata.com/key) (CFBD) | ESPN's public scoreboard API (no key needed) | ESPN's public scoreboard API (no key needed) | — |
| Grouped by | Week → day | Week → day | Day (each "week" in the JSON is one calendar day — see MLB section below) | — |
| Matchup ranking | 50% combined AP Top 25 rank + 25% combined win rank + 25% posted spread | Chiefs, then Broncos, then 50% spread + 50% combined win rank | Combined win rank only (run line is ~always ±1.5, so it isn't a useful signal) | — |
| Odds | [SharpAPI](https://sharpapi.io) (DraftKings + FanDuel) | same | same | — |

NCAAMB is nav-only right now — no builder, no data, just a "coming soon"
placeholder page.

All three real builders share matching odds-fetching / team-matching /
time-slot-bucketing / matchup-ranking logic from `scripts/common.py`, so a
fix in one benefits all three.

Every game with a posted DraftKings or FanDuel line also gets a **Gemini
Prediction Summary** — a collapsible button on the card showing an AI
confidence score, which expands to a winner pick, ATS pick, and a
five-sentence explanation. See "Gemini predictions" below.

- **Automation**: `.github/workflows/update-dashboard.yml` (odds +
  schedule, every 4 hours) and `.github/workflows/fetch-scores.yml`
  (scores, hourly) — both run via GitHub Actions and commit the refreshed
  JSON files.
- **Front-end**: static HTML, no build step, no JS framework. Shared
  front-end logic (odds/pick rendering, cookies, filters, nav) lives in
  `picks-store.js`, included by every page.

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
Eastern or another zone — every sport picks this up automatically since
they share the constant. A window only shows up in the output if it
actually has a game in it — no empty sections, and a day/week with zero
games at all doesn't render a heading either (previously an empty day
could show its raw internal date key, like `20260817` — fixed).

Each game gets a **Matchup: N** badge — a plain rank (1 = best/most
marquee matchup) among every game in its natural grouping: the whole
week for NCAAF/NFL, the whole day for MLB. That rank is derived from a
0–100 blended score (the exact blend differs per sport — see the table
above), computed once per week/day, not per time slot. Each kickoff
window also has its own **Time Slot Best Matchup** pick (starred, yellow
border) using its own slot-local logic (see each builder's docstring).

## MLB: day-based instead of week-based

Baseball plays every day, so `build_mlb_dashboard.py` doesn't have a
"week" concept — instead, each entry in `mlb_dashboard.json`'s `weeks`
array holds exactly **one calendar day**, and that entry's `week` number
is that day's date as an integer (`YYYYMMDD`, e.g. `20260817`) rather than
a real week number. This lets it reuse the exact same
`weeks → days → time_slots → games` shape (and all the merge/filter code
that already exists for that shape) while still meaning "one day" per
entry.

The board shows **yesterday through tomorrow** (3 days) by default, so
last night's final scores are still visible after the games wrap up, not
just today/tomorrow. `current_week` in the JSON always means "today" —
the actual build window is centered on it.

## Odds fetching

`common.py`'s `fetch_all_odds()` pages through SharpAPI's `/odds`
endpoint. A few things worth knowing:

- **Pagination info is a top-level field** of SharpAPI's response
  (`payload["pagination"]`), a sibling of `data`/`meta` — NOT nested
  inside `meta`. An earlier version of this function read it from
  `meta.pagination`, which never existed, so `has_more` was always falsy
  and every fetch silently stopped after the first page (up to `limit`
  rows) — for a market with more rows than one page holds, whichever
  games sorted past that cutoff just never got fetched. Fixed; this
  function also uses SharpAPI's `next_cursor` when present, falling back
  to `next_offset`.
- **Fetched per-day, not per-week.** A full week of NFL/NCAAF spreads
  (moneyline + spread, including every alternate line SharpAPI posts per
  game — the "keep only the main line" filter runs client-side *after*
  the fetch, so it doesn't reduce what gets pulled down) can run into the
  thousands of rows. Each builder now fetches one day at a time across
  the week's actual game dates and merges the results, instead of one
  `date_from`/`date_to` request spanning the whole week — smaller
  individual requests, fewer pages each, and one bad day can't affect any
  other day.
- **Side determination prefers SharpAPI's own `selection_type` field**
  (`"home"`/`"away"`, relative to that row's own home/away team) over
  parsing the `selection` text, which is sometimes an abbreviated team
  name (`"TEX Rangers"`, `"Athletics"` with no city) that doesn't always
  fuzzy-match reliably. Only falls back to text-parsing for a row that
  doesn't have `selection_type` at all.
- **MLB's spread market is called `run_line`**, not `spread`/`point_spread`
  (those are the football names) — `_SPREAD_MARKET_ALIASES` in
  `common.py` maps it back to the internal `"spread"` bucket. Only the
  main line (`is_main_line`, not `is_alternate_line`) is kept, matching
  the real-world run line, which is almost always exactly ±1.5.
- **Rate-limit (429) backoff**: SharpAPI's `X-RateLimit-Reset` header is
  an absolute Unix epoch timestamp for when the limit resets, not a
  countdown — treating it directly as "seconds to sleep" (an earlier
  version did) meant a real epoch value like `1786919580` got slept as
  literally that many *seconds*, over 55 years. `_rate_limit_wait_seconds()`
  detects an implausibly large value and converts it to "seconds from
  now", clamped to a sane range either way.

### Odds never get erased

Every build script carries forward the previous run's odds when today's
SharpAPI fetch comes back blank for a game that previously had a line,
instead of overwriting it with blank — sportsbooks periodically wipe and
repost lines day-to-day, and this keeps the board from flashing
"no odds posted" during that gap. A value present in today's fetch always
overwrites the old one immediately. Old weeks/days are merged onto, never
replaced by, a fresh build — nothing already on disk is ever dropped.

## NFL week labels: HOF / Preseason Week N / Regular Season Week N

ESPN numbers the NFL preseason as week 1 = the Hall of Fame Game, week 2 =
the first real preseason slate, week 3 = the second, etc. — so the
on-screen label is shifted down by one and the Hall of Fame week gets its
own `HOF` label:

| ESPN `week.number` | Label |
|---|---|
| 1 | `HOF` |
| 2 | `Preseason Week 1` |
| 3 | `Preseason Week 2` |
| — (season_type 2) | `Regular Season Week N` |
| — (season_type 3) | `Postseason Week N` |

`resolve_current()` in `build_nfl_dashboard.py` asks ESPN for both the
current week *and* season type fresh on every run (rather than trusting a
hardcoded default), with two safety nets against ESPN's own answer
stalling on an already-finished week (most likely during the multi-day
gap between two weeks, when "today" has no games of its own to resolve
"current" against): a floor check against any later week already built
with games still ahead, and an anti-regression guard against a
`current_week` already confirmed on a previous run.

## Payout calculator

Once you've picked a side (ATS or ML) on a game, the pick toolbar shows
what a **$10** and a **$100** bet on that side would pay out — using
standard American-odds payout math (the same formula both DraftKings and
FanDuel use for both moneyline and against-the-spread bets: the line only
decides win/lose, the American price attached to that side decides the
payout). DraftKings' price is used if posted, FanDuel's otherwise.

The payout is **calculated once, at the moment you pick**, and stored in
the pick's cookie right alongside the locked-in line — same reasoning as
locking the line itself: if a later rebuild changes or removes that game's
price, the payout already shown for your pick doesn't silently change or
disappear along with it.

The [Picks page](#the-picks-page) also shows a running **$10/pick** total
— net profit/loss across every *graded* pick, as if every single one had
been a flat $10 bet.

## The Picks page

`picks.html` shows every game you've picked across every sport, grouped
**Day → Time Slot → Sport → Matchup rank** (all sports for a given day
shown together, not in separate sport sections), plus three stat badges at
the top:

- **Active** — picks made, game not graded yet
- **Inactive** — `correct/total` across every graded pick (win or push
  counts as correct)
- **$10/pick** — net $, as if every graded pick had been a $10 bet (see
  "Payout calculator" above)

### How picks store their line

Clicking a pick button stores a cookie with the market/side you picked,
plus a snapshot of that book's line **at the moment you clicked** — so
grading stays correct even if the book later stops publishing that line
entirely. Unpicked cells always show today's live odds. Cookies are set
for 400 days (the actual ceiling — Chrome and other Chromium browsers cap
every cookie's expiry at 400 days no matter what a larger value asks for,
so there's no way to set one that truly never expires) and get a fresh
400-day clock every time the pick is read again, not just when it's made
or changed — so a pick you keep coming back to effectively never expires.

## Gemini predictions

`scripts/gemini_predictions.py` calls the Gemini API once per game with
current-season stats + posted odds (and, for MLB, the actual calendar date
and probable starting pitchers when announced) and caches the result by a
hash of the matchup + odds + model, so re-running a build doesn't re-spend
a Gemini call on a game whose numbers haven't changed. Set `GEMINI_KEY` to
enable; omit it and the build still runs, just without predictions.

## Setup

```
pip install -r requirements.txt
```

Environment variables:
- `SHARPAPI_KEY` — required, from [sharpapi.io](https://sharpapi.io)
- `CFBD_KEY` — required for NCAAF only, from
  [collegefootballdata.com/key](https://collegefootballdata.com/key)
- `GEMINI_KEY` — optional, enables Gemini predictions

```
python scripts/build_ncaaf_dashboard.py
python scripts/build_nfl_dashboard.py
python scripts/build_mlb_dashboard.py
python scripts/fetch_scores.py
```

Each accepts `--help` for its full set of options (date/week overrides,
output path, etc.).
