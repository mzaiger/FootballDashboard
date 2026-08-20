/* site-key.js
 * Shared abbreviation key + cookie-policy note, injected on every page.
 * Self-contained (own CSS + markup) so it doesn't depend on any given
 * page's :root color variables or nav layout -- just include this file
 * with a <script src="site-key.js"></script> tag and it wires itself up.
 */
(function () {
  const STYLE = `
    #keyFabBtn{
      position:fixed; right:1rem; bottom:1rem; z-index:400;
      font-family:'Oswald',sans-serif; font-weight:600; font-size:0.62rem;
      text-transform:uppercase; letter-spacing:0.04em;
      color:#e8ecef; background:#12161b; border:1px solid #2a323a;
      border-radius:999px; padding:0.28rem 0.55rem; cursor:pointer;
      box-shadow:0 4px 12px -6px rgba(0,0,0,0.6);
      display:flex; align-items:center; gap:0.25rem;
    }
    #keyFabBtn:hover{border-color:#e0a63c; color:#e0a63c;}
    #keyFabBtn .key-fab-icon{font-size:0.72rem;}
    #keyModalOverlay{
      display:none; position:fixed; inset:0; z-index:500;
      background:rgba(4,6,8,0.72);
      align-items:center; justify-content:center; padding:1.25rem;
    }
    #keyModalOverlay.open{display:flex;}
    #keyModal{
      width:100%; max-width:34rem; max-height:85vh; overflow-y:auto;
      background:#12161b; border:1px solid #2a323a; border-radius:12px;
      padding:1.5rem 1.5rem 1.25rem; font-family:'JetBrains Mono',monospace;
      color:#e8ecef;
    }
    #keyModal h2{
      font-family:'Oswald',sans-serif; font-weight:700; font-size:1.15rem;
      margin:0 0 0.9rem; text-transform:uppercase; letter-spacing:0.03em;
      color:#e0a63c; display:flex; align-items:center; justify-content:space-between;
      gap:1rem;
    }
    #keyModalClose{
      background:transparent; border:1px solid #2a323a; color:#8a95a1;
      border-radius:6px; font-size:0.85rem; padding:0.15rem 0.55rem; cursor:pointer;
      font-family:'JetBrains Mono',monospace;
    }
    #keyModalClose:hover{color:#e8ecef; border-color:#e8ecef;}
    #keyModal dl{margin:0 0 1.1rem;}
    #keyModal dt{
      font-family:'Oswald',sans-serif; font-weight:700; font-size:0.85rem;
      color:#43c179; display:inline-block; min-width:3.4rem;
    }
    #keyModal dd{margin:0 0 0.45rem; display:inline; color:#c3cad1; font-size:0.85rem;}
    #keyModal .key-row{margin-bottom:0.2rem;}
    #keyModal .key-note{
      font-size:0.78rem; color:#8a95a1; line-height:1.5;
      border-top:1px dashed #2a323a; padding-top:0.9rem; margin-top:0.2rem;
    }
    #keyModal .key-note + .key-note{border-top:none; padding-top:0;}
  `;

  function injectStyle() {
    const s = document.createElement('style');
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function buildModal() {
    const overlay = document.createElement('div');
    overlay.id = 'keyModalOverlay';
    overlay.innerHTML = `
      <div id="keyModal" role="dialog" aria-modal="true" aria-labelledby="keyModalTitle">
        <h2 id="keyModalTitle">Key
          <button type="button" id="keyModalClose" aria-label="Close">Close &times;</button>
        </h2>
        <dl>
          <div class="key-row"><dt>ML</dt><dd>Money Line -- bet on which team wins outright, no spread involved.</dd></div>
          <div class="key-row"><dt>ATS</dt><dd>Against The Spread -- bet on a team to cover the posted point spread.</dd></div>
          <div class="key-row"><dt>DK</dt><dd>DraftKings sportsbook.</dd></div>
          <div class="key-row"><dt>FD</dt><dd>FanDuel sportsbook.</dd></div>
          <div class="key-row"><dt>AP</dt><dd>The AP (Associated Press) Top 25 poll, used for college football rankings.</dd></div>
          <div class="key-row"><dt>CFB / NCAAF</dt><dd>College football.</dd></div>
          <div class="key-row"><dt>NCAAMB</dt><dd>College basketball (men's).</dd></div>
          <div class="key-row"><dt>NFL</dt><dd>National Football League (pro football).</dd></div>
          <div class="key-row"><dt>MLB</dt><dd>Major League Baseball.</dd></div>
        </dl>
        <div class="key-note">Matchup / Slot Pick badges rank games by a blend of team rank, record, and posted spread -- see each page's footer for the exact formula.</div>
        <div class="key-note"><b>+ / - odds:</b> a minus number is the favorite -- it's how much you'd need to bet to win $100 (e.g. -150 means bet $150 to win $100). A plus number is the underdog -- it's how much you'd win on a $100 bet (e.g. +150 means bet $100 to win $150). For a $10 bet: on -150, profit is 10 / (150/100) = $6.67; on +150, profit is 10 * (150/100) = $15. Either way you also get your original $10 back on a win. This applies the same way to ATS spread prices (usually around -110) and to moneyline prices.</div>
        <div class="key-note">Picks are saved in your browser's cookies and expire every <b>August 1st</b>, just before the next season's week 1 -- so last season's picks clear out on their own before the new one starts.</div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeModal();
    });
    document.getElementById('keyModalClose').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModal();
    });
  }

  function openModal() {
    document.getElementById('keyModalOverlay').classList.add('open');
  }
  function closeModal() {
    document.getElementById('keyModalOverlay').classList.remove('open');
  }

  function buildButton() {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'keyFabBtn';
    btn.innerHTML = '<span class="key-fab-icon">&#128273;</span> Key';
    btn.addEventListener('click', openModal);
    document.body.appendChild(btn);
  }

  function init() {
    injectStyle();
    buildModal();
    buildButton();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
