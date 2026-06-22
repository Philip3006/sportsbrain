// ── Deep-link: öffnet Bet-Modal direkt aus ?bet=MATCH:MARKET ──
function _openBetModalForBetId(betId) {
  const colonIdx = betId.lastIndexOf(':');
  if (colonIdx < 0) return;
  const matchStr = decodeURIComponent(betId.slice(0, colonIdx));
  const market   = decodeURIComponent(betId.slice(colonIdx + 1));
  const sig = _signals.find(s => s.match === matchStr && s.market === market);
  if (!sig) return;
  // Navigate to right tab first
  const tab = document.querySelector(`[data-view="${sig.sport || 'football'}"]`);
  if (tab) navTo(tab);
  // Synthetic button carries all dataset attrs _openBetModalFromBtn expects
  const btn = document.createElement('button');
  btn.dataset.match      = sig.match;
  btn.dataset.market     = sig.market;
  btn.dataset.odds       = sig.odds;
  btn.dataset.stake      = sig.stake_eur;
  btn.dataset.ev         = sig.ev_pct;
  btn.dataset.modelProb  = sig.model_prob || 0;
  btn.dataset.fairProb   = sig.fair_prob || 0;
  btn.dataset.confidence = sig.confidence || '';
  btn.dataset.kickoff    = sig.kickoff || '';
  btn.dataset.sport      = sig.sport || '';
  _openBetModalFromBtn(btn);
}

// ── Render open bets tab ─────────────────────────────────────
function _pwaPendingHtml() {
  if (!_pwaPendingIds.length) return '';
  // Eintrag entfernen, wenn schon im Server-Ledger gelandet
  const now = Date.now();
  const stillPending = _pwaPendingIds.filter(p => {
    if (now - p.ts > 6 * 3600000) return false; // 6h alte Pending verwerfen
    const match = (p.match || '').toLowerCase();
    return !_openBets.some(b => {
      const bm = (b.match || (b.home + ' vs ' + b.away) || '').toLowerCase();
      return bm === match && (b.market || '') === (p.market || '');
    });
  });
  if (stillPending.length !== _pwaPendingIds.length) _savePwaPending(stillPending);
  if (!stillPending.length) return '';
  return stillPending.map(p => {
    const [h, a] = (p.match || ' vs ').split(' vs ');
    return `<div class="bet-card" style="border-color:rgba(210,153,34,.35)">
      <div class="bet-card-top">
        <div class="bet-card-header">
          <span class="bet-match">${esc(h)} vs ${esc(a)}</span>
          <span class="pending-pill">⏳ Sync ausstehend</span>
        </div>
        <div class="bet-market-row">
          <span class="bet-market-chip">${esc(marketLabel(p.market, h, a))}</span>
        </div>
        <div style="font-size:11px;color:var(--muted);font-weight:600">Wird beim nächsten Consumer-Lauf ins Ledger geschrieben.</div>
      </div>
    </div>`;
  }).join('');
}

const _NORM_ALIASES = {
  'united states': 'usa', 'türkiye': 'turkey', 'republic of ireland': 'ireland',
  "côte d'ivoire": 'ivory coast', 'dr congo': 'democratic republic of congo',
};
function _normTeam(s) { const n = (s || '').toLowerCase().trim(); return _NORM_ALIASES[n] || n; }

function _buildLiveScoreLookup(data) {
  const out = {};
  for (const v of Object.values(data)) {
    const key = `${_normTeam(v.home)}_vs_${_normTeam(v.away)}`;
    // Keep the most recently updated entry if duplicates exist
    if (!out[key] || v.updated > out[key].updated) out[key] = v;
  }
  return out;
}

async function _fetchLiveScores() {
  try {
    const r = await fetch('data/live_scores.json?t=' + Date.now(), { cache: 'no-store' });
    if (r.ok) _liveScores = _buildLiveScoreLookup(await r.json());
  } catch (_) {}
}

function _setBetTab(tab) {
  _activeBetTab = tab;
  document.querySelectorAll('.bet-tab').forEach(el => {
    el.classList.toggle('active', el.getAttribute('onclick').includes(`'${tab}'`));
  });
  if (tab === 'live') {
    _fetchLiveScores().then(() => renderBets());
  } else {
    renderBets();
  }
}

function _betDateLabel(matchDate, forSettled) {
  if (!matchDate) return '—';
  const [y,mo,d] = matchDate.slice(0,10).split('-');
  const today = new Date(); today.setHours(0,0,0,0);
  const mDate = new Date(+y, +mo-1, +d);
  const diff = Math.round((mDate - today) / 86400000);
  if (forSettled) return diff === 0 ? 'Heute' : diff === -1 ? 'Gestern' : `${d}.${mo}.`;
  return diff === 0 ? 'Heute' : diff === 1 ? 'Morgen' : `${d}.${mo}.`;
}

function _renderOpenBetCards(bets, isLive) {
  let h = '';
  for (const b of bets) {
    const home = b.home || b.match?.split(' vs ')[0] || '';
    const away = b.away || b.match?.split(' vs ')[1] || '';
    const mktLabel = marketLabel(b.market, home, away);
    const conf = b.confidence || '';
    const confCls = conf === 'HIGH' ? 'conf-high' : conf === 'MEDIUM' ? 'conf-medium' : '';
    const stake = b.stake || 0;
    const odds = b.entry_odds || 0;
    const potProfit = (odds - 1) * stake;
    const totalReturn = odds * stake;
    const driftCls = b.drift_pct == null ? '' : b.drift_pct < 0 ? 'drift-good' : 'drift-bad';
    const driftArrow = b.drift_pct == null ? '' : b.drift_pct < 0 ? '↓' : '↑';
    const driftStr = b.drift_pct != null ? `${driftArrow}${Math.abs(b.drift_pct).toFixed(1)}%` : '—';
    const clvCls = b.clv_signal === 'good' ? 'clv-good' : b.clv_signal === 'bad' ? 'clv-bad' : 'clv-neutral';
    const clvLabel = b.clv_signal === 'good' ? '✓ CLV positiv' : b.clv_signal === 'bad' ? '✗ CLV negativ' : 'CLV ausstehend';
    const edgePct = b.model_edge_pct;
    const edgeStr = edgePct != null ? `${edgePct > 0 ? '+' : ''}${edgePct}% Edge` : '';
    const dateLabel = _betDateLabel(b.match_date, false);
    const flagH = teamFlag(home) || '';
    const flagA = teamFlag(away) || '';
    const currentOddsStr = b.current_odds ? b.current_odds.toFixed(2) : '—';

    // Live score lookup
    let liveScoreHtml = '';
    if (isLive) {
      const lsKey = `${_normTeam(home)}_vs_${_normTeam(away)}`;
      const ls = _liveScores[lsKey];
      if (ls && ls.home_score != null) {
        // Compute approximate match minute from kickoff in schedule
        const koEntry = _schedule.find(s => _normTeam(s.home) === _normTeam(home) && _normTeam(s.away) === _normTeam(away));
        let minStr = '';
        if (koEntry?.kickoff) {
          const elapsed = Math.floor((Date.now() - new Date(koEntry.kickoff).getTime()) / 60000);
          const clamp = Math.min(Math.max(elapsed, 0), 90);
          minStr = `${clamp}'`;
        }
        liveScoreHtml = `<div style="display:flex;align-items:center;gap:8px;padding:10px 14px 0;">
          <span class="score-box">${ls.home_score} : ${ls.away_score}</span>
          ${minStr ? `<span class="today-countdown">${minStr}</span>` : ''}
        </div>`;
      }
    }

    h += `<div class="bet-card">
      <div class="bet-card-top">
        <div class="bet-card-header">
          <span class="bet-match">${flagH} ${esc(home)} vs ${flagA} ${esc(away)}</span>
          <span class="bet-date">${dateLabel}</span>
        </div>
        <div class="bet-market-row">
          <span class="bet-market-chip">${esc(mktLabel)}</span>
          ${conf ? `<span class="bet-conf-chip ${confCls}">${conf}</span>` : ''}
          ${isLive ? `<span class="today-live-badge">LIVE</span>` : ''}
        </div>
        ${liveScoreHtml}
        <div class="bet-odds-row" style="${isLive ? 'padding-top:8px;' : ''}">
          <div>
            <div class="bet-odds-label">Einstieg</div>
            <span class="bet-odds-entry">@${odds.toFixed(2)}</span>
          </div>
          <span class="bet-odds-arrow">→</span>
          <div>
            <div class="bet-odds-label">Aktuell${infoTip('Markt-Bewegung seit deinem Einstieg. ↓ Quote gefallen (grün): der Markt glaubt jetzt eher an dein Outcome — du hast vorher zu guter Quote gegriffen. ↑ Quote gestiegen (rot): der Markt sieht es jetzt anders als du.')}</div>
            <span class="bet-odds-current ${driftCls}">${currentOddsStr} <small>${driftStr}</small></span>
          </div>
        </div>
      </div>
      <div class="bet-card-money">
        <div class="bet-money-cell">
          <div class="bet-money-label">Einsatz</div>
          <div class="bet-money-val">€${stake.toFixed(2)}</div>
          <div class="bet-money-sub">bei Verlust: −€${stake.toFixed(2)}</div>
        </div>
        <div class="bet-money-cell">
          <div class="bet-money-label">Möglicher Gewinn</div>
          <div class="bet-money-val bet-money-win">+€${potProfit.toFixed(2)}</div>
          <div class="bet-money-sub">Rückgabe: €${totalReturn.toFixed(2)}</div>
        </div>
      </div>
      <div class="bet-card-footer">
        ${edgeStr ? `<span class="bet-edge ${edgePct > 0 ? 'drift-good' : 'drift-bad'}">${esc(edgeStr)}</span>` : ''}
        <span class="clv-pill ${clvCls}">${clvLabel}${infoTip('CLV = Closing Line Value. Vergleicht deine Einstiegsquote mit der Endquote kurz vor Anpfiff (Markt-Konsens). ✓ positiv = du hast besser eingeschätzt als der Markt → langfristig der stärkste Profit-Indikator. ✗ negativ = der Markt war schlauer.')}</span>
      </div>
      <div class="bet-notes-row">
        <div class="bet-notes-label">📝 Notiz <span class="bet-notes-saved" id="notes-saved-${esc(_betNoteKey(home, away, b.market || ''))}">gespeichert</span></div>
        <textarea class="bet-notes-input" data-note-key="${esc(_betNoteKey(home, away, b.market || ''))}"
          placeholder="Warum diese Wette? Setze dein Reasoning, später nachvollziehen…"
          oninput="_onBetNoteInput(this)">${esc(_loadBetNote(home, away, b.market || ''))}</textarea>
      </div>
    </div>`;
  }
  return h;
}

function _renderSettledCards(bets) {
  let h = '';
  let lastDate = null;
  for (const b of bets) {
    const home = b.home || '';
    const away = b.away || '';
    const mktLabel = marketLabel(b.market, home, away);
    const flagH = teamFlag(home) || '';
    const flagA = teamFlag(away) || '';
    const dateStr = (b.match_date || '').slice(0,10);
    if (dateStr !== lastDate) {
      lastDate = dateStr;
      h += `<div class="bet-date-group">${_betDateLabel(dateStr, true)}</div>`;
    }
    const status = b.status || '';
    const resultCls = status === 'won' ? 'result-won' : status === 'lost' ? 'result-lost' : 'result-void';
    const resultLabel = status === 'won' ? '✅ Gewonnen' : status === 'lost' ? '❌ Verloren' : '↩️ Void';
    const pnl = b.pnl || 0;
    const pnlStr = status === 'void' || pnl === 0 ? '—' : (pnl > 0 ? `+€${pnl.toFixed(2)}` : `−€${Math.abs(pnl).toFixed(2)}`);
    const pnlCls = pnl > 0 ? 'drift-good' : pnl < 0 ? 'drift-bad' : '';
    // CLV-Pille (Closing Line Value): positiv = bessere Quote als Markt am Schluss → langfristig der stärkste Profit-Indikator.
    let clvPillHtml = '';
    if (b.clv != null) {
      const clvPct = b.clv * 100;
      const clvCls = clvPct > 0.5 ? 'clv-good' : clvPct < -0.5 ? 'clv-bad' : 'clv-neutral';
      const sign = clvPct >= 0 ? '+' : '';
      clvPillHtml = `<span class="clv-pill ${clvCls}" title="CLV: Einstiegsquote vs Closing-Quote (${(b.entry_odds||0).toFixed(2)} → ${(b.closing_odds||0).toFixed(2)})">CLV ${sign}${clvPct.toFixed(1)}%</span>`;
    }
    h += `<div class="bet-card">
      <div class="bet-card-top">
        <div class="bet-card-header">
          <span class="bet-match">${flagH} ${esc(home)} vs ${flagA} ${esc(away)}</span>
        </div>
        <div class="bet-market-row">
          <span class="bet-market-chip">${esc(mktLabel)}</span>
          <span class="bet-odds-label">@${(b.entry_odds||0).toFixed(2)} · €${(b.stake||0).toFixed(2)}</span>
        </div>
      </div>
      <div class="bet-result-row">
        <span class="bet-result-badge ${resultCls}">${resultLabel}</span>
        <span style="display:flex;align-items:center;gap:8px">${clvPillHtml}<span class="bet-pnl ${pnlCls}">${pnlStr}</span></span>
      </div>
    </div>`;
  }
  return h;
}

function renderBets() {
  const c = document.getElementById('bets-container');
  if (!c) return;

  const liveBets = _openBets.filter(b => b.is_live);
  const preBets  = _openBets.filter(b => !b.is_live);

  const tc = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n; };
  tc('tc-open',    preBets.length);
  tc('tc-live',    liveBets.length);
  tc('tc-settled', _settledBets.length);

  let h = '';
  if (_activeBetTab === 'live') {
    h = liveBets.length
      ? _renderOpenBetCards(liveBets, true)
      : `<div class="empty"><div class="icon">📡</div><div>Keine Live-Wetten gerade.</div></div>`;
  } else if (_activeBetTab === 'settled') {
    h = _settledBets.length
      ? _renderSettledCards(_settledBets)
      : `<div class="empty"><div class="icon">📭</div><div>Noch keine abgerechneten Wetten.</div></div>`;
  } else {
    h = _pwaPendingHtml();
    h += preBets.length
      ? _renderOpenBetCards(preBets, false)
      : `<div class="empty"><div class="icon">📭</div><div>Keine offenen Wetten.<br><small>Signals werden täglich 08:00 UTC aktualisiert.</small></div></div>`;
  }
  c.innerHTML = h;
}

// ── Render Bankroll strip ─────────────────────────────────────
function renderBankrollStrip() {
  const b = _bankrollState;
  if (!b || b.free == null) return;
  const free = b.free ?? 0;
  const pnl = (b.pnl_closed ?? 0);
  const maxWin = b.max_win ?? 0;
  const staked = b.staked ?? 0;
  const exp = b.exposure_pct ?? 0;
  const openCount = (_openBets || []).length;

  document.getElementById('br-free').textContent = '€' + free.toFixed(2);
  const pnlEl = document.getElementById('br-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '€';
  pnlEl.style.color = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : 'var(--text)';
  document.getElementById('br-maxwin').textContent = '+€' + maxWin.toFixed(2);
  document.getElementById('br-staked').textContent = staked.toFixed(2);
  document.getElementById('br-exp').textContent = exp.toFixed(1);
  document.getElementById('br-count').textContent = openCount + ' offene Wette' + (openCount !== 1 ? 'n' : '');

  const bar = document.getElementById('br-bar');
  bar.style.width = Math.min(exp, 100) + '%';
  bar.className = 'exposure-fill ' + (exp < 30 ? 'exposure-low' : exp < 60 ? 'exposure-mid' : 'exposure-high');

  const tipEl = document.getElementById('br-exp-tip');
  if (tipEl) {
    const stateTxt = exp < 30 ? 'grün — sicher, weitere Wetten ok.'
                   : exp < 60 ? 'gelb — moderat, neue Wetten vorsichtig.'
                   : 'rot — hoch, Pause empfohlen.';
    tipEl.innerHTML = infoTip(`Anteil der Bankroll, der gerade in offenen Wetten gebunden ist. <30% = grün, 30–60% = gelb, >60% = rot. Aktuell ${exp.toFixed(1)}% → ${stateTxt}`);
  }
}

// ── Bet-Modal & Worker-Submit ─────────────────────────────────
const WORKER_BASE = CLOUD_URL.replace(/\/signals\.json$/, '');
const BANKROLL_START = 100; // 5%-Regel-Referenz; ggf. später aus _bankrollState.start ableiten
function _applyUserBankroll(amount) {
  if (!amount || amount < 10) return;
  // D4/D5: localStorage-Wert hat Vorrang, wenn Backend nur den €100-Default
  // zeigt UND der User noch keine Aktivität hat. Sobald PnL oder offene
  // Stakes auftauchen, gewinnt das Backend (echte Bankroll-Historie).
  const bs = _bankrollState || {};
  const hasActivity = (bs.pnl_closed && bs.pnl_closed !== 0) ||
                      (bs.staked && bs.staked > 0);
  if (hasActivity) return;
  _bankrollState = { start: amount, free: amount, pnl_closed: 0, staked: 0, exposure_pct: 0, max_win: 0 };
  renderBankrollStrip();
}
let _pendingBet = null;
let _pwaPendingIds = JSON.parse(localStorage.getItem('sb_pwa_pending') || '[]');

function _savePwaPending(ids) {
  _pwaPendingIds = ids;
  localStorage.setItem('sb_pwa_pending', JSON.stringify(ids));
}

// ── Quick-Stake-Buttons (Tier 4.4, Bet365-Style) ──────────────
const _KELLY_FRAC = 0.25;  // fractional Kelly, identisch zu Backend
function _calcKellyStake() {
  if (!_pendingBet) return null;
  const p = _pendingBet.model_prob;
  const o = _pendingBet.odds;
  if (!p || p <= 0 || p >= 1 || !o || o <= 1) return null;
  // f* = (p*o - 1) / (o - 1)
  const fFull = (p * o - 1) / (o - 1);
  if (fFull <= 0) return null;
  const bk = (_bankrollState && _bankrollState.start) ? _bankrollState.start : BANKROLL_START;
  const stake = bk * fFull * _KELLY_FRAC;
  // 0.50€-Granularität, min 0.50€, max 25€ (UI-Limit)
  const rounded = Math.max(0.5, Math.min(25, Math.round(stake * 2) / 2));
  return rounded;
}
function _setStake(v) {
  const inp = document.getElementById('bet-modal-stake');
  if (!inp) return;
  inp.value = v.toFixed(2);
  _updateBetModalCalcs();
}
function _renderQuickStakes() {
  const wrap = document.getElementById('bet-modal-quick-stakes');
  if (!wrap) return;
  const kelly = _calcKellyStake();
  const half  = kelly != null ? Math.max(0.5, Math.round(kelly / 2 * 2) / 2) : null;
  const bk = (_bankrollState && _bankrollState.start) ? _bankrollState.start : BANKROLL_START;
  const onepct = Math.max(0.5, Math.round(bk * 0.01 * 2) / 2);
  const buttons = [
    { lbl: 'Kelly', val: kelly, cls: 'kelly' },
    { lbl: 'Half-K', val: half, cls: 'kelly' },
    { lbl: '€5', val: 5, cls: '' },
    { lbl: '€10', val: 10, cls: '' },
    { lbl: '1% BK', val: onepct, cls: '' },
  ];
  wrap.innerHTML = buttons.map(b => {
    if (b.val == null) {
      return `<button type="button" class="quick-stake-btn ${b.cls}" disabled aria-label="${b.lbl} (nicht verfügbar)">
        <span class="qs-lbl">${b.lbl}</span><span>—</span>
      </button>`;
    }
    return `<button type="button" class="quick-stake-btn ${b.cls}" onclick="_setStake(${b.val})" aria-label="${b.lbl} €${b.val.toFixed(2)}">
      <span class="qs-lbl">${b.lbl}</span><span>€${b.val.toFixed(b.val < 10 ? 2 : 0)}</span>
    </button>`;
  }).join('');
}

function _openBetModalFromBtn(btn) {
  const d = btn.dataset;
  const source = (d.source === 'manual') ? 'manual' : 'value';
  const modelProbRaw = parseFloat(d.modelProb || '0');
  const modelProb = (modelProbRaw > 0 && modelProbRaw <= 1) ? modelProbRaw
                  : (modelProbRaw > 1 && modelProbRaw <= 100) ? modelProbRaw / 100
                  : 0;
  _pendingBet = {
    match: d.match,
    market: d.market,
    odds: parseFloat(d.odds),
    stake_eur: parseFloat(d.stake) || (source === 'manual' ? 5 : 5),
    ev_pct: parseFloat(d.ev || '0'),
    confidence: d.confidence || '',
    kickoff: d.kickoff || '',
    sport: d.sport || '',
    source,
    model_prob: modelProb,
    fair_prob: parseFloat(d.fairProb || '0'),
  };
  const [h, a] = _pendingBet.match.split(' vs ').map(x => x.trim());
  document.getElementById('bet-modal-sub').textContent = `${h} vs ${a} · ${marketLabel(_pendingBet.market, h, a)}`;
  const oddsInp = document.getElementById('bet-modal-odds-input');
  if (oddsInp) oddsInp.value = _pendingBet.odds.toFixed(2);
  const badge = document.getElementById('bet-modal-kind-badge');
  if (badge) {
    if (source === 'manual') {
      badge.textContent = '✍️ Manuell';
      badge.style.background = 'rgba(210,153,34,.18)';
      badge.style.color = 'var(--yellow)';
      badge.style.border = '1px solid rgba(210,153,34,.4)';
      badge.style.display = 'inline-block';
    } else {
      badge.textContent = '💡 Value-Bet';
      badge.style.background = 'rgba(0,200,83,.15)';
      badge.style.color = 'var(--green)';
      badge.style.border = '1px solid rgba(0,200,83,.4)';
      badge.style.display = 'inline-block';
    }
  }
  const tierBadge = document.getElementById('bet-modal-tier-badge');
  if (tierBadge) {
    const t = (_pendingBet.confidence || '').toUpperCase();
    if (t === 'HIGH' || t === 'MEDIUM' || t === 'LOW') {
      tierBadge.className = 'conf-badge conf-' + t;
      tierBadge.textContent = t;
      tierBadge.style.display = 'inline-block';
    } else {
      tierBadge.style.display = 'none';
    }
  }
  const inp = document.getElementById('bet-modal-stake');
  inp.value = _pendingBet.stake_eur.toFixed(2);
  _renderQuickStakes();
  _updateBetModalCalcs();
  document.getElementById('bet-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
  // kein autofocus auf Mobile, hebt sonst die Tastatur an
}

function _closeBetModal() {
  document.getElementById('bet-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
  _pendingBet = null;
}

function _updateBetModalCalcs() {
  if (!_pendingBet) return;
  const stake = parseFloat(document.getElementById('bet-modal-stake').value) || 0;
  const oddsInp = document.getElementById('bet-modal-odds-input');
  const odds = oddsInp ? (parseFloat(oddsInp.value) || 0) : _pendingBet.odds;
  _pendingBet.odds = odds;
  const profit = (odds - 1) * stake;
  const ret = odds * stake;
  const free = _bankrollState?.free ?? BANKROLL_START;
  const start = _bankrollState?.start ?? BANKROLL_START;
  const riskPct = start > 0 ? (stake / start * 100) : 0;
  document.getElementById('bet-modal-profit').textContent = '+€' + profit.toFixed(2);
  document.getElementById('bet-modal-return').textContent = '€' + ret.toFixed(2);
  document.getElementById('bet-modal-risk').textContent = riskPct.toFixed(1) + '% (€' + free.toFixed(0) + ' frei)';
  // Live EV: use model_prob if available, else show the static ev_pct (or hide if neither)
  const evRow = document.getElementById('bet-modal-ev-row');
  const evEl = document.getElementById('bet-modal-ev');
  let evPct = null;
  if (_pendingBet.model_prob && _pendingBet.model_prob > 0 && odds > 0) {
    evPct = calcEV(_pendingBet.model_prob, odds);
  } else if (Number.isFinite(_pendingBet.ev_pct) && _pendingBet.ev_pct !== 0) {
    evPct = _pendingBet.ev_pct;
  }
  if (evPct == null) {
    if (evRow) evRow.style.display = 'none';
  } else {
    if (evRow) evRow.style.display = '';
    if (evEl) {
      const sign = evPct >= 0 ? '+' : '';
      evEl.textContent = sign + evPct.toFixed(1) + '%';
      evEl.style.color = evPct >= 0 ? 'var(--green)' : 'var(--red)';
    }
  }
  // C1: Why-this-bet Drawer — Modell% / Markt-fair% / Edge pp
  const drawer = document.getElementById('bet-modal-why');
  if (drawer) {
    const mpct = (_pendingBet.model_prob > 0) ? _pendingBet.model_prob * 100 : null;
    let fpct = (_pendingBet.fair_prob > 0) ? _pendingBet.fair_prob : null;
    if (fpct == null && odds > 1) fpct = 100 / odds; // Fallback: raw implied
    if (mpct !== null && fpct !== null) {
      const edge = mpct - fpct;
      document.getElementById('bet-modal-model-pct').textContent = mpct.toFixed(1) + '%';
      const fairEl = document.getElementById('bet-modal-fair-pct');
      fairEl.textContent = fpct.toFixed(1) + '%' + (_pendingBet.fair_prob > 0 ? '' : ' *');
      fairEl.title = _pendingBet.fair_prob > 0 ? 'Shin-fair aus Markt-Konsens' : '* aus 1/Quote (kein Shin-Konsens verfügbar)';
      const edgeEl = document.getElementById('bet-modal-edge-pp');
      const sign = edge >= 0 ? '+' : '';
      edgeEl.textContent = sign + edge.toFixed(1) + 'pp';
      edgeEl.style.color = edge >= 0 ? 'var(--green)' : 'var(--red)';
      drawer.style.display = '';
    } else {
      drawer.style.display = 'none';
    }
  }
  const warn = document.getElementById('bet-modal-warn');
  warn.classList.toggle('show', stake > start * 0.05 + 0.001);
  const btn = document.getElementById('bet-modal-confirm');
  const oddsOk = odds >= 1.01 && odds <= 100;
  btn.disabled = !(stake >= 0.5 && stake <= 25 && oddsOk);
}

async function _submitBet() {
  if (!_pendingBet) return;
  const stake = parseFloat(document.getElementById('bet-modal-stake').value) || 0;
  if (stake < 0.5 || stake > 25) { showToast('Einsatz muss zwischen €0.50 und €25 liegen', 'error'); return; }
  const oddsInp = document.getElementById('bet-modal-odds-input');
  const odds = oddsInp ? (parseFloat(oddsInp.value) || 0) : _pendingBet.odds;
  if (!(odds >= 1.01 && odds <= 100)) { showToast('Quote muss zwischen 1.01 und 100 liegen', 'error'); return; }
  const token = localStorage.getItem('sb_token');
  if (!token) { _openTokenModal(); return; }

  const btn = document.getElementById('bet-modal-confirm');
  btn.disabled = true; btn.textContent = 'Eintragen…';
  const hasModelProb = _pendingBet.model_prob && _pendingBet.model_prob > 0;
  const payload = {
    ..._pendingBet,
    odds,
    stake_eur: stake,
    // Ohne model_prob kann der Worker keine EV-Prüfung machen → 'manual'
    source: hasModelProb ? (_pendingBet.source || 'value') : 'manual',
  };
  if (hasModelProb) {
    payload.model_prob = _pendingBet.model_prob;
    payload.ev_pct = calcEV(_pendingBet.model_prob, odds);
  }
  try {
    const r = await fetch(WORKER_BASE + '/pending_bets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify(payload),
    });
    if (r.status === 401) {
      localStorage.removeItem('sb_token');
      _closeBetModal();
      showToast('Token abgelehnt — bitte erneut eingeben', 'error');
      _openTokenModal();
      return;
    }
    if (!r.ok) {
      let msg = 'HTTP ' + r.status;
      try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
      showToast('Fehler: ' + msg, 'error');
      return;
    }
    const j = await r.json();
    if (j.id) {
      const ids = [..._pwaPendingIds, { id: j.id, match: payload.match, market: payload.market, ts: Date.now() }];
      _savePwaPending(ids);
    }
    _closeBetModal();
    showToast('✅ Wette eingetragen — wird beim nächsten Sync ins Ledger geschrieben', 'success');
    renderBets();
  } catch (e) {
    showToast('Netzwerk-Fehler: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Wette eintragen';
  }
}

// ── Web Push: Subscribe-Flow + Settings-Modal ────────────────
function _urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const out = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) out[i] = rawData.charCodeAt(i);
  return out;
}

async function _swRegistration() {
  if (!('serviceWorker' in navigator)) return null;
  try {
    // sw.js liegt unter /sportsbrain/sw.js (GitHub Pages)
    const reg = await navigator.serviceWorker.register('sw.js');
    await navigator.serviceWorker.ready;
    return reg;
  } catch (e) {
    console.warn('SW register failed:', e);
    return null;
  }
}

async function _currentPushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return null;
  try { return await reg.pushManager.getSubscription(); } catch { return null; }
}

function _pushSupported() {
  return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
}

async function _togglePush() {
  const btn = document.getElementById('settings-push-toggle');
  if (!_pushSupported()) {
    showToast('⚠️ Push wird von diesem Browser nicht unterstützt.', 'error');
    return;
  }
  if (!VAPID_PUBLIC_KEY || VAPID_PUBLIC_KEY.startsWith('REPLACE_')) {
    showToast('⚠️ VAPID-Key noch nicht konfiguriert (siehe scripts/gen_vapid_keys.py).', 'error');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const existing = await _currentPushSubscription();
    if (existing) {
      // Abmelden
      const endpoint = existing.endpoint;
      try { await existing.unsubscribe(); } catch {}
      try {
        await fetch(WORKER_BASE + '/push/unsubscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint }),
        });
      } catch {}
      showToast('🔕 Push deaktiviert.', '');
    } else {
      // Permission anfragen
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        showToast('⚠️ Notification-Permission verweigert.', 'error');
        return;
      }
      const reg = await _swRegistration();
      if (!reg) throw new Error('Service Worker konnte nicht registriert werden.');
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
      });
      const r = await fetch(WORKER_BASE + '/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON ? sub.toJSON() : sub),
      });
      if (!r.ok) throw new Error('Subscribe-POST schlug fehl: HTTP ' + r.status);
      showToast('🔔 Push aktiviert.', 'success');
    }
  } catch (e) {
    showToast('⚠️ ' + (e.message || e), 'error');
  } finally {
    if (btn) btn.disabled = false;
    _renderSettingsState();
  }
}

async function _renderSettingsState() {
  // Push-Status
  const btn = document.getElementById('settings-push-toggle');
  const sub = document.getElementById('settings-push-status');
  const iosHint = document.getElementById('settings-ios-hint');
  if (btn && sub) {
    if (!_pushSupported()) {
      btn.textContent = 'N/A';
      btn.className = 'settings-toggle warn';
      sub.textContent = 'Dieser Browser unterstützt Web Push nicht.';
    } else if (!VAPID_PUBLIC_KEY || VAPID_PUBLIC_KEY.startsWith('REPLACE_')) {
      btn.textContent = 'Setup';
      btn.className = 'settings-toggle warn';
      sub.textContent = 'VAPID-Schlüssel noch nicht konfiguriert.';
    } else {
      const existing = await _currentPushSubscription();
      if (existing) {
        btn.textContent = 'AN';
        btn.className = 'settings-toggle on';
        sub.textContent = 'Notifications für neue Value-Bets + Settlement aktiv.';
      } else {
        btn.textContent = 'AUS';
        btn.className = 'settings-toggle off';
        sub.textContent = 'Tippe um Notifications zu aktivieren.';
      }
    }
  }
  // iOS-Hinweis nur anzeigen wenn iOS Safari ohne PWA-Standalone-Modus
  if (iosHint) {
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
    const isStandalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
      || window.navigator.standalone === true;
    iosHint.style.display = (isIOS && !isStandalone) ? '' : 'none';
  }
  // Compact-Mode-Status
  const cbtn = document.getElementById('settings-compact-toggle');
  if (cbtn) {
    cbtn.textContent = _compactMode ? 'AN' : 'AUS';
    cbtn.className = 'settings-toggle ' + (_compactMode ? 'on' : 'off');
  }
  // Token-Status
  const ts = document.getElementById('settings-token-status');
  if (ts) {
    try {
      const t = localStorage.getItem('sb_token');
      ts.textContent = t ? 'Gesetzt · ••••' + t.slice(-4) : 'Nicht gesetzt';
    } catch { ts.textContent = '—'; }
  }
  // D3 — User-Slot
  const us = document.getElementById('settings-user-status');
  if (us) {
    const u = _getUserSlot();
    const def = (_meta && _meta.default_user) || 'philip';
    us.textContent = (u === def) ? `${u} (Default)` : u;
  }
}

// D3 — Multi-User-Schema
function _getUserSlot() {
  try { return (localStorage.getItem('sb_user') || 'philip').trim() || 'philip'; }
  catch { return 'philip'; }
}
function _setUserSlot(name) {
  const clean = String(name || '').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '');
  if (!clean) return false;
  try { localStorage.setItem('sb_user', clean); } catch { return false; }
  return true;
}
function _promptUserSlot() {
  const current = _getUserSlot();
  const v = prompt(
    'User-Slot ändern\n\n' +
    'Aktuell: ' + current + '\n\n' +
    'Nur Buchstaben/Ziffern/-/_. Backend nutzt aktuell nur den Default-User; ' +
    'andere Slots sind vorbereitet, bekommen aber noch keine eigenen Daten.',
    current
  );
  if (v === null) return;
  if (_setUserSlot(v)) {
    showToast('User-Slot: ' + _getUserSlot(), 'ok');
    _renderSettingsState();
  } else {
    showToast('Ungültiger Slot-Name', 'error');
  }
}

// D2 — Token rotieren
async function _rotateToken() {
  const token = (() => { try { return localStorage.getItem('sb_token') || ''; } catch { return ''; } })();
  if (!token) {
    showToast('Kein Worker-Token gesetzt — erst eintragen, dann rotieren.', 'error');
    return;
  }
  const user = _getUserSlot();
  if (!confirm(
    'Token rotieren für User „' + user + '"?\n\n' +
    'Es wird ein neuer Token erzeugt und automatisch in dieser PWA gespeichert.\n' +
    'Der alte Token bleibt 24h gültig (Grace-Period), damit andere Geräte umgestellt werden können.'
  )) return;
  try {
    const r = await fetch(WORKER_BASE + '/rotate_token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify({ user }),
    });
    if (r.status === 401) {
      showToast('Aktueller Token ungültig — bitte manuell setzen.', 'error');
      return;
    }
    if (!r.ok) {
      showToast('Rotate fehlgeschlagen (HTTP ' + r.status + ')', 'error');
      return;
    }
    const data = await r.json();
    if (!data.ok || !data.token) {
      showToast('Rotate fehlgeschlagen (kein Token in Antwort)', 'error');
      return;
    }
    try { localStorage.setItem('sb_token', data.token); } catch {}
    const exp = data.previous_expires_at ? new Date(data.previous_expires_at).toLocaleString() : '—';
    showToast('Token rotiert (alter gültig bis ' + exp + ')', 'success');
    _renderSettingsState();
  } catch (e) {
    showToast('Netzwerk-Fehler: ' + (e && e.message || e), 'error');
  }
}

// D6: Master-Token erzeugt einen Invite-Link. Empfänger wählt Username + Bankroll
// im Onboarding-Flow selbst (Username-Step erscheint nur mit sb_invite_pending).
async function _createInvite() {
  const token = (() => { try { return localStorage.getItem('sb_token') || ''; } catch { return ''; } })();
  if (!token) {
    showToast('Kein Worker-Token gesetzt — Master-Token nötig.', 'error');
    return;
  }
  try {
    const r = await fetch(WORKER_BASE + '/invite', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify({}),
    });
    if (r.status === 401) {
      showToast('Nur Master-Token kann Invites erstellen.', 'error');
      return;
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok || !j.invite_token) {
      showToast('Invite fehlgeschlagen (HTTP ' + r.status + ')', 'error');
      return;
    }
    const link = window.location.origin + window.location.pathname + '?invite=' + j.invite_token;
    try {
      await navigator.clipboard.writeText(link);
      showToast('Invite-Link in Zwischenablage kopiert.', 'success');
    } catch {}
    // Show in a modal-like prompt as fallback
    window.prompt('Invite-Link (einmal nutzbar, läuft nach erster Registrierung ab):', link);
  } catch (e) {
    showToast('Netzwerk-Fehler: ' + (e && e.message || e), 'error');
  }
}

function _toggleCompactFromSettings() {
  toggleCompact();
  _renderSettingsState();
}

function _openSettings() {
  document.getElementById('settings-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
  _renderSettingsState();
}
function _closeSettings() {
  document.getElementById('settings-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
}

function _openTokenModal() {
  document.getElementById('token-modal-input').value = '';
  document.getElementById('token-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
}
function _closeTokenModal() {
  document.getElementById('token-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
}

// Listener verdrahten
(function () {
  document.getElementById('bet-modal-stake')?.addEventListener('input', _updateBetModalCalcs);
  document.getElementById('bet-modal-odds-input')?.addEventListener('input', _updateBetModalCalcs);
  document.getElementById('bet-modal-cancel')?.addEventListener('click', _closeBetModal);
  document.getElementById('bet-modal-confirm')?.addEventListener('click', _submitBet);
  document.getElementById('bet-modal-bd')?.addEventListener('click', (e) => { if (e.target.id === 'bet-modal-bd') _closeBetModal(); });
  document.getElementById('token-modal-cancel')?.addEventListener('click', _closeTokenModal);
  document.getElementById('token-modal-save')?.addEventListener('click', () => {
    const v = document.getElementById('token-modal-input').value.trim();
    if (!v) { showToast('Token darf nicht leer sein', 'error'); return; }
    localStorage.setItem('sb_token', v);
    _closeTokenModal();
    if (_pendingBet) _submitBet();
  });
  document.getElementById('token-modal-bd')?.addEventListener('click', (e) => { if (e.target.id === 'token-modal-bd') _closeTokenModal(); });
  document.getElementById('settings-modal-bd')?.addEventListener('click', (e) => { if (e.target.id === 'settings-modal-bd') _closeSettings(); });
})();

// ── Home-Suche ────────────────────────────────────────────────
(function () {
  const input = document.getElementById('home-search-input');
  const wrap  = document.getElementById('home-search-wrap');
  const clear = document.getElementById('home-search-clear');
  if (!input || !wrap || !clear) return;
  let t = null;
  input.addEventListener('input', () => {
    _homeSearch = input.value;
    wrap.classList.toggle('has-text', !!input.value);
    clearTimeout(t);
    t = setTimeout(() => { if (document.getElementById('view-home').classList.contains('active') || true) renderHome(); }, 80);
  });
  clear.addEventListener('click', () => {
    input.value = ''; _homeSearch = '';
    wrap.classList.remove('has-text');
    renderHome();
    input.focus();
  });
})();

// ── Hide bottom-nav on scroll down, show on scroll up ─────────
(function () {
  const nav = document.querySelector('.bottom-nav');
  if (!nav) return;
  let lastY = window.scrollY, dir = 0, hidden = false;
  window.addEventListener('scroll', () => {
    const y = window.scrollY;
    const d = y - lastY;
    lastY = y;

    // Ignore micro-scroll noise from iOS momentum/bounce — prevents flicker
    if (Math.abs(d) < 3) return;

    // Always show at top or page bottom
    const atBottom = (y + window.innerHeight) >= (document.documentElement.scrollHeight - 50);
    if (y < 80 || atBottom) {
      if (hidden) { nav.classList.remove('nav-hidden'); hidden = false; dir = 0; }
      return;
    }

    if ((d > 0 && dir < 0) || (d < 0 && dir > 0)) dir = 0;
    dir += d;

    if (dir > 80 && !hidden) {
      nav.classList.add('nav-hidden'); hidden = true; dir = 0;
    } else if (dir < -40 && hidden) {
      nav.classList.remove('nav-hidden'); hidden = false; dir = 0;
    }
  }, { passive: true });
})();

// ── Pull-to-Refresh ───────────────────────────────────────────
(function () {
  let startY = 0, pulling = false;
  document.addEventListener('touchstart', e => {
    if (window.scrollY === 0) startY = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener('touchmove', e => {
    if (!startY) return;
    if (e.touches[0].clientY - startY > 70 && window.scrollY === 0) pulling = true;
  }, { passive: true });
  document.addEventListener('touchend', () => {
    if (pulling) load();
    startY = 0;
    pulling = false;
  });
})();

// ── Escape-Taste: Modals + Detail-View schließen ──────────────
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('bet-modal-bd')?.classList.contains('show'))   { _closeBetModal();   return; }
  if (document.getElementById('token-modal-bd')?.classList.contains('show')) { _closeTokenModal(); return; }
  if (document.getElementById('settings-modal-bd')?.classList.contains('show')) { _closeSettings(); return; }
  if (document.getElementById('view-detail')?.classList.contains('active'))  { closeDetail(); }
});

// URL-Token-Setter: ?token=XXX → speichert in localStorage.
// Param wird NICHT aus URL entfernt, damit "Zum Home-Bildschirm" die Magic-URL
// behält und die PWA beim ersten Launch den Token auch in ihrer eigenen
// localStorage-Sandbox speichern kann (iOS-PWA hat separates Storage).
(function () {
  try {
    const params = new URLSearchParams(window.location.search);
    const t = params.get('token');
    if (t && t.length >= 32) {
      localStorage.setItem('sb_token', t);
      setTimeout(() => showToast && showToast('🔑 Worker-Token gespeichert', 'success'), 200);
    }
  } catch {}
})();

// Pre-register Service Worker im Hintergrund (für Push), nicht-blockierend
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(err => console.warn('SW register failed:', err));
}

load();
_onbMaybeShow();
