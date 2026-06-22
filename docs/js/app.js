// Cloud URL — set after Cloudflare Worker setup (see cloudflare/SETUP.md)
// Leave empty ('') to fall back to GitHub Pages only.
const CLOUD_URL  = 'https://sportsbrain-signals.sportsbrain-philip.workers.dev/signals.json';
// VAPID Public Key (kann öffentlich sein) — wird via scripts/gen_vapid_keys.py
// erzeugt und hier eingetragen. Bei Platzhalter ist Push-Toggle deaktiviert.
const VAPID_PUBLIC_KEY = 'BCWFSMWF_b1ef9i76yoGltxiEEel_pJtXqjl-0q7ZYS3Ya2V9dBW5gN5N_rzdShckjkLq7UaI-5GPaV0PKc7ZBI';

// ── Utility: Expected Value ───────────────────────────────────
const calcEV = (p, q) => (p * q - 1) * 100;  // p: decimal prob, q: decimal odds
const DATA_URL   = 'data/signals.json';
const SQUADS_URL = 'data/squads.json';
let _signals = [];
let _schedule = [];
let _allOdds = {};
let _modelTips = {};
let _openBets = [];
let _settledBets = [];
let _activeBetTab = 'open';
let _liveScores = {};
let _bankrollState = {};
let _meta = {};
let _squads = {};
let _oddsHistory = {};
let _wmResults = {};  // {matchKey: {home_score, away_score}}
let _prevView = 'home';
let _homeSearch = '';
let _journalFilter = 'all';
// Sport-Tab Filter/Sort State (persistiert in LocalStorage)
const _SPORT_FILTER_KEY = 'sb_sport_filter_v1';
function _loadSportFilters() {
  try { return JSON.parse(localStorage.getItem(_SPORT_FILTER_KEY) || '{}') || {}; }
  catch { return {}; }
}
function _saveSportFilters(state) {
  try { localStorage.setItem(_SPORT_FILTER_KEY, JSON.stringify(state)); } catch {}
}
let _sportFilters = _loadSportFilters();
// Compact-Mode (Tier 4.1) — Tabellen-Layout statt Karten
let _compactMode = (() => {
  try { return localStorage.getItem('sb_compact_mode') === '1'; } catch { return false; }
})();
function toggleCompact() {
  _compactMode = !_compactMode;
  try { localStorage.setItem('sb_compact_mode', _compactMode ? '1' : '0'); } catch {}
  const btn = document.getElementById('compact-btn');
  if (btn) btn.classList.toggle('active', _compactMode);
  renderSport('football');
  renderSport('tennis');
}
function _getSportFilter(sport) {
  const def = { ev: 0, conf: 'all', sort: 'kickoff' };
  return Object.assign({}, def, _sportFilters[sport] || {});
}
function _setSportFilter(sport, patch) {
  _sportFilters[sport] = Object.assign(_getSportFilter(sport), patch);
  _saveSportFilters(_sportFilters);
}

// ── Navigation ───────────────────────────────────────────────
function navTo(tab) {
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.remove('active');
    t.setAttribute('aria-selected', 'false');
  });
  tab.classList.add('active');
  tab.setAttribute('aria-selected', 'true');
  _prevView = tab.dataset.view;
  showView(tab.dataset.view);
}
function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + id).classList.add('active');
  if (id === 'forecast') renderForecast();
}
function closeDetail() { showView(_prevView); }

// ── Swipe-back-Geste für Detail-View (WhatsApp-Style) ─────────
(function setupDetailSwipeBack() {
  const view = document.getElementById('view-detail');
  if (!view) return;
  let startX = null, startY = null, dx = 0, dy = 0, locked = null;
  const THRESHOLD_DIST = 90;   // px — Mindestdistanz für Auslösen
  const LOCK_ANGLE = 10;       // px — ab dann entscheiden wir horizontal vs vertikal
  function reset() {
    view.classList.remove('swiping', 'swiping-back');
    view.style.transform = '';
    startX = startY = null; dx = dy = 0; locked = null;
  }
  view.addEventListener('touchstart', (e) => {
    if (!view.classList.contains('active')) return;
    if (e.touches.length !== 1) return;
    // Inputs/Buttons: keinen Swipe starten, damit Quote-Tap nicht versehentlich auslöst
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button') return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    locked = null;
  }, { passive: true });
  view.addEventListener('touchmove', (e) => {
    if (startX === null) return;
    dx = e.touches[0].clientX - startX;
    dy = e.touches[0].clientY - startY;
    if (locked === null) {
      if (Math.abs(dx) < LOCK_ANGLE && Math.abs(dy) < LOCK_ANGLE) return;
      locked = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
      if (locked === 'h' && dx > 0) view.classList.add('swiping', 'swiping-back');
    }
    if (locked !== 'h' || dx <= 0) return;
    e.preventDefault();
    const t = Math.min(dx, window.innerWidth);
    view.style.transform = `translateX(${t}px)`;
  }, { passive: false });
  view.addEventListener('touchend', () => {
    if (startX === null) return;
    if (locked === 'h' && dx > THRESHOLD_DIST) {
      view.classList.remove('swiping');
      view.style.transform = `translateX(${window.innerWidth}px)`;
      setTimeout(() => { reset(); closeDetail(); }, 220);
    } else {
      view.classList.remove('swiping');
      view.style.transform = '';
      setTimeout(reset, 220);
    }
  });
  view.addEventListener('touchcancel', reset);
})();

// ── Open match detail ────────────────────────────────────────
function openMatch(displayKey) {
  const [dh, da] = displayKey.split(' vs ').map(x => x.trim());
  const nk = matchKey(dh, da);
  const sigs = _signals.filter(s => {
    const [sh, sa] = s.match.split(' vs ').map(x => x.trim());
    return matchKey(sh, sa) === nk;
  });
  // Find kickoff + sport from schedule
  const sched = _schedule.find(g => matchKey(g.home, g.away) === nk);
  const s0 = sigs[0];
  const sport = s0?.sport || sched?.sport || 'football';
  const kickoff = s0?.kickoff || sched?.kickoff || '';
  const tour = s0?.tour || sched?.tour || '';
  const sportIcon = sport === 'football' ? '⚽' : '🎾';
  const metaStr = kickoff ? fmtKickoffCompact(kickoff) : '';
  const cdHtml = kickoff ? `<span class="match-countdown" data-kickoff="${esc(kickoff)}" style="margin-top:0;font-size:10px;padding:2px 7px">⏱ …</span>` : '';
  document.getElementById('detail-header').innerHTML = `
    <div class="match-header">
      <span class="match-sport-icon">${sportIcon}</span>
      <span class="match-teams">${esc(displayKey)}</span>
      ${metaStr ? `<span class="match-meta">${metaStr}</span>` : ''}
      ${cdHtml}
    </div>`;
  _tickCountdowns();
  // Look up model tip — try exact key first, then normalized key
  let tip = _modelTips[displayKey];
  if (!tip) {
    for (const [k, v] of Object.entries(_modelTips)) {
      const [kh, ka] = k.split(' vs ').map(x => x.trim());
      if (matchKey(kh, ka) === nk) { tip = v; break; }
    }
  }
  // Find odds for this match
  let oddsEntry = _allOdds[displayKey];
  if (!oddsEntry) {
    const found = Object.entries(_allOdds).find(([k]) => {
      const [kh, ka] = k.split(' vs ').map(x => x.trim());
      return matchKey(kh, ka) === nk;
    });
    oddsEntry = found ? found[1] : {};
  }

  const ouSigs = sigs.filter(s => /^o\/u/.test(s.market));
  const otherSigs = sigs.filter(s => !/^o\/u/.test(s.market));

  let cards = '';
  if (tip) cards += predCard(dh, da, tip, oddsEntry, nk, kickoff, sport);
  if (sport === 'football') cards += _betHistoryCard(dh, da);
  cards += _bookieMatrixCard(oddsEntry, sport);

  if (!tip && !sigs.length) {
    cards += `<div class="empty"><div class="icon">⏳</div><div>Noch keine Modellbewertung für dieses Spiel.<br><small>Wird im nächsten Scan (täglich 08:00 UTC) ergänzt.</small></div></div>`;
  } else if (sigs.length) {
    cards += `<div style="font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;padding:14px 16px 6px">💡 Vorgeschlagene Value Bets</div>`;
    cards += otherSigs.map(s => sigCard(s, false)).join('');
    if (ouSigs.length) cards += buildOuAccordion(ouSigs, otherSigs.length === 0 || ouSigs.some(s => s.confidence === 'HIGH'));
  }

  if (sport === 'football') cards += squadSection(dh, da);

  document.getElementById('detail-cards').innerHTML = cards;
  showView('detail');
}

// ── Helpers ──────────────────────────────────────────────────
const esc = s => s.replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ── Spinner + Toast ───────────────────────────────────────────
function _spinner(on) {
  document.getElementById('spinner-overlay')?.classList.toggle('show', !!on);
}
let _toastTimer = null;
function showToast(msg, kind) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  // force reflow so re-trigger of CSS transition works
  void el.offsetWidth;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 4000);
}

// ── 🛡️ System-Health (Phase E) ───────────────────────────────
// Health-data comes inline in signals.json under key "health" — written by
// src/monitoring/aggregate_health.py every 2 minutes.
let _health = null;

function _healthLabel(s) {
  return s === 'ok' ? 'OK' :
         s === 'degraded' ? 'DEGRADED' :
         s === 'error' ? 'ERROR' :
         s === 'stale' ? 'STALE' : (s||'?').toUpperCase();
}

function _healthRelTime(iso) {
  if (!iso) return 'noch nie gelaufen';
  const dt = new Date(iso);
  const diffM = Math.round((Date.now() - dt.getTime()) / 60000);
  if (!isFinite(diffM)) return iso;
  if (diffM < 1) return 'gerade eben';
  if (diffM < 60) return `vor ${diffM} Min`;
  const h = Math.round(diffM / 60);
  if (h < 24) return `vor ${h} h`;
  const d = Math.round(h / 24);
  return `vor ${d} Tag(en)`;
}

function renderHealthSummary(h) {
  _health = h;
  const sumEl = document.getElementById('settings-health-summary');
  if (!sumEl) return;
  if (!h) { sumEl.textContent = 'Status: keine Daten'; return; }
  const total = (h.jobs||[]).length;
  const ok    = (h.jobs||[]).filter(j => j.status === 'ok').length;
  const bad   = (h.jobs||[]).filter(j => j.status === 'error').length;
  const deg   = (h.jobs||[]).filter(j => j.status === 'degraded' || j.status === 'stale').length;

  let label;
  if (h.overall === 'ok')       label = `✅ Alle ${total} Jobs ok`;
  else if (h.overall === 'down') label = `🔴 ${bad} Job(s) fehlgeschlagen · ${ok}/${total} ok`;
  else                            label = `⚠️ ${deg} Job(s) degraded · ${ok}/${total} ok`;
  sumEl.textContent = label;
}

function renderHealthDetail(h) {
  if (!h) h = _health;
  const list = document.getElementById('health-jobs-list');
  const top  = document.getElementById('health-overall-line');
  if (!list || !top || !h) return;
  const overallLabel = h.overall === 'ok' ? '✅ Alles läuft' :
                       h.overall === 'down' ? '🔴 System gestört' :
                       '⚠️ Eingeschränkter Betrieb';
  top.innerHTML = `${overallLabel} · zuletzt aktualisiert: ${_healthRelTime(h.generated_at)}`;
  list.innerHTML = (h.jobs||[]).map(j => {
    const pillCls = j.status === 'ok' ? 'ok' :
                    j.status === 'error' ? 'error' :
                    j.status === 'degraded' ? 'degraded' : 'stale';
    const subLines = [];
    subLines.push(`<div class="health-job-sub">${esc(j.cadence||'')} · ${_healthRelTime(j.last_run_at)}</div>`);
    if (j.fallback_used) {
      subLines.push(`<div class="health-job-sub">Fallback aktiv: <b>${esc(j.fallback_used)}</b></div>`);
    }
    if (j.error) {
      subLines.push(`<div class="health-job-err">${esc(String(j.error).slice(0, 240))}</div>`);
    }
    return `
      <div class="health-job">
        <div class="health-job-name">
          <div class="health-job-title">${esc(j.job)}</div>
          ${subLines.join('')}
        </div>
        <span class="health-pill ${pillCls}">${_healthLabel(j.status)}</span>
      </div>`;
  }).join('') || '<div class="settings-row-sub">Keine Job-Daten</div>';
}

function _openHealthDetail() {
  // Close settings first if open, then open the sub-modal.
  _closeSettings();
  renderHealthDetail(_health);
  const bd = document.getElementById('health-detail-modal-bd');
  if (bd) bd.classList.add('show');
}
function _closeHealthDetail() {
  const bd = document.getElementById('health-detail-modal-bd');
  if (bd) bd.classList.remove('show');
}

// Toast on first transition to degraded/down — throttled via localStorage so
// the user doesn't see the same warning every 60s. When the system recovers,
// reset state so a future regression triggers a fresh toast.
function _maybeShowHealthToast(h) {
  if (!h) return;
  let last;
  try { last = localStorage.getItem('health.lastSeenOverall'); } catch (_) {}

  if (h.overall === 'ok') {
    if (last && last !== 'ok') {
      try { localStorage.setItem('health.lastSeenOverall', 'ok'); } catch (_) {}
      showToast('✅ System wieder ok', 'success');
    }
    return;
  }

  if (last === h.overall) return;  // same warn state already announced
  try { localStorage.setItem('health.lastSeenOverall', h.overall); } catch (_) {}

  const msg = h.overall === 'down'
    ? '🔴 System-Job fehlgeschlagen — Settings → System-Status'
    : '⚠️ System läuft mit Einschränkungen — Details in Settings';
  showToast(msg, 'error');
}

// ── Tooltip: tap-toggle. esc() den body, weil er als innerHTML reingeht ──
function infoTip(body) {
  return `<span class="tip" onclick="event.stopPropagation();_toggleTip(this)" aria-label="Info">i<span class="tip-pop">${esc(body)}</span></span>`;
}
function _toggleTip(el) {
  const isOpen = el.classList.contains('open');
  document.querySelectorAll('.tip.open').forEach(t => t.classList.remove('open'));
  if (!isOpen) el.classList.add('open');
}
// Outside-Tap schließt sofort und schluckt den Tap, damit nicht versehentlich
// ein anderes Element (Match-Row, Nav-Tab) aktiviert wird.
// Ausnahme: Buttons werden NICHT geschluckt — der User will Tip schließen UND
// den Button drücken (z.B. "Wette platzieren").
function _closeAllTipsCapture(e) {
  if (e.target.closest('.tip')) return;
  const open = document.querySelectorAll('.tip.open');
  if (!open.length) return;
  open.forEach(t => t.classList.remove('open'));
  if (e.target.closest('button')) return;
  e.preventDefault();
  e.stopPropagation();
}
document.addEventListener('pointerdown', _closeAllTipsCapture, true);
document.addEventListener('touchstart', _closeAllTipsCapture, {capture:true, passive:false});
document.addEventListener('click', _closeAllTipsCapture, true);

// Normalize team names to match between scanner sources and schedule
const TEAM_ALIASES = {
  'czechia': 'czech republic',
  'united states': 'usa',
  "cote d'ivoire": 'ivory coast',
};
const COUNTRY_FLAGS = {
  'Mexico':'🇲🇽','South Africa':'🇿🇦','South Korea':'🇰🇷','Czechia':'🇨🇿','Czech Republic':'🇨🇿',
  'Canada':'🇨🇦','Bosnia and Herzegovina':'🇧🇦','Bosnia & Herzegovina':'🇧🇦','Qatar':'🇶🇦','Switzerland':'🇨🇭',
  'Brazil':'🇧🇷','Morocco':'🇲🇦','Haiti':'🇭🇹','Scotland':'🏴󠁧󠁢󠁳󠁣󠁴󠁿',
  'United States':'🇺🇸','USA':'🇺🇸','Paraguay':'🇵🇾','Australia':'🇦🇺','Turkey':'🇹🇷',
  'Germany':'🇩🇪','Curacao':'🇨🇼','Curaçao':'🇨🇼',"Cote d'Ivoire":'🇨🇮','Ivory Coast':'🇨🇮','Ecuador':'🇪🇨',
  'Netherlands':'🇳🇱','Japan':'🇯🇵','Sweden':'🇸🇪','Tunisia':'🇹🇳',
  'Belgium':'🇧🇪','Egypt':'🇪🇬','Iran':'🇮🇷','New Zealand':'🇳🇿',
  'Spain':'🇪🇸','Cape Verde':'🇨🇻','Saudi Arabia':'🇸🇦','Uruguay':'🇺🇾',
  'France':'🇫🇷','Senegal':'🇸🇳','Iraq':'🇮🇶','Norway':'🇳🇴',
  'Argentina':'🇦🇷','Algeria':'🇩🇿','Austria':'🇦🇹','Jordan':'🇯🇴',
  'Portugal':'🇵🇹','DR Congo':'🇨🇩','Uzbekistan':'🇺🇿','Colombia':'🇨🇴',
  'England':'🏴󠁧󠁢󠁥󠁮󠁧󠁿','Croatia':'🇭🇷','Ghana':'🇬🇭','Panama':'🇵🇦',
  'Italy':'🇮🇹','Ukraine':'🇺🇦','Poland':'🇵🇱','Serbia':'🇷🇸','Denmark':'🇩🇰',
  'Romania':'🇷🇴','Hungary':'🇭🇺','Greece':'🇬🇷','Nigeria':'🇳🇬','Cameroon':'🇨🇲',
  'Chile':'🇨🇱','Peru':'🇵🇪','Bolivia':'🇧🇴','Venezuela':'🇻🇪','Costa Rica':'🇨🇷',
  'Honduras':'🇭🇳','Jamaica':'🇯🇲','El Salvador':'🇸🇻','Suriname':'🇸🇷',
  'Wales':'🏴󠁧󠁢󠁷󠁬󠁳󠁿','Ireland':'🇮🇪','Slovakia':'🇸🇰','Bulgaria':'🇧🇬','Finland':'🇫🇮',
  'Russia':'🇷🇺','Belarus':'🇧🇾','China':'🇨🇳','Israel':'🇮🇱','Kazakhstan':'🇰🇿',
};
function teamFlag(name) { return COUNTRY_FLAGS[name] || ''; }
function normTeam(name) {
  return name.normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().replace(/&/g,' and ').replace(/[^a-z0-9 ']/g,'').replace(/\s+/g,' ').trim();
}
function matchKey(a, b) {
  const na = normTeam(a), nb = normTeam(b);
  const na2 = TEAM_ALIASES[na] || na, nb2 = TEAM_ALIASES[nb] || nb;
  return [na2, nb2].join(' vs ');
}

function fmtKickoff(k) {
  try {
    return new Date(k).toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Berlin'});
  } catch { return ''; }
}

function fmtKickoffCompact(k) {
  // "Mo 15.06 · 21:00"
  try {
    const d = new Date(k);
    const wd = d.toLocaleDateString('de-DE',{weekday:'short',timeZone:'Europe/Berlin'}).replace('.','');
    const date = d.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit',timeZone:'Europe/Berlin'});
    const time = d.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Berlin'});
    return `${wd} ${date} · ${time}`;
  } catch { return ''; }
}

function fmtTime(k) {
  try {
    return new Date(k).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Berlin'}) + ' Uhr';
  } catch { return ''; }
}

function marketLabel(mkt, match) {
  const p = match.split(' vs ');
  const a = p[0]?.trim()||'?', b = p[1]?.trim()||'?';
  const known = {
    home:`Sieg ${a}`, draw:'Unentschieden', away:`Sieg ${b}`,
    'btts_yes':'Beide treffen','btts_no':'Nicht beide treffen',
    'ftts_home':`1. Tor: ${a}`,'ftts_away':`1. Tor: ${b}`,
    'first_set_a':`1. Satz: ${a}`,'first_set_b':`1. Satz: ${b}`,
    'ah-1.5_a':`${a} 3:0 / 3:1`,'ah+1.5_b':`${b} +1.5 Sätze`,
    'dc_1x':`DC: ${a} oder Remis`,'dc_x2':`DC: Remis oder ${b}`,'dc_12':`DC: ${a} oder ${b}`,
    'goals_2_4':'2-4 Tore (Spiel)','goals_2_4_no':'KEIN 2-4 Tore (Spiel)',
    'h1_goals_2_4':'2-4 Tore (1. HZ)','h1_goals_2_4_no':'KEIN 2-4 Tore (1. HZ)',
    'h2_goals_2_4':'2-4 Tore (2. HZ)','h2_goals_2_4_no':'KEIN 2-4 Tore (2. HZ)',
  };
  if (known[mkt]) return known[mkt];
  const ouM = mkt.match(/^o\/u([\d.]+)_(over|under)$/);
  if (ouM) return ouM[2] === 'over' ? `Über ${ouM[1]} Tore` : `Unter ${ouM[1]} Tore`;
  const ahM = mkt.match(/^ah([+-][\d.]+)_(home|away)$/);
  if (ahM) return `AH ${ahM[1]} ${ahM[2] === 'home' ? a : b}`;
  const scM = mkt.match(/^scorer_(.+)$/);
  if (scM) return `⚽ Torschütze: ${scM[1]}`;
  return mkt;
}

// ── Odds movement badge (line movement) ──────────────────────
function oddsMovement(nk, side) {
  // Find match history via normalized key
  let hist = null;
  for (const [k, v] of Object.entries(_oddsHistory)) {
    const [kh, ka] = k.split(' vs ').map(x => x.trim());
    if (matchKey(kh, ka) === nk) { hist = v; break; }
  }
  if (!hist || hist.length < 2) return '';
  const prev = hist[hist.length - 2]; // yesterday
  const curr = hist[hist.length - 1]; // today
  const prevOdds = prev[side];
  const currOdds = curr[side];
  if (!prevOdds || !currOdds) return '';
  const delta = currOdds - prevOdds;
  if (Math.abs(delta) < 0.02) return '';
  const isGood = delta > 0; // rising odds = more value for bettor
  const cls = isGood ? 'drift-good' : 'drift-bad';
  const arrow = isGood ? '▲' : '▼';
  return `<span class="${cls}" style="font-size:10px;font-weight:800;margin-left:4px">${arrow}${Math.abs(delta).toFixed(2)}</span>`;
}

// ── Model prediction card ─────────────────────────────────────
// ── Render Home — Bet365-style with inline odds ───────────────
function _updateClock() {
  const el = document.getElementById('today-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Berlin'}) + ' Uhr';
}
setInterval(_updateClock, 30000);

// ── Live-Countdown (Detail-View) ──────────────────────────────
function _tickCountdowns() {
  const els = document.querySelectorAll('.match-countdown[data-kickoff]');
  if (!els.length) return;
  const now = Date.now();
  els.forEach(el => {
    const k = el.getAttribute('data-kickoff');
    if (!k) return;
    const t = new Date(k).getTime();
    if (isNaN(t)) return;
    const diff = t - now;
    let cls = 'match-countdown', txt;
    if (diff <= -7200000) { cls += ' done'; txt = '✓ Abgeschlossen'; }
    else if (diff <= 0)   { cls += ' live'; txt = '🔴 LIVE jetzt'; }
    else if (diff <= 300000) { cls += ' live'; txt = `⏱ ${Math.ceil(diff/1000)}s — gleich Anpfiff`; }
    else if (diff <= 3600000) { cls += ' warn'; const m = Math.floor(diff/60000); txt = `⏱ ${m}m bis Anpfiff`; }
    else {
      const h = Math.floor(diff/3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      txt = `⏱ ${h}h ${m}m bis Anpfiff`;
    }
    el.className = cls;
    el.textContent = txt;
  });
}
setInterval(_tickCountdowns, 1000);

let _cloudHealthy = true;
async function _load() {
  // Während aktiver Walkthrough-Demo keine Live-Daten ziehen/überschreiben
  if (_walkDemoActive) return;
  const ts = '?t=' + Date.now();
  let r, cloudFailed = false;
  if (CLOUD_URL) {
    try { r = await fetch(CLOUD_URL + ts, { cache: 'no-store' }); } catch (_) { cloudFailed = true; }
    if (!r || !r.ok) cloudFailed = true;
  }
  if (!r || !r.ok) r = await fetch(DATA_URL + ts, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  // Cloud-Erreichbarkeit nur einmal melden, wenn sich der Zustand ändert
  if (cloudFailed && _cloudHealthy) {
    showToast('⚠️ Cloud nicht erreichbar — Fallback auf GitHub-Daten (ggf. veraltet)', 'error');
    _cloudHealthy = false;
  } else if (!cloudFailed && !_cloudHealthy) {
    _cloudHealthy = true;
  }
  const d = await r.json();

  const dt = new Date(d.updated), age = (Date.now()-dt)/36e5;
  document.getElementById('updated-time').textContent =
    dt.toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  document.getElementById('stale-banner').style.display = age > 1.5 ? 'block' : 'none';
  document.getElementById('dot').style.background = age > 1.5 ? 'var(--yellow)' : 'var(--green)';

  // C5: Build-Info Pille im Footer (aus signals.json.build_info)
  const bi = d.build_info || {};
  const pill = document.getElementById('build-pill');
  if (pill && (bi.sha || bi.date)) {
    const dateStr = bi.date ? String(bi.date).slice(0,10) : '';
    const shaStr  = bi.sha  ? String(bi.sha).slice(0,7)   : '';
    pill.textContent = `v${dateStr}${dateStr && shaStr ? ' · ' : ''}${shaStr}`;
    pill.title = `Build ${shaStr || '?'} · ${bi.date || '?'}`;
    pill.style.display = 'inline-block';
  }

  _signals = [...(d.football||[]), ...(d.tennis||[])];
  _schedule = d.schedule || [];
  _allOdds = d.all_odds || {};
  _modelTips = d.model_tips || {};
  _openBets = d.open_bets || [];
  _settledBets = d.settled_bets || [];
  _bankrollState = d.bankroll_state || {};
  // D4/D5: localStorage-Bankroll übernimmt, wenn Backend nur den €100-Default
  // liefert UND der User noch keine Aktivität hat (neuer User-Slot).
  try {
    const saved = parseFloat(localStorage.getItem('sb_bankroll_start'));
    const bs = _bankrollState;
    const hasActivity = (bs && bs.pnl_closed && bs.pnl_closed !== 0) ||
                        (bs && bs.staked && bs.staked > 0);
    const isDefault = !bs || !bs.start || bs.start === 100;
    if (saved >= 10 && isDefault && !hasActivity) {
      _bankrollState = { start: saved, free: saved, pnl_closed: 0, staked: 0, exposure_pct: 0, max_win: 0 };
    }
  } catch {}
  _meta = d.meta || {};
  _oddsHistory = d.odds_history || {};
  // Index wm_results by matchKey for fast lookup in Home tab
  _wmResults = {};
  for (const r of (d.wm_results || [])) {
    const mk = matchKey(r.home, r.away);
    _wmResults[mk] = r;
  }
  renderBankrollStrip();

  const setB = (id, n) => { const el=document.getElementById(id); el.textContent=n; el.classList.toggle('show',n>0); };
  setB('badge-all',      _signals.length);
  setB('badge-football', _signals.filter(s=>s.sport==='football').length);
  setB('badge-tennis',   _signals.filter(s=>s.sport==='tennis').length);
  const setBBets = document.getElementById('badge-bets');
  if (setBBets) { setBBets.textContent = _openBets.length; setBBets.classList.toggle('show', _openBets.length > 0); }

  // 🛡️ System-Health (Phase E) — health is injected into signals.json by
  // src/monitoring/aggregate_health.py. Render the settings summary and
  // pop a toast on state transitions.
  if (d.health) {
    renderHealthSummary(d.health);
    _maybeShowHealthToast(d.health);
  } else {
    // Reset memory if no health data is present so a later transition still pops a toast.
    try { localStorage.removeItem('health.lastSeenOverall'); } catch (_) {}
  }
  // If the user has the detail modal open, refresh it live too.
  if (document.getElementById('health-detail-modal-bd')?.classList.contains('show')) {
    renderHealthDetail(d.health);
  }

  _wmStats = d.wm_stats || {};
  renderHome();
  renderSport('football');
  renderSport('tennis');
  renderJournalStats(_wmStats);
  renderJournal(d.history || []);
  renderStandings();
  renderBets();
  _updateClock();

  // Load squads in background (non-blocking)
  fetch(SQUADS_URL + '?t=' + Date.now(), { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null)
    .then(sd => { if (sd) _squads = sd.teams || {}; })
    .catch(err => {
      const c = document.getElementById('squads-container');
      if (c && !c.querySelector('.squad-team-row')) {
        c.innerHTML = `<div class="empty"><div class="icon">⚠️</div><div>Kader konnten nicht geladen werden.<br><small style="color:var(--muted)">${esc(err.message)}</small></div></div>`;
      }
    });
}

function _renderApiFailEmpty(msg) {
  const containers = ['home-container', 'bets-container', 'journal-container', 'forecast-container', 'football-container', 'tennis-container'];
  const html = `<div class="empty"><div class="icon">⚠️</div>
    <div>Daten konnten nicht geladen werden.<br><small style="color:var(--muted)">${esc(msg)}</small></div>
    <button class="modal-btn confirm" type="button" onclick="load()" style="margin-top:14px;min-width:140px">↺ Neu laden</button>
  </div>`;
  for (const id of containers) {
    const el = document.getElementById(id);
    if (el && (el.querySelector('.skel') || el.querySelector('.empty') || !el.children.length)) {
      el.innerHTML = html;
    }
  }
}

async function load() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = '↺…';
  _spinner(true);
  try { await _load(); } catch(e) {
    _renderApiFailEmpty(e.message);
    showToast('⚠️ Daten konnten nicht geladen werden: ' + e.message, 'error');
  } finally { btn.textContent = '↺'; _spinner(false); }
}

setInterval(load, 60*1000);

