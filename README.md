# Sports Dashboard

Daily-refreshed betting dashboards across five sports (NCAAF, NFL, MLB,
NBA, and NCAAMB), plus a Picks page that tracks every pick you've made
across all of them. Each sport's board groups games by week (or, for
MLB/NBA/NCAAMB, by day) → day → kickoff window (Morning / Noon /
Afternoon / Prime Time / Late Night), ranked within each window by best
matchup, with **DraftKings** and **FanDuel** spreads/run-lines +
moneylines attached to every game.

The underlying JSON files keep **every week/day ever built, forever** —
nothing is ever deleted, and a SharpAPI fetch that comes back blank never
overwrites a previously-fetched line (see "Odds never get erased" below).
Each board only *displays* a small rolling window (current + next week for
NCAAF/NFL, yesterday–today–tomorrow for MLB/NBA/NCAAMB); the
[Picks page](#the-picks-page) shows every pick you've ever made, no matter
how long ago.

Every page has:
- A **hamburger menu** on mobile (upper-left) for the NCAAF / NCAAMB / NFL
  / MLB / NBA / Accuracy / Export / Picks nav links; shown inline on desktop.
- **Open / Closed / All pills**, showing counts, next to the nav — Open =
  game hasn't started, Closed = game has started or finished. Defaults to
  **All** on every page; your choice is remembered per-page.
- A **Tiles/Rows toggle** — Tiles is the original card grid, Rows lays the
  same games out as a compact list. Defaults to Rows on desktop and Tiles
  on mobile, remembers whichever you pick after that (stored per device).
- A floating **Key** button (bottom-right, via `site-key.js`) that opens a
  glossary of every abbreviation on the site (ML, ATS, DK, FD, AP, and the
  sport names) plus a note on the cookie expiry policy.

### Export page

`export.html` lets you download the board data or your own picks as
**JSON** or **CSV** (pick the format at the top of the page):
- **Sport Data** — the full current NCAAF/NFL/MLB/NBA/NCAAMB dataset.
  JSON downloads the exact file the site itself reads; CSV flattens it to
  one row per game (teams, records, channel, DK/FD spread + moneyline,
  matchup rank).
- **My Picks** — every pick saved in your cookies, across all five
  sports, with the matchup, pick type (ATS/ML), which side you took, the
  spread or money line you locked in, and how much a $10 bet on it won or
  lost (blank/"Pending" if the game hasn't finished). A pick on a game
  that's since aged off the live board's JSON won't appear, since the
  export needs that game's data to fill in the row.

### Season year

NCAAF, NFL, NBA, and NCAAMB all derive their current season year from
today's date automatically — none of them need a manual year bump when a
new season starts:
- **NCAAF/NFL** (`current_season_year()` in each script): games from July
  onward belong to the season starting that same calendar year; games
  from January–June belong to the season that started the *previous*
  calendar year (bowl/playoff season). Pass `--year` explicitly to
  override it for a one-off manual run.
- **NBA**: games from October onward belong to the season starting that
  same calendar year; games from January–September belong to the season
  that started the previous calendar year (playoffs stretch into June;
  nothing else is scheduled the rest of the summer).
- **NCAAMB**: games from August onward belong to the season starting that
  same calendar year (exhibitions can start in late October, but August
  is a safe off-season cutover); games from January–July belong to the
  season that started the previous calendar year.

MLB was already date-driven and didn't need this.

| | NCAAF | NFL | MLB | NBA | NCAAMB |
|---|---|---|---|---|---|
| Page | `index.html` | `nfl.html` | `mlb.html` | `nba.html` | `ncaamb.html` |
| Data | `data/ncaaf_dashboard.json` | `data/nfl_dashboard.json` | `data/mlb_dashboard.json` | `data/nba_dashboard.json` | `data/ncaamb_dashboard.json` |
| Builder | `scripts/build_ncaaf_dashboard.py` | `scripts/build_nfl_dashboard.py` | `scripts/build_mlb_dashboard.py` | `scripts/build_nba_dashboard.py` | `scripts/build_ncaamb_dashboard.py` |
| Schedule/TV source | ESPN's public scoreboard + rankings APIs (no key needed) | ESPN's public scoreboard API (no key needed) | ESPN's public scoreboard API (no key needed) | ESPN's public scoreboard API (no key needed) | ESPN's public scoreboard API (no key needed) |
| TV filter | Main channels only (ABC/CBS/NBC/FOX/ESPN/ESPN2/FS1) | none — all games | none — all games | none — all games | Main channels only (same set as NCAAF) |
| Grouped by | Week → day | Week → day | Day (each "week" in the JSON is one calendar day — see MLB section below) | Day (same day-based scheme as MLB) | Day (same day-based scheme as MLB) |
| Matchup ranking | 50% combined AP Top 25 rank + 25% combined win rank + 25% posted spread | 50% posted spread + 50% combined win rank | Combined win rank only (run line is ~always ±1.5, so it isn't a useful signal) | 50% posted spread + 50% combined win rank | 50% combined AP Top 25 rank + 25% combined win rank + 25% posted spread (same blend as NCAAF) |
| Odds | [SharpAPI](https://sharpapi.io) (DraftKings + FanDuel) | same | same | same (SharpAPI league code `nba`) | same (SharpAPI league code `ncaab`) |

All five builders share matching odds-fetching / team-matching /
time-slot-bucketing / matchup-ranking logic from `scripts/common.py`, so a
fix in one benefits all five. NBA and NCAAMB both reuse the exact
day-based `weeks → days → time_slots → games` scheme MLB introduced (see
"MLB: day-based instead of week-based" below) rather than a real "week N"
concept, since both play/schedule day-to-day. NCAAMB additionally reuses
NCAAF's main-channel TV filter and AP-poll blend, since like CFB, most of
a given day's slate is on ESPNU/ESPN+/conference networks and not worth
boarding, and there's a real AP Top 25 to rank by.

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

## MLB/NBA/NCAAMB: day-based instead of week-based

Baseball, pro basketball, and college basketball all play/schedule most
days of the week, so `build_mlb_dashboard.py`, `build_nba_dashboard.py`,
and `build_ncaamb_dashboard.py` don't have a "week" concept — instead,
each entry in their `weeks` array holds exactly **one calendar day**, and
that entry's `week` number is that day's date as an integer (`YYYYMMDD`,
e.g. `20260817`) rather than a real week number. This lets all three reuse
the exact same `weeks → days → time_slots → games` shape (and all the
merge/filter code that already exists for that shape) while still meaning
"one day" per entry. NBA and NCAAMB copy this scheme directly from MLB,
which introduced it first.

Each board shows **yesterday through tomorrow** (3 days) by default, so
last night's final scores are still visible after the games wrap up, not
just today/tomorrow. `current_week` in each JSON always means "today" —
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

### Odds and Gemini predictions freeze once a game starts

Each build script reads `data/scores.json` (the same overlay file
`fetch_scores.py` writes hourly) and checks it for every game before
touching that game's odds or Gemini prediction. `fetch_scores.py` only
ever writes an entry for a game once its status leaves "not started" —
so ANY entry there, live or final, means kickoff/first pitch has already
happened. For a game in that state, the build script skips re-fetching
odds and skips calling Gemini entirely, reusing exactly what was saved
in the previous build instead. This is deliberate: a sportsbook's
in-game line moves constantly and doesn't reflect the pregame market a
pick or prediction was actually made against, and re-calling Gemini
against a moving in-game (or final) line doesn't make sense either. Once
a game is frozen this way it stays frozen — nothing about it updates
again until the next time that particular JSON gets rebuilt fresh (e.g.
a new season/week).

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
shown together, not in separate sport sections), plus a **My Week's
Stats** row of 4 badges at the top. Unlike the pick list underneath
(which is all-time), these 4 badges are scoped to the current
**Wednesday-through-Tuesday window** — so they answer "how did I do
this week, and how much would $10/pick have made me this week":

- **Open** — picks made this week, game not graded yet
- **My Accuracy** — percentage (with `correct/total` underneath) across
  every one of your own graded picks this week (win or push counts as
  correct)
- **$10/bet** — net $ this week, as if every graded pick had been a $10
  bet (see "Payout calculator" above)
- **Gemini** — Gemini's own accuracy this week on just the games you
  actually picked, in whichever market (ML/ATS/Both) the filter bar has
  selected — percentage on top, `correct/total` underneath, with the
  active market shown under "Gemini"

### How picks store their line

Clicking a pick button stores a cookie with the market/side you picked,
plus a snapshot of that book's line **at the moment you clicked** — so
grading stays correct even if the book later stops publishing that line
entirely. Unpicked cells always show today's live odds. Cookies expire
every **August 1st (UTC)** — shortly before each new season's week 1 —
so last season's picks clear out on their own before the new one starts,
rather than sticking around indefinitely. This is also noted in the Key
(see below) on every page.

## The Accuracy page

`accuracy.html` shows two things side by side: how accurate **Gemini's**
predictions have been, and how accurate **your own saved picks** have
been — across **every graded prediction/pick ever made** in any of the
five dashboard files, not just what's on the current board. It combines
`data/ncaaf_dashboard.json`, `data/nfl_dashboard.json`,
`data/mlb_dashboard.json`, `data/nba_dashboard.json`, and
`data/ncaamb_dashboard.json` (each of which keeps every week/day ever
built) with `data/scores.json` to grade every prediction and every saved
pick against the actual final score. "My Accuracy" is read from the same
pick cookies the Picks page uses, so it works without any extra setup.

Its own filter bar (separate from the board pages' filter, so changing one
doesn't affect the other) controls:
- **Best Matchups** — restrict to each time slot's single top-ranked game;
  affects both Gemini's numbers and your own
- **Both / ML / ATS** — which of Gemini's two numbers to actually show;
  the hidden one reads "NA" rather than being excluded. This toggle
  never changes which games count toward either number — ML accuracy is
  always every graded prediction whose OWN ml confidence clears the
  Confidence floor below, and ATS accuracy is always every graded
  prediction whose OWN ats confidence clears it, entirely independently
  of each other and of this toggle. (Also picks which of your own pick
  markets "My Accuracy" counts — see below.)
- **Confidence** — a minimum threshold (0–100, in steps of 5), checked
  per market as described above; affects **only Gemini's numbers**,
  never "My Accuracy" — a bet you made is graded the same regardless of
  how confident Gemini happened to be about that game

For example, "Best Matchups" + "ATS" + "65+" shows Gemini's all-time ATS
accuracy on just the single best matchup in every time slot it's ever
been at least 65% confident on (and, right alongside it, your own ATS
accuracy on just those best-matchup games, unaffected by the 65+). Any
active filters show up right on the ML/ATS labels and table column
headers so it's always clear what's being counted. Two "by Sports"
tables break the same numbers down into NCAAF / NFL / MLB / NBA / NCAAMB
individually, each with an "Ungraded Games" column for predictions/picks
made on a game that hasn't finished yet.

### Total Winnings

Alongside accuracy, the page shows **Total Winnings** — same flat-$10-
per-pick math as the Picks page's `$10/bet` badge, but all-time and
split two ways:

- **Gemini Total Winnings** — hypothetical: what a flat $10 bet on every
  one of Gemini's own picks would have paid, priced off whatever
  DraftKings/FanDuel odds are currently attached to that game (there's no
  "locked-in" price for a prediction the way a real pick has one). This
  number honors **Best Matchups**, **Confidence**, and **Both/ML/ATS**
  exactly like Gemini's accuracy numbers do, since those filters change
  which predictions Gemini "made" in the first place.
- **My Total Winnings** — real: your own picks, priced at whatever line
  was locked in at pick time. Like "My Accuracy," this only responds to
  **Best Matchups** — Confidence and the Both/ML/ATS toggle never affect
  it, since what a bet actually pays doesn't depend on how confident
  Gemini was or which market column it happens to be shown under.

Both "by Sports" tables also show winnings per market — a signed,
whole-dollar amount (no cents, to keep the layout stable as totals grow)
underneath each market's `correct/total` fraction in the ML and ATS
columns, filtered the same way as their column's accuracy number.

## Gemini predictions

`scripts/gemini_predictions.py` calls the Gemini API once per game with
current-season stats + posted odds (and, for MLB, the actual calendar date
and probable starting pitchers when announced) and caches the result by a
hash of the matchup + odds + model fallback chain, so re-running a build
doesn't re-spend a Gemini call on a game whose numbers haven't changed.
Set `GEMINI_KEY` to enable; omit it and the build still runs, just
without predictions.

**Model fallback chain** — each call tries these in order, falling back
immediately to the next one the instant a model comes back rate-limited,
over quota, or unavailable, instead of waiting/retrying on the same model:

1. `gemini-3.5-flash-lite`
2. `gemini-3.1-flash-lite`
3. `gemini-2.5-flash-lite`
4. `gemini-2.0-flash-lite`

If every model in the chain is rate-limited on a given run, that run stops
calling Gemini entirely; the remaining games are picked up on the next run
(the cache preserves whatever was already completed).

**Weekly full refresh** — every Wednesday (UTC), every not-yet-started NFL
game with posted odds gets a brand-new Gemini call regardless of whether
its cache entry is still valid. Every Thursday (UTC), CFB gets the same
treatment. MLB/NBA/NCAAMB don't have a scheduled refresh day. Either way,
a game that's already started or finished is never touched by this — its
prediction (and odds) stay frozen at whatever was saved before kickoff.

## Setup

```
pip install -r requirements.txt
```

Environment variables:
- `SHARPAPI_KEY` — required, from [sharpapi.io](https://sharpapi.io)
- `GEMINI_KEY` — optional, enables Gemini predictions

NCAAF no longer needs its own key — it pulls schedule, broadcast, records,
and the AP Top 25 poll from ESPN's public API, same as NFL and MLB.

```
python scripts/build_ncaaf_dashboard.py
python scripts/build_nfl_dashboard.py
python scripts/build_mlb_dashboard.py
python scripts/build_nba_dashboard.py
python scripts/build_ncaamb_dashboard.py
python scripts/fetch_scores.py
```

Each accepts `--help` for its full set of options (date/week overrides,
output path, etc.).
