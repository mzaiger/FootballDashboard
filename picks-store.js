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
 * "now". Each build stamps the JSON with a top-level `current_week`: the
 * same week number the build itself resolved as current (CFB's date-based
 * derive_week(), or NFL's ESPN-lookup-plus-stuck-week-fallback in
 * resolve_current_week() -- see scripts/build_dashboard.py /
 * build_nfl_dashboard.py). College/NFL just display `current_week` and
 * `current_week + 1`; everything else stays in the JSON (for Picks) but
 * off the boards. This intentionally does NOT try to guess "current" from
 * game kickoff times client-side -- it trusts whatever the last build run
 * decided, which only changes once a day when the build actually runs.
 */

// Returns just the weeks College/NFL should render: the week matching
// data.current_week, plus the one right after it, pulled out of the full
// `weeks` array by week number (not by array position -- a week can be
// briefly missing if a build hasn't run yet for it). Picks.html does NOT
// use this -- it shows every week that's ever had a pick made against it.
function selectDisplayWeeks(weeks, currentWeek) {
  if (!weeks || !weeks.length) return [];
  if (currentWeek === undefined || currentWeek === null) {
    // Older cached JSON with no current_week field yet -- fall back to
    // the two most recently stored weeks rather than showing nothing.
    return weeks.slice(-2);
  }
  return weeks.filter(w => w.week === currentWeek || w.week === currentWeek + 1);
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

// Summary across every pick ever made (any week, any sport), for the
// badges at the top of the Picks page. `datasets` is an array of
// {sport, data} (the full, unfiltered dashboard JSON for each sport);
// `scores` is {cfb: {...}, nfl: {...}} from data/scores.json. A push/tie
// counts as correct, matching the same convention oddsHitClass() already
// uses for cell coloring.
//
// Every picked game falls into exactly one bucket:
//   - active: the game hasn't been graded yet (not finished, or no score
//     posted yet) -- the pick is still "live"
//   - inactive: the game is graded (win, loss, or push) -- inactiveCorrect
//     is how many of those inactive picks hit
function computePickRecord(datasets, scores) {
  let active = 0, inactiveTotal = 0, inactiveCorrect = 0;
  (datasets || []).forEach(({ sport, data }) => {
    if (!data) return;
    (data.weeks || []).forEach(week => (week.days || []).forEach(day => (day.time_slots || []).forEach(slot => {
      (slot.games || []).forEach(g => {
        if (!getPick(sport, g.id)) return;
        const gScore = (scores[sport] || {})[String(g.id)];
        const outcome = pickOutcome(sport, g, gScore);
        if (outcome === 'hit') { inactiveTotal++; inactiveCorrect++; }
        else if (outcome === 'miss') { inactiveTotal++; }
        else { active++; }
      });
    })));
  });
  return { active, inactiveTotal, inactiveCorrect };
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

  // Splits and Direction: dedicated field from newer cache entries, with a
  // fallback that pulls it out of display_text for any older cached
  // predictions that predate this field.
  let splits = p.splits_and_direction || '';
  if (!splits && p.display_text) {
    const match = p.display_text.match(/Splits and Direction:\s*([\s\S]*)/i);
    if (match) splits = match[1].trim();
  }

  return `<button type="button" class="gemini-toggle" aria-expanded="false">
    <span class="gemini-toggle-icon">&#10024;</span>
    <span class="gemini-toggle-main">Gemini Prediction Summary</span>
    <span class="gemini-toggle-trailing">
      <span class="gemini-picks-summary">
        <span class="gemini-toggle-winner">ML: ${p.winner || 'TBD'} (${conf})</span>
        ${p.ats_pick ? `<span class="gemini-toggle-ats">ATS: ${p.ats_pick} (${atsConf})</span>` : ''}
      </span>
      <span class="gemini-caret">▾</span>
    </span>
  </button>
  <div class="gemini-panel" hidden>
    <div class="gemini-panel-row"><span>Winner</span> <span class="gemini-value">${p.winner || '—'}</span></div>
    ${p.ats_pick ? `<div class="gemini-panel-row"><span>ATS</span> <span class="gemini-value">${p.ats_pick}</span></div>` : ''}
    <div class="gemini-panel-row"><span>ML Confidence</span> <span class="gemini-value">${conf}</span></div>
    ${p.ats_pick ? `<div class="gemini-panel-row"><span>ATS Confidence</span> <span class="gemini-value">${atsConf}</span></div>` : ''}
    <p class="gemini-analysis"><strong>Analysis:</strong> ${p.analysis || ''}</p>
    ${splits ? `<p class="gemini-analysis"><strong>Splits and Direction:</strong> ${splits}</p>` : ''}
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
 * Board filters (Best Matchup Only / Min Conf / ML-ATS) -- shared by
 * index.html and nfl.html. Not used on picks.html, which shows every pick
 * ever made rather than the current board.
 *
 * Three controls, all optional and combinable:
 * - "Best Matchup Only": keep only each slot's is_slot_pick game.
 * - "Min Conf": keep only games whose Gemini confidence is at least this
 *   number, in steps of 5 (0 = off / no games excluded on this basis).
 * - ML / ATS: which Gemini confidence field "Min Conf" reads --
 *   moneyline/straight-up confidence, or the ATS pick's confidence (a game
 *   with no posted ATS pick fails this filter, since there's nothing to
 *   compare against).
 *
 * The chosen state persists in localStorage per device, same pattern as
 * the Tiles/Rows view toggle below.
 */

const FILTER_STATE_KEY = 'fb_filter_state';

function getFilterState() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(FILTER_STATE_KEY));
  } catch (e) { /* ignore malformed value */ }
  return {
    bestMatchupOnly: !!(stored && stored.bestMatchupOnly),
    minConf: (stored && Number.isInteger(stored.minConf)) ? stored.minConf : 0,
    confType: (stored && (stored.confType === 'ats' || stored.confType === 'ml')) ? stored.confType : 'ml',
  };
}

function saveFilterState(state) {
  localStorage.setItem(FILTER_STATE_KEY, JSON.stringify(state));
}

function gamePassesFilters(g, state) {
  if (state.bestMatchupOnly && !g.is_slot_pick) return false;
  if (state.minConf > 0) {
    const pred = g.gemini_prediction;
    if (!pred) return false;
    if (state.confType === 'ats') {
      if (!pred.ats_pick) return false;
      if ((pred.ats_confidence ?? 0) < state.minConf) return false;
    } else {
      if ((pred.confidence ?? 0) < state.minConf) return false;
    }
  }
  return true;
}

// Returns a filtered copy of `weeks` -- games that don't pass, then any
// slot/day left with zero games, are dropped, with game_count/total_games
// recomputed at each level. Passing the same `weeks` array back out
// unfiltered (no clone) when no filter is active avoids needless work on
// every render.
function filterWeeksForDisplay(weeks, state) {
  if (!state.bestMatchupOnly && state.minConf <= 0) return weeks;
  return weeks.map(week => {
    const days = week.days
      .map(day => {
        const time_slots = day.time_slots
          .map(slot => ({ ...slot, games: slot.games.filter(g => gamePassesFilters(g, state)) }))
          .filter(slot => slot.games.length);
        const game_count = time_slots.reduce((n, s) => n + s.games.length, 0);
        return { ...day, time_slots, game_count };
      })
      .filter(day => day.time_slots.length);
    const total_games = days.reduce((n, d) => n + d.game_count, 0);
    return { ...week, days, total_games, _filtered: true };
  });
}

// Call once per page, after the nav (with its #bestMatchupOnly,
// #minConfSelect, and .conf-type-btn controls) is in the DOM. `onChange`
// re-renders the board using the new filter state.
function initFilterBar(onChange) {
  const state = getFilterState();

  const bestMatchupEl = document.getElementById('bestMatchupOnly');
  const minConfEl = document.getElementById('minConfSelect');
  const confTypeBtns = document.querySelectorAll('.conf-type-btn');

  if (bestMatchupEl) bestMatchupEl.checked = state.bestMatchupOnly;
  if (minConfEl) minConfEl.value = String(state.minConf);
  confTypeBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.conftype === state.confType));

  if (bestMatchupEl) {
    bestMatchupEl.addEventListener('change', () => {
      state.bestMatchupOnly = bestMatchupEl.checked;
      saveFilterState(state);
      onChange();
    });
  }
  if (minConfEl) {
    minConfEl.addEventListener('change', () => {
      state.minConf = parseInt(minConfEl.value, 10) || 0;
      saveFilterState(state);
      onChange();
    });
  }
  confTypeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      state.confType = btn.dataset.conftype;
      saveFilterState(state);
      confTypeBtns.forEach(b => b.classList.toggle('active', b === btn));
      onChange();
    });
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
