/*
 * Shared "my picks" storage for the CFB / NFL / Picks pages.
 *
 * Picks are stored one cookie per game (pick_<sport>_<gameId>) rather than
 * one giant cookie, so any page can cheaply enumerate every active pick for
 * a sport without needing to know the full game list up front. Value is a
 * tiny JSON blob: {"market":"spread"|"moneyline","side":"home"|"away"}.
 * Only one pick is allowed per game -- selecting a new option overwrites
 * the old one, and clicking the active option again clears it.
 *
 * Picks intentionally do NOT store the team name, line, or odds -- those
 * are looked up live from the day's dashboard JSON at render time, so a
 * pick always reflects the latest number even if odds move.
 */

/*
 * Week label formatting, shared by nfl.html and picks.html.
 *
 * ESPN numbers NFL preseason weeks 1-4 the same as it numbers regular
 * season weeks 1-4, so a bare "Week 1" is ambiguous between the Hall of
 * Fame/preseason opener and the real regular-season opener. When a
 * dashboard's season_type is 1 (preseason), this renders "P1"-"P4"
 * instead; season_type 2 (regular) and 3 (postseason) just render
 * "Week N" as before. CFB never sets season_type, so it always falls
 * through to "Week N".
 */
function formatWeekLabel(weekNum, seasonType) {
  return seasonType === 1 ? `P${weekNum}` : `Week ${weekNum}`;
}

// Bare number/label with no "Week" word -- e.g. "P1" or "3" -- for building
// a "Week ..."/"Weeks ..." title the same way the College page does
// (`Week ${n}` / `Weeks ${a}–${b}`), just with the preseason "P" prefix
// folded into the number instead of a plain integer.
function formatWeekNumberLabel(weekNum, seasonType) {
  return seasonType === 1 ? `P${weekNum}` : `${weekNum}`;
}

/*
 * Which week(s) to actually show on the College / NFL boards.
 *
 * dashboard.json / nfl_dashboard.json now keep every week ever built (see
 * merge_weeks() in scripts/common.py) so Picks can grade a bet from way
 * back -- but College and NFL themselves should still only ever show
 * "now". A game's line is worth displaying up through the day after it's
 * played (so you can still glance at how it closed), then it should drop
 * off the board.
 *
 * "Current week" = the week containing the nearest game whose kickoff is
 * on or after (today - 1 day), i.e. yesterday at midnight. That one day of
 * grace is what keeps, say, Sunday's slate visible all through Monday --
 * Tuesday is when it finally drops, since by then even Sunday's late game
 * is more than a day old. Whichever week that lands on, plus the very next
 * stored week, are the only two shown; everything else stays in the JSON
 * (for Picks) but off the College/NFL boards.
 */

function findCurrentWeekIndex(weeks, now) {
  now = now || new Date();
  const cutoff = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);

  let bestTime = null;
  let bestIdx = null;
  (weeks || []).forEach((week, idx) => {
    (week.days || []).forEach(day => {
      (day.time_slots || []).forEach(slot => {
        (slot.games || []).forEach(g => {
          const t = new Date(g.start_time);
          if (Number.isNaN(t.getTime())) return;
          if (t >= cutoff && (bestTime === null || t < bestTime)) {
            bestTime = t;
            bestIdx = idx;
          }
        });
      });
    });
  });

  if (bestIdx !== null) return bestIdx;
  // Nothing on or after the cutoff (e.g. off-season, or every stored game
  // has already aged out) -- fall back to the most recent week we have.
  return weeks && weeks.length ? weeks.length - 1 : null;
}

// Returns just the weeks College/NFL should render: the current week (per
// findCurrentWeekIndex) plus the one immediately after it in the stored,
// week-number-ascending list. Picks.html does NOT use this -- it shows
// every week that has ever had a pick made against it.
function selectDisplayWeeks(weeks, now) {
  if (!weeks || !weeks.length) return [];
  const idx = findCurrentWeekIndex(weeks, now);
  if (idx === null) return [];
  return weeks.slice(idx, idx + 2);
}

const PICK_COOKIE_PREFIX = 'pick_';
const PICK_COOKIE_DAYS = 210;
const PICK_LOCKED_STATUSES = ['final', 'in_progress', 'live', 'halftime'];

function isPickLocked(gScore) {
  if (!gScore || !gScore.status) return false;

  const status = String(gScore.status).toLowerCase();

  return PICK_LOCKED_STATUSES.includes(status);
}

function _pickCookieName(sport, gameId) {
  return `${PICK_COOKIE_PREFIX}${sport}_${gameId}`;
}

function _setCookie(name, value, days) {
  const expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
}

function _deleteCookie(name) {
  document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; SameSite=Lax`;
}

function getPick(sport, gameId) {
  const target = _pickCookieName(sport, gameId) + '=';
  const parts = document.cookie.split(';');
  for (let raw of parts) {
    raw = raw.trim();
    if (raw.startsWith(target)) {
      try { return JSON.parse(decodeURIComponent(raw.slice(target.length))); }
      catch (e) { return null; }
    }
  }
  return null;
}

function getAllPicks(sport) {
  const prefix = `${PICK_COOKIE_PREFIX}${sport}_`;
  const out = {};
  document.cookie.split(';').forEach(raw => {
    raw = raw.trim();
    const eq = raw.indexOf('=');
    if (eq === -1) return;
    const name = raw.slice(0, eq);
    if (!name.startsWith(prefix)) return;
    const gameId = name.slice(prefix.length);
    try { out[gameId] = JSON.parse(decodeURIComponent(raw.slice(eq + 1))); }
    catch (e) { /* ignore malformed cookie */ }
  });
  return out;
}

// Toggle a pick: clicking the already-active option clears it, clicking any
// other option overwrites it (only one pick allowed per game at a time).
// `oddsSnapshot`, when provided, is `{draftkings: entry|null, fanduel: entry|null}`
// -- each entry is a copy of that book's odds row (`{line, american}`) for
// the chosen market/side, captured at the moment of the click. It's stored
// in the cookie alongside the pick so the line you actually took survives
// even if a later daily rebuild shows a different number (the market
// moved) or no number at all (the book pulled the line after the game
// closed). Live cells that were never picked keep showing today's live
// line as always -- only the picked cell is pinned.
function togglePick(sport, gameId, market, side, oddsSnapshot) {
  const current = getPick(sport, gameId);
  if (current && current.market === market && current.side === side) {
    _deleteCookie(_pickCookieName(sport, gameId));
    return null;
  }
  const value = { market, side, odds: oddsSnapshot || null };
  _setCookie(_pickCookieName(sport, gameId), JSON.stringify(value), PICK_COOKIE_DAYS);
  return value;
}

// Returns the locked-in odds entry (`{line, american}`) captured at pick
// time for one book's cell, but only if that cell is the one actually
// picked (matching market + side) -- otherwise null, so callers fall back
// to today's live line. This is what lets grading survive a book removing
// its line after a game closes.
function getLockedOddsEntry(sport, gameId, market, side, book) {
  const pick = getPick(sport, gameId);
  if (!pick || pick.market !== market || pick.side !== side || !pick.odds) return null;
  return pick.odds[book] || null;
}

// Short "(-3.5)" / "(+150)" suffix for the active pick button, using
// whichever book's snapshot was captured (DraftKings first, then
// FanDuel) so you can see at a glance what number you actually picked at.
function formatLockedLine(pick) {
  if (!pick || !pick.odds) return '';
  const entry = pick.odds.draftkings || pick.odds.fanduel;
  if (!entry) return '';
  const raw = pick.market === 'spread' ? entry.line : entry.american;
  if (raw === undefined || raw === null || raw === '') return '';
  const n = Number(raw);
  if (Number.isNaN(n)) return '';
  return ` (${n > 0 ? '+' : ''}${n})`;
}

// The 4-button toolbar (away/home x spread/moneyline) for one game card.
// Once a game is final (gScore.status === 'final'), the buttons render
// disabled -- the pick made (if any) still shows, but can no longer be
// changed after the fact.
function renderPickToolbar(sport, g, gScore) {
  const pick = getPick(sport, g.id);
  const locked = isPickLocked(gScore);

  const oddsFor = (market, side) => ({
    draftkings: (g.odds?.draftkings?.[market]?.[side]) || null,
    fanduel: (g.odds?.fanduel?.[market]?.[side]) || null,
  });

  const opts = [
    { market: 'spread', side: 'away', label: `${g.away_team} ATS` },
    { market: 'spread', side: 'home', label: `${g.home_team} ATS` },
    { market: 'moneyline', side: 'away', label: `${g.away_team} ML` },
    { market: 'moneyline', side: 'home', label: `${g.home_team} ML` },
  ];

  const btns = opts.map(o => {
    const active = pick && pick.market === o.market && pick.side === o.side;
    const oddsAttr = encodeURIComponent(JSON.stringify(oddsFor(o.market, o.side)));
    const lockedSuffix = active ? formatLockedLine(pick) : '';

    return `<button type="button" class="pick-btn${active ? ' active' : ''}" data-sport="${sport}" data-game="${g.id}" data-market="${o.market}" data-side="${o.side}" data-odds="${oddsAttr}"${locked ? ' disabled' : ''}>${active ? '\u2605 ' : ''}${o.label}${lockedSuffix}</button>`;
  }).join('');

  return `<div class="pick-toolbar">${btns}</div>`;
}

// CSS class to drop on an odds-table cell so the picked market/side lights
// up yellow wherever it appears (both the DraftKings and FanDuel rows).
function pickCellClass(sport, g, market, side) {
  const pick = getPick(sport, g.id);
  return (pick && pick.market === market && pick.side === side) ? 'picked' : '';
}

// CSS class to mark an odds-table cell green (hit) or red (miss), once a
// game is final -- the team that covered (spread) or won outright
// (moneyline) gets 'hit', the other side gets 'miss'. A push (spread) or
// tie (moneyline) -- no actual loser -- marks BOTH sides 'hit'. Empty
// string while the game is still live/unstarted, since the outcome isn't
// settled yet, or if this cell has no line/odds posted. `entry` is the
// cell's own live odds object (e.g. spread.home) from today's build. For
// the exact cell that was picked (market/side match), the line locked in
// at pick time (see getLockedOddsEntry) takes priority over `entry` --
// that's what keeps grading correct even after a book stops publishing a
// line for a finished game. Un-picked cells always use today's live line,
// same as before.
function oddsHitClass(sport, gameId, book, market, side, entry, gScore) {
  if (!isPickLocked(gScore)) return '';

  if (gScore.home_score === null || gScore.home_score === undefined) return '';
  if (gScore.away_score === null || gScore.away_score === undefined) return '';

  const sideScore = Number(side === 'home' ? gScore.home_score : gScore.away_score);
  const otherScore = Number(side === 'home' ? gScore.away_score : gScore.home_score);

  if (Number.isNaN(sideScore) || Number.isNaN(otherScore)) return '';

  if (market === 'moneyline') {
    // Currently tied live game or final tie: no loser.
    // If you want live ties to stay neutral instead of green, return '' here.
    if (sideScore === otherScore) return 'hit';

    return sideScore > otherScore ? 'hit' : 'miss';
  }

  if (market === 'spread') {
    const lockedEntry = getLockedOddsEntry(sport, gameId, market, side, book);
    const source = lockedEntry || entry;
    if (!source || source.line === null || source.line === undefined) return '';

    const line = Number(source.line);

    if (Number.isNaN(line)) return '';

    const result = (sideScore - otherScore) + line;

    // Currently pushing live game or final push: no loser.
    // If you want live pushes to stay neutral instead of green, return '' here.
    if (result === 0) return 'hit';

    return result > 0 ? 'hit' : 'miss';
  }

  return '';
}

// Whether the pick actually made on this game (if any) is currently a win
// ('hit'), a loss ('miss'), or not yet determined ('' -- game hasn't
// finished, or there's no line to grade against). Reuses oddsHitClass()
// against the picked market/side (preferring the DraftKings snapshot,
// falling back to FanDuel, same preference order as formatLockedLine) so
// this always agrees with the color already shown on that picked cell.
// Returns null if no pick was made on this game at all.
function pickOutcome(sport, g, gScore) {
  const pick = getPick(sport, g.id);
  if (!pick) return null;
  const dkEntry = g.odds && g.odds.draftkings && g.odds.draftkings[pick.market] && g.odds.draftkings[pick.market][pick.side];
  const fdEntry = g.odds && g.odds.fanduel && g.odds.fanduel[pick.market] && g.odds.fanduel[pick.market][pick.side];
  const book = dkEntry ? 'draftkings' : 'fanduel';
  const entry = dkEntry || fdEntry || null;
  return oddsHitClass(sport, g.id, book, pick.market, pick.side, entry, gScore) || '';
}

// Total correct/incorrect count across every pick ever made (any week,
// any sport), for the summary at the top of the Picks page. `datasets` is
// an array of {sport, data} (the full, unfiltered dashboard JSON for each
// sport); `scores` is {cfb: {...}, nfl: {...}} from data/scores.json. A
// push/tie counts as correct, matching the same convention oddsHitClass()
// already uses for cell coloring. Picks with no settled result yet aren't
// counted in either bucket.
function computePickRecord(datasets, scores) {
  let correct = 0, incorrect = 0;
  (datasets || []).forEach(({ sport, data }) => {
    if (!data) return;
    (data.weeks || []).forEach(week => (week.days || []).forEach(day => (day.time_slots || []).forEach(slot => {
      (slot.games || []).forEach(g => {
        if (!getPick(sport, g.id)) return;
        const gScore = (scores[sport] || {})[String(g.id)];
        const outcome = pickOutcome(sport, g, gScore);
        if (outcome === 'hit') correct++;
        else if (outcome === 'miss') incorrect++;
      });
    })));
  });
  return { correct, incorrect };
}

// Click-delegation for every .pick-btn inside containerEl. `onPick` runs
// after each toggle so the caller can re-render with the new cookie state.
function attachPickHandlers(containerEl, onPick) {
  containerEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.pick-btn');
    if (!btn || !containerEl.contains(btn) || btn.disabled) return;
    const { sport, game, market, side, odds } = btn.dataset;
    let oddsSnapshot = null;
    if (odds) {
      try { oddsSnapshot = JSON.parse(decodeURIComponent(odds)); } catch (e) { /* ignore malformed snapshot */ }
    }
    togglePick(sport, game, market, side, oddsSnapshot);
    if (typeof onPick === 'function') onPick();
  });
}

/*
 * Gemini Prediction Summary block, shared across all three pages.
 *
 * Every game with a "gemini_prediction" field (attached by the Python
 * builders when DK/FanDuel odds are posted) gets a collapsed toggle
 * button showing the confidence score, which expands to the full
 * winner / ATS / analysis panel. Uses click-delegation on whatever
 * container the games were rendered into, same pattern as
 * attachPickHandlers, so it survives re-renders without re-binding.
 */

function renderGeminiBlock(g) {
  const p = g.gemini_prediction;
  if (!p) return '';

  const fmt = (v) => (v !== undefined && v !== null && v !== '') ? `${v}%` : '—';

  // Main pick confidence
  const conf = fmt(p.confidence);

  // ATS confidence: use a dedicated field if your Python builder provides one,
  // otherwise fall back to the main confidence so it's never blank.
  const atsRaw = (p.ats_confidence !== undefined && p.ats_confidence !== null) ? p.ats_confidence
               : (p.ats_conf       !== undefined && p.ats_conf       !== null) ? p.ats_conf
               : p.confidence;
  const atsConf = fmt(atsRaw);

  return `<button type="button" class="gemini-toggle" aria-expanded="false">
    <span class="gemini-toggle-icon">&#10024;</span>
    <span class="gemini-toggle-main">Gemini Prediction Summary</span>
    <span class="gemini-toggle-trailing">
      <span class="gemini-picks-summary">
        <span class="gemini-toggle-winner">Pick: ${p.winner || 'TBD'} (${conf})</span>
        ${p.ats_pick ? `<span class="gemini-toggle-ats">ATS Pick: ${p.ats_pick} (${atsConf})</span>` : ''}
      </span>
      <span class="gemini-caret">▾</span>
    </span>
  </button>
  <div class="gemini-panel" hidden>
    <div class="gemini-panel-row"><span>Winner</span> <span class="gemini-value">${p.winner || '—'}</span></div>
    ${p.ats_pick ? `<div class="gemini-panel-row"><span>ATS Pick</span> <span class="gemini-value">${p.ats_pick}</span></div>` : ''}
    <div class="gemini-panel-row"><span>Confidence</span> <span class="gemini-value">${conf}</span></div>
    ${p.ats_pick ? `<div class="gemini-panel-row"><span>ATS Confidence</span> <span class="gemini-value">${atsConf}</span></div>` : ''}
    <p class="gemini-analysis">${p.analysis || ''}</p>
  </div>`;
}

function attachGeminiHandlers(containerEl) {
  containerEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.gemini-toggle');
    if (!btn || !containerEl.contains(btn)) return;
    const panel = btn.nextElementSibling;
    if (!panel || !panel.classList.contains('gemini-panel')) return;
    const isOpen = !panel.hidden;
    panel.hidden = isOpen;
    btn.classList.toggle('open', !isOpen);
    btn.setAttribute('aria-expanded', String(!isOpen));
  });
}

/*
 * Tiles / Rows view toggle, shared across all three pages.
 *
 * If the person has never picked a mode, it auto-picks Rows on wider
 * (desktop-ish) screens and Tiles on narrow (mobile) screens. Once they
 * click a toggle button, that explicit choice is remembered in
 * localStorage and wins from then on, on every page, until they change it
 * again or clear site data.
 */

const VIEW_MODE_KEY = 'fb_view_mode';
const VIEW_MODE_DESKTOP_BREAKPOINT = '(min-width: 860px)';

function getStoredViewMode() {
  const stored = localStorage.getItem(VIEW_MODE_KEY);
  return (stored === 'tiles' || stored === 'rows') ? stored : null;
}

function getEffectiveViewMode() {
  const stored = getStoredViewMode();
  if (stored) return stored;
  return window.matchMedia(VIEW_MODE_DESKTOP_BREAKPOINT).matches ? 'rows' : 'tiles';
}

function applyViewMode(mode) {
  document.body.classList.toggle('row-view', mode === 'rows');
  document.querySelectorAll('.view-toggle .view-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
}

function setViewMode(mode) {
  localStorage.setItem(VIEW_MODE_KEY, mode);
  applyViewMode(mode);
}

// Call once per page, after the nav (with its .view-toggle buttons) is in
// the DOM. Doesn't depend on board data having loaded.
function initViewToggle() {
  applyViewMode(getEffectiveViewMode());
  document.querySelectorAll('.view-toggle .view-btn').forEach(btn => {
    btn.addEventListener('click', () => setViewMode(btn.dataset.mode));
  });
  // If the person never explicitly chose a mode, keep following the
  // desktop/mobile default as the window is resized.
  window.matchMedia(VIEW_MODE_DESKTOP_BREAKPOINT).addEventListener('change', () => {
    if (!getStoredViewMode()) applyViewMode(getEffectiveViewMode());
  });
}

/*
 * Live scores overlay -- shared by index.html / nfl.html / picks.html.
 *
 * data/scores.json is written hourly by scripts/fetch_scores.py, separate
 * from the once-a-day dashboard builds, so scores can refresh far more
 * often without hitting SharpAPI's/Gemini's tighter rate limits. It's
 * shaped as {"cfb": {"<gameId>": {...}}, "nfl": {"<gameId>": {...}}} and
 * simply merged in here at render time by game id -- games with no entry
 * (not started yet) render exactly as before, with no score line.
 */

const SCORES_URL = 'data/scores.json';

// Fetches data/scores.json and returns its `cfb` or `nfl` lookup (by game
// id). Never throws -- if the file is missing or malformed (e.g. the
// hourly workflow hasn't run yet), returns {} so the rest of the page
// renders normally with no score lines.
async function fetchScores(sport) {
  try {
    const res = await fetch(SCORES_URL, { cache: 'no-store' });
    if (!res.ok) return {};
    const data = await res.json();
    return data[sport] || {};
  } catch (e) {
    return {};
  }
}

// Returns the small score badge to place right next to a team's name in
// the matchup title, or '' if this game has no score yet. Colored green
// once final, red while live (no color once the game hasn't started).
function teamScoreBadge(score, status) {
  if (score === null || score === undefined) return '';
  const cls = status === 'final' ? 'final' : status === 'in_progress' ? 'live' : '';
  return ` <span class="team-score ${cls}">${score}</span>`;
}

// Returns the "LIVE · Q3 08:42" line to place under the matchup title
// while a game is in progress, or '' once it's final (or hasn't started).
function renderLiveStatusLine(scoreEntry) {
  if (!scoreEntry || scoreEntry.status !== 'in_progress') return '';
  return `<div class="score-status-line"><span class="live-dot"></span>LIVE${scoreEntry.status_detail ? ' \u00b7 ' + scoreEntry.status_detail : ''}</div>`;
}
