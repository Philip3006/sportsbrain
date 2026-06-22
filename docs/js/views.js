function predCard(home, away, tip, odds, nk, kickoff, sport) {
  odds = odds || {};
  const matchStr = `${home} vs ${away}`;
  const koStr = kickoff || '';
  const spStr = sport || '';
  const ph = Math.round(tip.p_home * 100);
  const pd = Math.round(tip.p_draw * 100);
  const pa = Math.round(tip.p_away * 100);
  // Determine top outcome
  let tipLabel;
  if (tip.p_home >= tip.p_away && tip.p_home >= tip.p_draw) {
    tipLabel = `🏆 ${esc(home)} gewinnt`;
  } else if (tip.p_away >= tip.p_home && tip.p_away >= tip.p_draw) {
    tipLabel = `🏆 ${esc(away)} gewinnt`;
  } else {
    tipLabel = '🤝 Unentschieden';
  }

  // Build Marktübersicht — enriched with all available odds
  const mkts = [];
  if (tip.p_home && odds.home > 1) mkts.push({key: 'home', name: esc(home) + ' gewinnt', p: tip.p_home, q: odds.home});
  if (tip.p_draw && odds.draw > 1) mkts.push({key: 'draw', name: 'Unentschieden', p: tip.p_draw, q: odds.draw});
  if (tip.p_away && odds.away > 1) mkts.push({key: 'away', name: esc(away) + ' gewinnt', p: tip.p_away, q: odds.away});
  // DC markets
  if (tip.p_home && tip.p_draw && odds.dc_1x > 1) mkts.push({key: 'dc_1x', name: `DC: ${esc(home)} oder Remis`, p: tip.p_home + tip.p_draw, q: odds.dc_1x});
  if (tip.p_draw && tip.p_away && odds.dc_x2 > 1) mkts.push({key: 'dc_x2', name: `DC: Remis oder ${esc(away)}`, p: tip.p_draw + tip.p_away, q: odds.dc_x2});
  if (tip.p_home && tip.p_away && odds.dc_12 > 1) mkts.push({key: 'dc_12', name: `DC: ${esc(home)} oder ${esc(away)}`, p: tip.p_home + tip.p_away, q: odds.dc_12});
  if (tip.p_btts_yes && odds.btts_yes > 1) mkts.push({key: 'btts_yes', name: 'Beide treffen', p: tip.p_btts_yes, q: odds.btts_yes});
  if (tip.p_btts_no && odds.btts_no > 1) mkts.push({key: 'btts_no', name: 'Kein BTTS', p: tip.p_btts_no, q: odds.btts_no});


  // Direct bet buttons next to 1/X/2 probability bars (same data as Marktübersicht)
  const barBetBtn = (mktKey, modelP, q) => {
    if (!q || q <= 1) return '';
    const ev = calcEV(modelP, q);
    const isValue = ev >= 3;
    const src = isValue ? 'value' : 'manual';
    const stake = isValue ? 10 : 5;
    const style = isValue
      ? 'background:rgba(0,200,83,.18);color:var(--green);border:1px solid rgba(0,200,83,.55)'
      : 'background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--border)';
    const attrs = [
      `type="button"`,
      `data-match="${esc(matchStr)}"`,
      `data-market="${mktKey}"`,
      `data-odds="${q}"`,
      `data-stake="${stake}"`,
      `data-ev="${ev.toFixed(2)}"`,
      `data-confidence=""`,
      `data-kickoff="${esc(koStr)}"`,
      `data-sport="${esc(spStr)}"`,
      `data-model-prob="${modelP}"`,
      `data-source="${src}"`,
      `onclick="event.stopPropagation();_openBetModalFromBtn(this)"`,
    ].join(' ');
    return `<button ${attrs} aria-label="Wette ${mktKey} @ ${q.toFixed(2)}"
      style="${style};font-size:13px;font-weight:800;padding:6px 12px;border-radius:8px;cursor:pointer;white-space:nowrap;min-width:62px;height:38px;-webkit-appearance:none;appearance:none">${q.toFixed(2)}</button>`;
  };
  const homeBtn = barBetBtn('home', tip.p_home, odds.home);
  const drawBtn = barBetBtn('draw', tip.p_draw, odds.draw);
  const awayBtn = barBetBtn('away', tip.p_away, odds.away);

  // Compact "Weitere Märkte" buttons: DC, BTTS — direkt über xG.
  // Ersetzt die separate Marktübersicht-Tabelle.
  const extraMkts = mkts.filter(m => !['home','draw','away'].includes(m.key));
  const extraBtn = (m) => {
    const ev = (m.p * m.q - 1) * 100;
    const isValue = ev >= 3;
    const src = isValue ? 'value' : 'manual';
    const stake = isValue ? 10 : 5;
    const cardStyle = isValue
      ? 'background:rgba(0,200,83,.10);border:1px solid rgba(0,200,83,.45)'
      : 'background:rgba(255,255,255,.04);border:1px solid var(--border)';
    const oddsColor = isValue ? 'var(--green)' : 'var(--text)';
    const evColor = ev >= 0 ? 'var(--green)' : 'var(--red)';
    const attrs = [
      `type="button"`,
      `data-match="${esc(matchStr)}"`,
      `data-market="${m.key}"`,
      `data-odds="${m.q}"`,
      `data-stake="${stake}"`,
      `data-ev="${ev.toFixed(2)}"`,
      `data-confidence=""`,
      `data-kickoff="${esc(koStr)}"`,
      `data-sport="${esc(spStr)}"`,
      `data-model-prob="${m.p}"`,
      `data-source="${src}"`,
      `onclick="event.stopPropagation();_openBetModalFromBtn(this)"`,
    ].join(' ');
    return `<button ${attrs} aria-label="Wette ${m.name} @ ${m.q.toFixed(2)}"
      style="${cardStyle};border-radius:10px;padding:9px 10px;display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer;font-family:inherit;color:var(--text);-webkit-appearance:none;appearance:none;min-width:0">
      <span style="font-size:10px;font-weight:700;color:var(--muted);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">${m.name}</span>
      <span style="font-size:16px;font-weight:900;color:${oddsColor}">${m.q.toFixed(2)}</span>
      <span style="font-size:10px;font-weight:800;color:${evColor}">${Math.round(m.p*100)}% · ${ev>=0?'+':''}${ev.toFixed(1)}%</span>
    </button>`;
  };
  const extraGrid = extraMkts.length ? `
    <div style="margin-top:10px;margin-bottom:10px">
      <div style="font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">Weitere Märkte</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:7px">
        ${extraMkts.map(extraBtn).join('')}
      </div>
    </div>` : '';

  // ── Storytelling (Tier 4.3) ─────────────────────────────
  const _story = (() => {
    const xgSum = (tip.xg_home || 0) + (tip.xg_away || 0);
    const probMax = Math.max(tip.p_home, tip.p_draw, tip.p_away);
    const probGap = probMax - Math.min(tip.p_home, tip.p_away);
    const parts = [];
    // Tor-Erwartung
    if (xgSum >= 2.8) {
      parts.push(`Hohe Tor-Erwartung (<b>${xgSum.toFixed(1)} xG</b> kombiniert) — Über 2.5 in Reichweite.`);
    } else if (xgSum < 2.0) {
      parts.push(`Defensiv-Match (<b>${xgSum.toFixed(1)} xG</b> kombiniert) — Unter 2.5 wahrscheinlich.`);
    } else {
      parts.push(`Mittlere Tor-Erwartung (<b>${xgSum.toFixed(1)} xG</b>).`);
    }
    // Favoriten-Gap
    let storyCls = '';
    if (probMax >= 0.55) {
      const favTeam = tip.p_home >= tip.p_away && tip.p_home >= tip.p_draw ? home : (tip.p_away >= tip.p_draw ? away : null);
      if (favTeam) {
        parts.push(`<b>${esc(favTeam)}</b> klarer Favorit mit <b>${Math.round(probMax*100)}%</b>.`);
      } else {
        parts.push(`Remis-Tendenz (<b>${Math.round(tip.p_draw*100)}%</b>).`);
        storyCls = 'draw';
      }
    } else if (probGap < 0.10) {
      parts.push(`Offenes Duell — 1/X/2 nahezu gleich verteilt.`);
      storyCls = 'draw';
    } else {
      const favTeam = tip.p_home >= tip.p_away ? home : away;
      parts.push(`Leichter Vorteil <b>${esc(favTeam)}</b> (<b>${Math.round(probMax*100)}%</b>).`);
    }
    return `<div class="pred-story ${storyCls}">${parts.join(' ')}</div>`;
  })();

  return `<div class="pred-card">
    <div class="pred-title">⚡ Modell-Tipp</div>
    <div class="pred-tip">${tipLabel}</div>
    ${_story}
    <div class="pred-bars">
      <div class="pred-bar-row">
        <span class="pred-bar-label">1</span>
        <div class="pred-bar-track"><div class="pred-bar-fill win" style="width:${ph}%"></div></div>
        <span class="pred-bar-pct" style="color:var(--green)">${ph}%</span>
        ${homeBtn}
      </div>
      <div class="pred-bar-row">
        <span class="pred-bar-label">X</span>
        <div class="pred-bar-track"><div class="pred-bar-fill draw" style="width:${pd}%"></div></div>
        <span class="pred-bar-pct" style="color:var(--yellow)">${pd}%</span>
        ${drawBtn}
      </div>
      <div class="pred-bar-row">
        <span class="pred-bar-label">2</span>
        <div class="pred-bar-track"><div class="pred-bar-fill loss" style="width:${pa}%"></div></div>
        <span class="pred-bar-pct" style="color:var(--muted)">${pa}%</span>
        ${awayBtn}
      </div>
    </div>
    ${extraGrid}
    <div class="pred-xg">
      <div class="pred-xg-team">
        <span class="pred-xg-val" style="color:var(--text)">${tip.xg_home.toFixed(2)}</span>
        <span class="pred-xg-label">xG</span>
        <span class="pred-xg-team-name">${esc(home)}</span>
      </div>
      <div class="pred-xg-team" style="align-items:center">
        <span style="font-size:12px;color:var(--muted);font-weight:700">Erw. Tore${infoTip('Expected Goals laut Dixon-Coles. Aktueller WM-2026-Schnitt: ~1.15 Tore pro Team. Werte >1.6 = offensiv stark, <0.8 = stark dominiert.')}</span>
      </div>
      <div class="pred-xg-team" style="align-items:flex-end">
        <span class="pred-xg-val" style="color:var(--text)">${tip.xg_away.toFixed(2)}</span>
        <span class="pred-xg-label">xG</span>
        <span class="pred-xg-team-name">${esc(away)}</span>
      </div>
    </div>
    ${(() => {
      const hsc = tip.top_scorers_home || [];
      const asc = tip.top_scorers_away || [];
      if (!hsc.length && !asc.length) return '';
      const pctColor = p => p >= .45 ? 'var(--green)' : p >= .25 ? 'var(--yellow)' : 'var(--muted)';
      const medals = ['🥇','🥈','🥉'];
      const col = (sc, team) => {
        if (!sc.length) return `<div style="flex:1"><div style="font-size:11px;font-weight:800;color:var(--muted);margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(team)}</div><div style="font-size:11px;color:var(--muted);font-style:italic">Keine Daten</div></div>`;
        return `<div style="flex:1;min-width:0">
          <div style="font-size:11px;font-weight:800;color:var(--muted);margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(team)}</div>
          ${sc.map((p,i) => `
            <div style="display:flex;align-items:center;gap:7px;margin-bottom:7px">
              <span style="font-size:14px;line-height:1;flex-shrink:0">${medals[i]||'·'}</span>
              <span style="font-size:13px;font-weight:700;color:var(--text);flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.name)}</span>
              <span style="font-size:13px;font-weight:900;color:${pctColor(p.p)};flex-shrink:0">${Math.round(p.p*100)}%</span>
            </div>`).join('')}
        </div>`;
      };
      return `<div style="margin-top:12px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:12px 14px">
        <div style="font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:10px">🎯 Torschützen-Prognose</div>
        <div style="display:flex;gap:16px">
          ${col(hsc, home)}
          <div style="width:1px;background:var(--border);flex-shrink:0"></div>
          ${col(asc, away)}
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:8px;padding-top:8px;border-top:1px solid rgba(48,54,61,.4)">StatsBomb xG · gegnerstärke-korrigiert · nur aktuelle Kaderspieler</div>
      </div>`;
    })()}
  </div>`;
}

// ── Signal card ──────────────────────────────────────────────
function buildOuAccordion(ouSigs, autoOpen) {
  if (!ouSigs.length) return '';
  const hasValue = ouSigs.some(s => s.ev_pct >= 3);
  const valueBadge = hasValue ? `<span class="ou-value-badge">⚡ Value</span>` : '';
  return `<div class="ou-accordion${autoOpen ? ' open' : ''}">
    <div class="ou-accordion-header" role="button" tabindex="0" aria-label="Über/Unter-Wetten ein- oder ausklappen" onclick="this.closest('.ou-accordion').classList.toggle('open')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.closest('.ou-accordion').classList.toggle('open');}">
      <span class="ou-accordion-label">Über / Unter</span>
      ${valueBadge}
      <span class="ou-count">${ouSigs.length} Wetten</span>
      <span class="ou-chevron">›</span>
    </div>
    <div class="ou-accordion-body">${ouSigs.map(s => sigCard(s, false)).join('')}</div>
  </div>`;
}
function _modelDots(n) {
  const filled = Math.max(0, Math.min(3, n || 0));
  let out = '';
  for (let i = 0; i < 3; i++) {
    out += `<span class="${i < filled ? 'models-green' : 'models-muted'}">●</span>`;
  }
  return out;
}

// ── Match-Notes (#2): localStorage-Persistenz pro Bet ────────
const _BET_NOTES_KEY = 'sb_bet_notes_v1';
function _loadBetNotes() {
  try { return JSON.parse(localStorage.getItem(_BET_NOTES_KEY) || '{}') || {}; }
  catch { return {}; }
}
function _saveBetNotes(obj) {
  try { localStorage.setItem(_BET_NOTES_KEY, JSON.stringify(obj)); } catch {}
}
function _betNoteKey(home, away, market) {
  return `${(home||'').toLowerCase().trim()}|${(away||'').toLowerCase().trim()}|${(market||'').toLowerCase().trim()}`;
}
function _loadBetNote(home, away, market) {
  return _loadBetNotes()[_betNoteKey(home, away, market)] || '';
}
let _betNoteSaveTimers = {};
function _onBetNoteInput(textarea) {
  const key = textarea.dataset.noteKey;
  if (!key) return;
  clearTimeout(_betNoteSaveTimers[key]);
  _betNoteSaveTimers[key] = setTimeout(() => {
    const notes = _loadBetNotes();
    const v = textarea.value.trim();
    if (v) notes[key] = v; else delete notes[key];
    _saveBetNotes(notes);
    const indicator = document.getElementById('notes-saved-' + key);
    if (indicator) {
      indicator.classList.add('show');
      setTimeout(() => indicator.classList.remove('show'), 1500);
    }
  }, 400);
}

// ── Bet-History Card (#1, Detail-View) ──────────────────────
function _betHistoryCard(home, away) {
  const ptm = (_wmStats && _wmStats.per_team_market) || {};
  const h = ptm[home];
  const a = ptm[away];
  if (!h && !a) return '';
  const teamBlock = (team, data) => {
    if (!data || Object.keys(data).length === 0) {
      return `<div class="bh-team-row">${teamFlag(team)} ${esc(team)}</div>
        <div class="bh-empty">Noch keine settlement-Historie.</div>`;
    }
    // Sort markets by # of bets desc
    const entries = Object.entries(data).sort((x, y) => (y[1].n||0) - (x[1].n||0));
    const pills = entries.map(([mg, b]) => {
      const roi = b.roi == null ? null : b.roi;
      const roiCls = roi == null ? '' : roi > 0 ? 'pos' : roi < 0 ? 'neg' : '';
      const roiStr = roi == null ? '—' : (roi >= 0 ? '+' : '') + roi.toFixed(0) + '%';
      const wl = `${b.won}W ${(b.n - b.won)}L`;
      return `<div class="bh-pill" title="${b.n} Wetten · €${b.pnl >= 0 ? '+' : ''}${b.pnl.toFixed(2)}">
        <span class="bh-mkt">${mg}</span>
        <span class="bh-rec">${wl}</span>
        <span class="bh-roi ${roiCls}">${roiStr}</span>
      </div>`;
    }).join('');
    return `<div class="bh-team-row">${teamFlag(team)} ${esc(team)}</div>
      <div class="bh-grid">${pills}</div>`;
  };
  return `<div class="bh-card">
    <div class="bh-card-title">📊 Wett-Historie</div>
    ${teamBlock(home, h)}
    ${teamBlock(away, a)}
  </div>`;
}

// ── Bookmaker-Matrix (Detail-View, OddsJam/RebelBetting-inspiriert) ──
function _bookieMatrixCard(oddsEntry, sport) {
  if (!oddsEntry || !Array.isArray(oddsEntry.bookmakers_h2h)) return '';
  const rows = oddsEntry.bookmakers_h2h.filter(b =>
    b && b.home > 1 && b.away > 1 && (sport === 'tennis' || b.draw > 1)
  );
  if (rows.length < 2) return '';
  // Beste Quote pro Spalte ermitteln
  const bestHome = Math.max(...rows.map(r => r.home || 0));
  const bestDraw = sport === 'tennis' ? 0 : Math.max(...rows.map(r => r.draw || 0));
  const bestAway = Math.max(...rows.map(r => r.away || 0));
  const sorted = [...rows].sort((a, b) => (b.home + (b.draw||0) + b.away) - (a.home + (a.draw||0) + a.away));
  const top = sorted.slice(0, 8);
  const drawCol = sport === 'tennis' ? '' : '<th>X</th>';
  const tbody = top.map(r => {
    const hCls = r.home === bestHome ? 'bm-best' : '';
    const dCls = (sport !== 'tennis' && r.draw === bestDraw) ? 'bm-best' : '';
    const aCls = r.away === bestAway ? 'bm-best' : '';
    const drawCell = sport === 'tennis' ? '' : `<td class="${dCls}">${(r.draw||0).toFixed(2)}</td>`;
    return `<tr>
      <td>${esc(r.title || r.key || '—')}</td>
      <td class="${hCls}">${r.home.toFixed(2)}</td>
      ${drawCell}
      <td class="${aCls}">${r.away.toFixed(2)}</td>
    </tr>`;
  }).join('');
  return `<details class="bm-matrix">
    <summary>Quoten bei Bookmakern · ${top.length}</summary>
    <table class="bm-matrix-table">
      <thead><tr><th>Bookie</th><th>1</th>${drawCol}<th>2</th></tr></thead>
      <tbody>${tbody}</tbody>
    </table>
  </details>`;
}

// ── Line-Movement Sparkline ──────────────────────────────────
// Liefert SVG-Sparkline + Direction-Badge für ein 1X2-Signal.
// Returns '' wenn keine Historie (≥2 valid points) verfügbar.
function _oddsSparkline(matchStr, side, currentOdds) {
  if (!['home','draw','away'].includes(side)) return '';
  // matchStr: "Home vs Away". Suche in _oddsHistory exakte oder normalisierte Variante.
  const [mh, ma] = matchStr.split(' vs ').map(x => x.trim());
  const nk = matchKey(mh, ma);
  let series = _oddsHistory[matchStr];
  if (!series) {
    for (const [k, v] of Object.entries(_oddsHistory)) {
      const [kh, ka] = k.split(' vs ').map(x => x.trim());
      if (matchKey(kh, ka) === nk) { series = v; break; }
    }
  }
  if (!Array.isArray(series)) return '';
  // Punkte mit gültiger Quote (>1)
  const pts = series
    .map(p => ({ date: p.date, v: parseFloat(p[side]) }))
    .filter(p => p.v > 1.0 && isFinite(p.v));
  // Aktuelle Quote als letzten Punkt hinzufügen, wenn sie sich vom letzten Snapshot unterscheidet
  if (currentOdds > 1.0 && (pts.length === 0 || Math.abs(pts[pts.length-1].v - currentOdds) > 0.005)) {
    pts.push({ date: 'now', v: currentOdds });
  }
  if (pts.length < 2) return '';
  const vals = pts.map(p => p.v);
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const range = maxV - minV || 1;
  const W = 56, H = 16, pad = 2;
  const coords = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (W - 2*pad);
    // y invertiert: fallende Quote (gut für uns) → Linie geht NACH OBEN
    const y = H - pad - ((maxV - v) / range) * (H - 2*pad);
    return [x, y];
  });
  const polyPts = coords.map(([x,y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const [lx, ly] = coords[coords.length-1];
  const first = vals[0], last = vals[vals.length-1];
  const dropPct = ((first - last) / first) * 100;  // positiv = Quote gefallen → gut
  let dirCls, dirSym, dirTxt;
  if (dropPct >= 3) {
    dirCls = 'good'; dirSym = '↘'; dirTxt = `Quote ${first.toFixed(2)}→${last.toFixed(2)} · Markt geht in unsere Richtung`;
  } else if (dropPct <= -3) {
    dirCls = 'bad'; dirSym = '↗'; dirTxt = `Quote ${first.toFixed(2)}→${last.toFixed(2)} · Markt geht gegen uns`;
  } else {
    dirCls = 'flat'; dirSym = '→'; dirTxt = `Quote ${first.toFixed(2)}→${last.toFixed(2)} · stabil`;
  }
  const stroke = dirCls === 'good' ? 'var(--green)' : dirCls === 'bad' ? 'var(--red)' : 'rgba(139,148,158,.7)';
  return `<span class="line-mv ${dirCls}" title="${dirTxt}" aria-label="${dirTxt}">
    <svg class="line-mv-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
      <polyline points="${polyPts}" fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="1.8" fill="${stroke}"/>
    </svg>
    <span class="line-mv-arrow">${dirSym}</span>
  </span>`;
}

function sigCard(s, showMatch) {
  const cls = s.confidence === 'HIGH' ? 'high' : 'medium';
  const evCls = s.ev_pct >= 10 ? 'ev-h' : 'ev-m';
  const [sh, sa] = s.match.split(' vs ').map(x => x.trim());
  const matchLine = showMatch
    ? `<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:8px;display:flex;align-items:center;gap:6px"><span>⚽</span><span>${esc(sh)}</span><span style="color:var(--muted)">vs</span><span>${esc(sa)}</span></div>`
    : '';
  const btnAttrs = [
    `data-match="${esc(s.match)}"`,
    `data-market="${esc(s.market)}"`,
    `data-odds="${s.odds}"`,
    `data-stake="${s.stake_eur}"`,
    `data-ev="${s.ev_pct}"`,
    `data-model-prob="${s.model_prob || 0}"`,
    `data-fair-prob="${s.fair_prob || 0}"`,
    `data-confidence="${esc(s.confidence||'')}"`,
    `data-kickoff="${esc(s.kickoff||'')}"`,
    `data-sport="${esc(s.sport||'')}"`,
  ].join(' ');
  // Prob-Bars: visueller Vergleich Markt vs Modell (nur wenn fair_prob vorhanden)
  const _edge = s.fair_prob > 0 && s.model_prob > 0 ? s.model_prob - s.fair_prob : null;
  let probRow = '';
  if (_edge !== null) {
    const mktPct = Math.max(0, Math.min(100, s.fair_prob));
    const mdlPct = Math.max(0, Math.min(100, s.model_prob));
    const edgeCls = _edge >= 0 ? 'pos' : 'neg';
    const mdlBarCls = _edge >= 0 ? '' : 'neg';
    const edgeSign = _edge >= 0 ? '+' : '';
    probRow = `<div class="edge-bars">
      <div class="edge-bar-row">
        <span class="edge-bar-label">Markt</span>
        <span class="edge-bar-track"><span class="edge-bar-fill market" style="width:${mktPct.toFixed(1)}%"></span></span>
        <span class="edge-bar-val">${s.fair_prob.toFixed(1)}%</span>
      </div>
      <div class="edge-bar-row">
        <span class="edge-bar-label">Modell</span>
        <span class="edge-bar-track"><span class="edge-bar-fill model ${mdlBarCls}" style="width:${mdlPct.toFixed(1)}%"></span></span>
        <span class="edge-bar-val">${s.model_prob.toFixed(1)}%</span>
      </div>
      <div class="edge-bar-delta ${edgeCls}">Edge ${edgeSign}${_edge.toFixed(1)}pp</div>
    </div>`;
  }
  const stakeLabel = s.stake_pct > 0
    ? `€${s.stake_eur.toFixed(0)} <span class="stake-pct">(${s.stake_pct.toFixed(1)}%)</span>`
    : `€${s.stake_eur.toFixed(0)}`;
  const dotsHtml = s.n_models_agree > 0
    ? `<span class="models-dots" title="${s.n_models_agree}/3 Modelle einig">${_modelDots(s.n_models_agree)}</span>`
    : '';
  // Line-Movement Sparkline nur für 1X2-Märkte (für andere fehlt Historie)
  const lineMvHtml = ['home','draw','away'].includes(s.market)
    ? _oddsSparkline(s.match, s.market, s.odds)
    : '';
  // M1+M2: Inline-Drawer „Warum diese Wette?" mit Plain-Language-Erklärung
  let whyInline = '';
  if (_edge !== null) {
    const fpct = s.fair_prob.toFixed(1);
    const mpct = s.model_prob.toFixed(1);
    const edgePp = _edge.toFixed(1);
    const edgeSign = _edge >= 0 ? '+' : '';
    const valCls = _edge >= 0 ? 'pos' : 'neg';
    const profitEur = (s.ev_pct/100*s.stake_eur).toFixed(2);
    let plain;
    if (_edge >= 0) {
      plain = `Unsere KI schätzt die Chance auf <b>${mpct}%</b>, der Markt nur auf <b>${fpct}%</b>. Das sind <b>${edgeSign}${edgePp} Prozentpunkte</b> mehr als die Buchmacher. <i>Wenn</i> die KI im Schnitt recht hat, wäre der erwartete Gewinn bei €${s.stake_eur.toFixed(0)} Einsatz <b>~€${profitEur} pro Wette</b> — keine Garantie, einzelne Wetten können verlieren.`;
    } else {
      plain = `Modell ${mpct}% vs Markt ${fpct}% — kein klarer Edge. Trotzdem im Scanner, weil andere Indikatoren (Form/Modell-Konsens) das ausgleichen.`;
    }
    const openAttr = s.confidence === 'HIGH' ? ' open' : '';
    whyInline = `<details class="why-inline"${openAttr}>
      <summary>💡 Warum diese Wette?</summary>
      <div class="why-inline-body">
        <div class="why-inline-row"><span class="wir-label">KI sagt</span><span class="wir-val">${mpct}%</span></div>
        <div class="why-inline-row"><span class="wir-label">Markt (fair)</span><span class="wir-val">${fpct}%</span></div>
        <div class="why-inline-row"><span class="wir-label">Dein Vorteil</span><span class="wir-val ${valCls}">${edgeSign}${edgePp} pp</span></div>
        <div class="why-inline-text">${plain}</div>
        <div class="why-inline-foot"><a onclick="event.stopPropagation();_openGlossary()">Begriffe erklärt →</a></div>
      </div>
    </details>`;
  }
  const _escA = s => s.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  return `<div class="sig-card ${cls}" style="cursor:pointer" data-match-home="${_escA(sh)}" data-match-away="${_escA(sa)}" onclick="if(!event.target.closest('.place-bet-btn,.why-inline,button,a'))_openMatchDetailFromSignal(this.dataset.matchHome,this.dataset.matchAway)">
    ${matchLine}
    <div class="card-market">${marketLabel(s.market, s.match)}</div>
    <div class="card-footer">
      <span class="odds-btn">${s.odds.toFixed(2)}</span>
      <span class="ev-chip ${evCls}">EV +${s.ev_pct}%${infoTip(`Expected Value (erwarteter Gewinn). +${s.ev_pct}% heißt: wenn du diese Wette 100× spielen würdest, gewinnst du im Schnitt €${(s.ev_pct/100*s.stake_eur).toFixed(2)} pro Wette. SportsBrain zeigt nur Wetten ab EV ≥ 3%.`)}</span>
      <span class="conf-badge conf-${s.confidence}">${s.confidence}${infoTip(s.confidence === 'HIGH' ? 'HIGH = mehrere KI-Modelle sind sich einig. Höchster Vertrauenswert — Einsatz wird +10% erhöht.' : s.confidence === 'LOW' ? 'LOW = grenzwertige Vorhersage. KI ist sich weniger sicher — Einsatz bleibt klein (≤€5) zum Schutz der Bankroll.' : 'MEDIUM = solide Vorhersage, aber nicht alle Modelle stimmen voll überein. Standard-Einsatz €5–15.')}</span>
      ${dotsHtml}
      ${lineMvHtml}
      <span class="stake-val">${stakeLabel}</span>
    </div>
    ${probRow}
    ${whyInline}
    ${['h1_goals_2_4','h2_goals_2_4','h1_goals_2_4_no','h2_goals_2_4_no'].includes(s.market)
      ? `<div style="font-size:10px;color:var(--muted);padding:2px 8px 6px">⚠ HZ-Settlement manuell — Quote beim Buchmacher prüfen</div>`
      : `<button class="place-bet-btn" type="button" onclick="event.stopPropagation();_openBetModalFromBtn(this)" ${btnAttrs} aria-label="Wette platzieren">Wette platzieren · €${s.stake_eur.toFixed(0)}</button>`}
  </div>`;
}

function renderHome() {
  const c = document.getElementById('home-container');


  let games = _schedule.length ? _schedule : _signals.map(s => ({
    sport: s.sport, home: s.match.split(' vs ')[0].trim(),
    away: s.match.split(' vs ')[1].trim(), kickoff: s.kickoff||'', tour: s.tour||'',
  })).filter((g,i,a) => a.findIndex(x=>x.home===g.home&&x.away===g.away)===i);

  // Suchfilter (Team-Name, normalisiert)
  const q = (_homeSearch || '').trim().toLowerCase();
  if (q) {
    const qn = normTeam(q);
    games = games.filter(g => {
      const nh = normTeam(g.home || ''), na = normTeam(g.away || '');
      return nh.includes(qn) || na.includes(qn) || (g.home||'').toLowerCase().includes(q) || (g.away||'').toLowerCase().includes(q);
    });
  }

  if (!games.length) {
    c.innerHTML = q
      ? `<div class="empty"><div class="icon">🔍</div><div>Keine Treffer für „${esc(q)}".<br><small>Versuche einen anderen Team-Namen.</small></div></div>`
      : `<div class="empty"><div class="icon">🔄</div><div>Keine anstehenden Spiele.<br><small>Letztes Update: ${document.getElementById('updated-time').textContent || '…'} · Nächster Scan: 08:00 UTC</small></div></div>`;
    return;
  }

  // Seed oddsMap from all_odds (raw book odds for all games)
  const sigCount = {}, oddsMap = {};
  for (const [rawKey, raw] of Object.entries(_allOdds)) {
    const [rh, ra] = rawKey.split(' vs ').map(x => x.trim());
    const nk = matchKey(rh, ra);
    oddsMap[nk] = {
      home: raw.home > 1 ? { odds: raw.home, ev: null } : null,
      draw: raw.draw > 1 ? { odds: raw.draw, ev: null } : null,
      away: raw.away > 1 ? { odds: raw.away, ev: null } : null,
    };
  }
  // Override with signal odds (value bets) — marked with ev so they show green
  for (const s of _signals) {
    const [sh, sa] = s.match.split(' vs ').map(x => x.trim());
    const nk = matchKey(sh, sa);
    sigCount[nk] = (sigCount[nk]||0) + 1;
    if (!oddsMap[nk]) oddsMap[nk] = {};
    let k = null;
    if (['home','ah-0.5_home','first_set_a'].includes(s.market)) k = 'home';
    else if (s.market === 'draw') k = 'draw';
    else if (['away','ah+0.5_away','first_set_b'].includes(s.market)) k = 'away';
    if (k) oddsMap[nk][k] = { odds: s.odds, ev: s.ev_pct, model_prob: s.model_prob || 0 };
  }

  // Sort + filter next 30 days; remove finished games 30min after final whistle
  // (kickoff + 90min + ~30min Halbzeit/Nachspielzeit + 30min Sichtbarkeit = 2.5h)
  const now = Date.now();
  const cutoff = now + 30*24*36e5;
  const POST_MATCH_VISIBLE_MS = 2.5 * 60 * 60 * 1000;
  const sorted = games
    .filter(g => {
      if (g.kickoff && new Date(g.kickoff) > cutoff) return false;
      if (g.kickoff) {
        const ko = new Date(g.kickoff).getTime();
        if (ko + POST_MATCH_VISIBLE_MS < now) return false; // 30min nach Spielende: weg
      }
      return true;
    })
    .sort((a,b) => {
      if (!a.kickoff && !b.kickoff) return 0;
      if (!a.kickoff) return 1; if (!b.kickoff) return -1;
      return new Date(a.kickoff) - new Date(b.kickoff);
    });

  // Group by date
  const days = {};
  for (const g of sorted) {
    const dk = g.kickoff ? g.kickoff.slice(0,10) : '__';
    if (!days[dk]) days[dk] = [];
    days[dk].push(g);
  }

  const oddsBtn = (nk, mkt, game) => {
    const od = (oddsMap[nk]||{})[mkt];
    if (!od) return `<div class="b365-btn no-odds">—</div>`;
    const cls = od.ev !== null ? 'val' : '';
    if (!game) return `<div class="b365-btn ${cls}">${od.odds.toFixed(2)}</div>`;
    const mkStr = `${game.home} vs ${game.away}`;
    const isValue = od.ev !== null && od.ev >= 3;
    const src = isValue ? 'value' : 'manual';
    const evVal = od.ev !== null ? od.ev : 0;
    const stake = isValue ? 10 : 5;
    const attrs = [
      `type="button"`,
      `class="b365-btn ${cls} b365-btn-click"`,
      `data-match="${esc(mkStr)}"`,
      `data-market="${mkt}"`,
      `data-odds="${od.odds}"`,
      `data-stake="${stake}"`,
      `data-ev="${evVal}"`,
      `data-model-prob="${od.model_prob || 0}"`,
      `data-confidence=""`,
      `data-kickoff="${esc(game.kickoff || '')}"`,
      `data-sport="${esc(game.sport || '')}"`,
      `data-source="${src}"`,
      `onclick="event.stopPropagation();_openBetModalFromBtn(this)"`,
      `aria-label="Wette platzieren · ${mkt} @ ${od.odds.toFixed(2)}"`,
    ].join(' ');
    return `<button ${attrs}>${od.odds.toFixed(2)}</button>`;
  };

  // ── Heute-Sektion (nächste 24h) ──────────────────────────────
  const in24h = now + 24 * 36e5;
  const todayGames = sorted.filter(g => {
    if (!g.kickoff) return false;
    const t = new Date(g.kickoff).getTime();
    return t >= now - 36e5 && t <= in24h; // -1h für laufende Spiele
  });

  let todayHtml = '';
  if (todayGames.length > 0) {
    todayHtml += `<div class="today-header">
      <span class="today-dot"></span>
      <span>Heute · ${todayGames.length} Spiele</span>
      <span class="today-sub" id="today-clock"></span>
    </div>`;

    for (const g of todayGames) {
      const mk = `${g.home} vs ${g.away}`;
      const nk = matchKey(g.home, g.away);
      const n = sigCount[nk] || 0;
      const kickoffTs = new Date(g.kickoff).getTime();
      const minsLeft = Math.round((kickoffTs - now) / 60000);
      const isLive = minsLeft < 0 && minsLeft > -110;
      const timeStr = g.kickoff ? fmtTime(g.kickoff) : '—';

      // Countdown string
      let countdown = '';
      if (isLive) {
        countdown = `<span class="today-live-badge">LIVE</span>`;
      } else if (minsLeft < 60) {
        countdown = `<span class="today-countdown warn">in ${minsLeft} min</span>`;
      } else {
        const h = Math.floor(minsLeft / 60), m = minsLeft % 60;
        countdown = `<span class="today-countdown">in ${h}h ${m}min</span>`;
      }

      // Last-Call badge: Value-Signal vorhanden + Anpfiff < 4h
      const hasLastCall = n > 0 && minsLeft > 0 && minsLeft < 240;
      const lastCallBadge = hasLastCall ? `<span class="last-call-badge">⚡ Last Call</span>` : '';

      // Odds buttons
      const isFootball = g.sport === 'football';
      const odds = isFootball
        ? oddsBtn(nk,'home',g) + oddsBtn(nk,'draw',g) + oddsBtn(nk,'away',g)
        : oddsBtn(nk,'home',g) + oddsBtn(nk,'away',g);

      todayHtml += `<div class="b365-row today-row" role="button" tabindex="0" aria-label="${esc(g.home)} gegen ${esc(g.away)}" onclick='openMatch(${JSON.stringify(mk)})' onkeydown='if(event.key==="Enter"||event.key===" "){event.preventDefault();openMatch(${JSON.stringify(mk)});}'>
        <div class="b365-left">
          <div class="b365-time">
            ${timeStr}
            ${countdown}
            ${lastCallBadge}
            ${n > 0 && !hasLastCall ? `<span class="b365-val-tag">${n} Value</span>` : ''}
          </div>
          <div class="b365-teams">
            <span class="b365-team">${esc(g.home)} ${teamFlag(g.home)}</span>
            <span class="b365-team">${esc(g.away)} ${teamFlag(g.away)}</span>
          </div>
        </div>
        <div class="b365-odds">${odds}</div>
      </div>`;
    }

    todayHtml += `<div style="height:8px"></div>`;
  }

  // Update countdowns every 60s
  clearInterval(window._todayTimer);
  if (todayGames.length > 0) {
    window._todayTimer = setInterval(() => {
      if (document.getElementById('view-home')?.classList.contains('active')) {
        renderHome();
        clearInterval(window._todayTimer);
      }
    }, 60000);
  }

  // ── Smart Suggest ──────────────────────────────────────────
  let suggestHtml = '';
  const free = (_bankrollState && _bankrollState.free != null) ? _bankrollState.free : 0;
  const openCount = (_openBets || []).length;

  if (free > 5) {
    const slots = Math.max(0, 5 - openCount);
    const nowSug = Date.now();
    const now24h = nowSug + 12 * 36e5;
    const openMkSug = new Set((_openBets || []).map(b => {
      const [bh, ba] = (b.match || '').split(' vs ').map(x => x.trim());
      return matchKey(bh, ba);
    }));
    const seenKeys = new Set();
    const picks = [];
    let remaining = free;
    const candidates = (_signals || [])
      .filter(s => s.sport === 'football' && s.ev_pct >= 3)
      .filter(s => {
        if (!s.kickoff) return false;
        const ko = new Date(s.kickoff).getTime();
        return ko > nowSug && ko <= now24h;
      })
      .filter(s => {
        const [sh, sa] = (s.match || '').split(' vs ').map(x => x.trim());
        return !openMkSug.has(matchKey(sh, sa));
      })
      .sort((a, b) => b.ev_pct - a.ev_pct);

    const mktCat = m =>
      ['home','draw','away'].includes(m) ? '1x2'
      : m.startsWith('ah') ? 'ah'
      : m.startsWith('o/u') ? 'ou'
      : m.startsWith('btts_') ? 'btts'
      : m.startsWith('dc_') ? 'dc'
      : m;

    for (const s of candidates) {
      if (picks.length >= 5) break;
      const [sh, sa] = (s.match || '').split(' vs ').map(x => x.trim());
      const dedupKey = matchKey(sh, sa) + '|' + mktCat(s.market);
      if (seenKeys.has(dedupKey)) continue;
      const stake = Math.min(s.stake_eur || 10, remaining);
      if (stake < 3) continue;
      seenKeys.add(dedupKey);
      picks.push({ ...s, display_stake: Math.round(stake) });
      remaining -= stake;
    }

    picks.sort((a, b) => (a.kickoff || '') < (b.kickoff || '') ? -1 : 1);
    if (picks.length > 0) {
      const usedStake = picks.reduce((s, p) => s + p.display_stake, 0);
      const picksHtml = picks.map(p => {
        const [ph, pa] = (p.match || '').split(' vs ').map(x => x.trim());
        const mktLabel = marketLabel(p.market, p.match);
        const evColor = p.ev_pct >= 10 ? 'var(--green)' : 'var(--yellow)';
        const timeStr = p.kickoff ? fmtKickoff(p.kickoff) : '';
        return `<div class="suggest-pick" onclick='openMatch(${JSON.stringify(p.match)})'>
          <div class="suggest-teams-row">${teamFlag(ph)} <strong>${esc(ph)}</strong><span class="vs">vs</span>${teamFlag(pa)} <strong>${esc(pa)}</strong><span style="font-size:11px;color:var(--muted);font-weight:500;margin-left:6px">${esc(timeStr)}</span></div>
          <div class="suggest-bottom-row">
            <span class="suggest-mkt-pill">${esc(mktLabel)}</span>
            <span class="suggest-odds">${p.odds.toFixed(2)}</span>
            <span class="suggest-ev" style="color:${evColor}">+${p.ev_pct.toFixed(1)}%</span>
            <span class="suggest-eur">€${p.display_stake}</span>
          </div>
        </div>`;
      }).join('');
      suggestHtml = `<div class="suggest-card">
        <div class="suggest-header">
          <div>
            <div class="suggest-title">⚡ Empfehlung</div>
            <div class="suggest-subtitle">${slots} freier Slot${slots !== 1 ? 's' : ''} · €${free.toFixed(0)} verfügbar</div>
          </div>
          <div>
            <div class="suggest-stake-big">€${usedStake}</div>
            <div class="suggest-stake-label">Einsatz</div>
          </div>
        </div>
        ${picksHtml}
      </div>`;
    }
  }

  let h = suggestHtml + todayHtml;
  for (const [dk, group] of Object.entries(days)) {
    // Day header
    if (dk === '__') {
      h += `<div class="b365-day-header">Datum ausstehend</div>`;
    } else {
      const dt = new Date(dk + 'T12:00:00');
      const diff = Math.round((dt - new Date().setHours(0,0,0,0)) / 864e5);
      const label = diff === 0 ? 'Heute' : diff === 1 ? 'Morgen'
        : dt.toLocaleDateString('de-DE',{weekday:'long',day:'2-digit',month:'2-digit'});
      h += `<div class="b365-day-header">${label}</div>`;
    }

    // Group by competition within each day
    const comps = {};
    for (const g of group) {
      const ck = g.sport === 'football' ? 'FIFA WM 2026' : (g.tour||'Tennis').toUpperCase();
      if (!comps[ck]) comps[ck] = { sport: g.sport, games: [] };
      comps[ck].games.push(g);
    }

    for (const [compKey, { sport, games: cg }] of Object.entries(comps)) {
      const mktHdr = sport === 'football'
        ? `<span>1</span><span>X</span><span>2</span>`
        : `<span>1</span><span>2</span>`;
      h += `<div class="comp-header">
        <span class="comp-icon">${sport === 'football' ? '🏆' : '🎾'}</span>
        <span class="comp-name">${compKey}</span>
        <span class="comp-meta">${cg.length} Spiele</span>
      </div>
      <div class="mkt-header">${mktHdr}</div>`;

      for (const g of cg) {
        const mk = `${g.home} vs ${g.away}`;
        const nk = matchKey(g.home, g.away);
        const n = sigCount[nk] || 0;
        const timeStr = g.kickoff ? fmtTime(g.kickoff) : '—';
        const isFootball = sport === 'football';
        const result = _wmResults[nk];
        const isCompleted = result && result.home_score != null && result.away_score != null;

        let rightCol;
        if (isCompleted) {
          const resultClass = result.home_score > result.away_score ? 'result-home'
            : result.away_score > result.home_score ? 'result-away' : 'result-draw';
          rightCol = `<div class="score-box ${resultClass}">${result.home_score}–${result.away_score}</div>`;
        } else {
          rightCol = isFootball
            ? oddsBtn(nk,'home',g) + oddsBtn(nk,'draw',g) + oddsBtn(nk,'away',g)
            : oddsBtn(nk,'home',g) + oddsBtn(nk,'away',g);
        }

        h += `<div class="b365-row" role="button" tabindex="0" aria-label="${esc(g.home)} gegen ${esc(g.away)}" onclick='openMatch(${JSON.stringify(mk)})' onkeydown='if(event.key==="Enter"||event.key===" "){event.preventDefault();openMatch(${JSON.stringify(mk)});}'>
          <div class="b365-left">
            <div class="b365-time">${timeStr}${isCompleted?` <span style="font-size:10px;color:var(--muted);font-weight:700">FT</span>`:''} ${n>0&&!isCompleted?` <span class="b365-val-tag">${n} Value</span>`:''}</div>
            <div class="b365-teams">
              <span class="b365-team">${esc(g.home)} ${teamFlag(g.home)}</span>
              <span class="b365-team">${esc(g.away)} ${teamFlag(g.away)}</span>
            </div>
          </div>
          <div class="b365-odds">${rightCol}</div>
        </div>`;
      }
    }
  }
  c.innerHTML = h;
}

// ── Render sport tab (signals grouped by match) ──────────────
function _buildSportControls(sport, filter, totalAll, totalFiltered) {
  const evChips = [0, 3, 5, 10].map(v => {
    const lbl = v === 0 ? 'Alle' : `≥${v}%`;
    const active = filter.ev === v ? ' active' : '';
    return `<span class="filter-chip${active}" data-sf="${sport}" data-key="ev" data-val="${v}" role="tab" tabindex="0" aria-selected="${filter.ev===v}">${lbl}</span>`;
  }).join('');
  const confChips = [['all','Beide'],['HIGH','High'],['MEDIUM','Med']].map(([v, lbl]) => {
    const active = filter.conf === v ? ' active' : '';
    return `<span class="filter-chip${active}" data-sf="${sport}" data-key="conf" data-val="${v}" role="tab" tabindex="0" aria-selected="${filter.conf===v}">${lbl}</span>`;
  }).join('');
  const sortChips = [['kickoff','Kickoff'],['ev','EV'],['odds','Quote']].map(([v, lbl]) => {
    const active = filter.sort === v ? ' active' : '';
    return `<span class="filter-chip${active}" data-sf="${sport}" data-key="sort" data-val="${v}" role="tab" tabindex="0" aria-selected="${filter.sort===v}">${lbl}</span>`;
  }).join('');
  const countTxt = (totalFiltered !== totalAll)
    ? `<span class="sc-label" style="margin-left:auto">${totalFiltered}/${totalAll}</span>`
    : `<span class="sc-label" style="margin-left:auto">${totalAll} Signal${totalAll===1?'':'e'}</span>`;
  return `<div class="sport-controls" role="toolbar" aria-label="Filter und Sortierung">
    <div class="sc-group"><span class="sc-label">EV</span>${evChips}</div>
    <div class="sc-group"><span class="sc-label">Conf</span>${confChips}</div>
    <div class="sc-group"><span class="sc-label">Sort</span>${sortChips}</div>
    ${countTxt}
  </div>`;
}

function _bindSportControls(sport) {
  document.querySelectorAll(`.sport-controls .filter-chip[data-sf="${sport}"]`).forEach(el => {
    const fresh = el.cloneNode(true);
    el.parentNode.replaceChild(fresh, el);
    const handler = () => {
      const key = fresh.dataset.key;
      let val = fresh.dataset.val;
      if (key === 'ev') val = parseInt(val, 10) || 0;
      _setSportFilter(sport, { [key]: val });
      renderSport(sport);
    };
    fresh.addEventListener('click', handler);
    fresh.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); } });
  });
}

function renderSport(sport) {
  const c = document.getElementById(sport + '-container');
  const allSigs = _signals.filter(s => s.sport === sport);
  const filter = _getSportFilter(sport);
  // Filter anwenden
  const sigs = allSigs.filter(s => {
    if (filter.ev > 0 && (s.ev_pct || 0) < filter.ev) return false;
    if (filter.conf !== 'all' && s.confidence !== filter.conf) return false;
    return true;
  });
  const controlsHtml = _buildSportControls(sport, filter, allSigs.length, sigs.length);
  if (!allSigs.length) {
    c.innerHTML = controlsHtml + `<div class="empty"><div class="icon">🔍</div><div>Keine Value Bets.<br><small>Nächster Scan: täglich 08:00 UTC</small></div></div>`;
    _bindSportControls(sport);
    return;
  }
  if (!sigs.length) {
    c.innerHTML = controlsHtml + `<div class="empty"><div class="icon">🎚️</div><div>Keine Signale mit aktuellem Filter.<br><small>Filter lockern oder zurücksetzen.</small></div></div>`;
    _bindSportControls(sport);
    return;
  }
  const groupMap = {};
  for (const s of sigs) {
    if (!groupMap[s.match]) groupMap[s.match] = [];
    groupMap[s.match].push(s);
  }
  const sortedGroups = Object.entries(groupMap).sort(([, a], [, b]) => {
    if (filter.sort === 'ev') {
      const ea = Math.max(...a.map(s => s.ev_pct || 0));
      const eb = Math.max(...b.map(s => s.ev_pct || 0));
      return eb - ea;  // desc
    }
    if (filter.sort === 'odds') {
      const oa = Math.min(...a.map(s => s.odds || 9999));
      const ob = Math.min(...b.map(s => s.odds || 9999));
      return oa - ob;  // asc (niedrigste Quote = Favorit zuerst)
    }
    // default: kickoff
    const ka = a[0].kickoff || '9999', kb = b[0].kickoff || '9999';
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
  // Compact-Mode: dichte Tabelle statt Karten
  if (_compactMode) {
    const sortedSigs = [];
    for (const [, mSigs] of sortedGroups) sortedSigs.push(...mSigs);
    if (filter.sort === 'ev') sortedSigs.sort((a,b) => (b.ev_pct||0) - (a.ev_pct||0));
    else if (filter.sort === 'odds') sortedSigs.sort((a,b) => (a.odds||9999) - (b.odds||9999));
    // kickoff bleibt durch sortedGroups vorgegeben
    const rows = sortedSigs.map(s => {
      const [mh, ma] = s.match.split(' vs ').map(x => x.trim());
      const tKo = s.kickoff ? fmtKickoffCompact(s.kickoff) : '—';
      const evCls = s.ev_pct >= 10 ? '' : 'lo';
      const trCls = s.ev_pct >= 10 ? 'ev-h' : '';
      const lbl = marketLabel(s.market, s.match);
      const isManual = ['h1_goals_2_4','h2_goals_2_4','h1_goals_2_4_no','h2_goals_2_4_no'].includes(s.market);
      const btnAttrs = isManual ? '' : [
        `type="button"`,
        `data-match="${esc(s.match)}"`,
        `data-market="${esc(s.market)}"`,
        `data-odds="${s.odds}"`,
        `data-stake="${s.stake_eur}"`,
        `data-ev="${s.ev_pct}"`,
        `data-model-prob="${s.model_prob || 0}"`,
        `data-confidence="${esc(s.confidence||'')}"`,
        `data-kickoff="${esc(s.kickoff||'')}"`,
        `data-sport="${esc(s.sport||'')}"`,
        `onclick="event.stopPropagation();_openBetModalFromBtn(this)"`,
      ].join(' ');
      const btn = isManual
        ? `<span style="font-size:9px;color:var(--yellow)" title="HZ manuell">⚠ HZ</span>`
        : `<button class="compact-place-btn" ${btnAttrs} aria-label="Wette platzieren">€${s.stake_eur.toFixed(0)}</button>`;
      return `<tr class="${trCls}" onclick='openMatch(${JSON.stringify(s.match)})' role="button" tabindex="0" onkeydown='if(event.key==="Enter"||event.key===" "){event.preventDefault();openMatch(${JSON.stringify(s.match)});}'>
        <td>${tKo}</td>
        <td>${esc(mh)} – ${esc(ma)}</td>
        <td>${esc(lbl)}</td>
        <td>${s.odds.toFixed(2)}</td>
        <td><span class="compact-ev ${evCls}">+${s.ev_pct.toFixed(1)}%</span></td>
        <td>${btn}</td>
      </tr>`;
    }).join('');
    c.innerHTML = controlsHtml + `<div class="compact-table-wrap">
      <table class="compact-table">
        <thead><tr><th>Zeit</th><th>Match</th><th>Markt</th><th>Quote</th><th>EV</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
    _bindSportControls(sport);
    return;
  }

  let h = controlsHtml + '<div class="sport-view-inner">';
  for (const [match, mSigs] of sortedGroups) {
    const s0 = mSigs[0];
    const [mh, ma] = match.split(' vs ').map(x => x.trim());
    const timeStr = s0.kickoff ? fmtKickoff(s0.kickoff) : '';
    const tourStr = sport === 'tennis' ? (s0.tour||'Tennis').toUpperCase() : 'WM 2026';
    h += `<div style="padding:14px 16px 6px;border-bottom:1px solid rgba(48,54,61,.4)">
      <div style="font-size:16px;font-weight:900;letter-spacing:-.3px;margin-bottom:3px">${teamFlag(mh)} ${esc(mh)} <span style="color:var(--muted);font-weight:500">vs</span> ${teamFlag(ma)} ${esc(ma)}</div>
      <div style="font-size:11px;color:var(--muted);font-weight:600">${esc(tourStr)}${timeStr ? ' · ' + timeStr : ''}</div>
    </div>`;
    const ouS = mSigs.filter(s => /^o\/u/.test(s.market));
    const otherS = mSigs.filter(s => !/^o\/u/.test(s.market));
    h += `<div class="match-group-cards">${otherS.map(s => sigCard(s, false)).join('')}${buildOuAccordion(ouS, otherS.length === 0 || ouS.some(s => s.confidence === 'HIGH'))}</div>`;
  }
  h += '</div>';
  c.innerHTML = h;
  _bindSportControls(sport);
}

// ── WM Stats (Sparkline + Trefferquoten) ─────────────────────
let _wmStats = {};

function renderJournalStats(wmStats) {
  const wrap = document.getElementById('wm-stats-container');
  if (!wrap) return;

  const series = (wmStats.series || []);
  const stats = wmStats.stats || {};

  // Sparkline SVG
  let sparkHtml = '';
  if (series.length >= 2) {
    const balances = series.map(p => p.balance);
    const minB = Math.min(...balances), maxB = Math.max(...balances);
    const range = maxB - minB || 1;
    const W = 280, H = 60, pad = 4;
    const pts = series.map((p, i) => {
      const x = pad + (i / (series.length - 1)) * (W - 2*pad);
      const y = H - pad - ((p.balance - minB) / range) * (H - 2*pad);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const last = balances[balances.length - 1];
    const delta = last - 100;
    const deltaStr = (delta >= 0 ? '+' : '') + delta.toFixed(2) + '€';
    const lineColor = delta >= 0 ? 'var(--green)' : 'var(--red)';
    const lastPt = pts.split(' ').pop().split(',');
    const firstLabel = (series[0]?.date?.slice(8,10) || '') + '.' + (series[0]?.date?.slice(5,7) || '') + '.';
    const lastLabel  = (series[series.length-1]?.date?.slice(8,10) || '') + '.' + (series[series.length-1]?.date?.slice(5,7) || '') + '.';
    const firstX = pad, lastX = pad + (W - 2*pad);
    const fillPts = pts + ` ${lastX.toFixed(1)},${H} ${firstX.toFixed(1)},${H}`;
    sparkHtml = `<div class="wm-sparkline">
      <div class="sparkline-header">
        <span class="sparkline-title">Bankroll-Verlauf WM 2026</span>
        <span class="sparkline-delta" style="color:${lineColor}">${deltaStr}</span>
      </div>
      <svg class="sparkline-svg" viewBox="0 0 ${W} ${H}" height="60">
        <polygon points="${fillPts}" fill="${lineColor}" opacity="0.07"/>
        <polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round"/>
        <circle cx="${parseFloat(lastPt[0])}" cy="${parseFloat(lastPt[1])}" r="3" fill="${lineColor}"/>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-top:1px;padding:0 ${pad}px">
        <span>${firstLabel}</span><span>${lastLabel}</span>
      </div>
      <div style="font-size:10px;color:var(--muted);text-align:center;margin-top:4px">Start €100 · Aktuell €${last.toFixed(2)}</div>
    </div>`;
  }

  // Stats-Tabelle
  const rows = [
    ['1X2', stats['1x2']],
    ['Über/Unter', stats['ou25']],
    ['BTTS', stats['btts']],
    ['Sonstige', stats['other']],
  ].filter(([_, d]) => d && d.n > 0).map(([label, d]) =>
    `<tr>
      <td>${label}</td>
      <td>${d.n}</td>
      <td>${d.won}/${d.n}</td>
      <td>${d.hit_rate != null ? d.hit_rate : '—'}%</td>
      <td style="color:${d.roi != null && d.roi >= 0 ? 'var(--green)' : 'var(--red)'}">
        ${d.roi != null ? (d.roi >= 0 ? '+' : '') + d.roi.toFixed(1) + '%' : '—'}
      </td>
      <td style="color:${d.pnl >= 0 ? 'var(--green)' : 'var(--red)'}">
        ${d.pnl >= 0 ? '+' : ''}${d.pnl != null ? d.pnl.toFixed(2) : '0.00'}€
      </td>
    </tr>`
  ).join('');

  const tableHtml = rows ? `<div class="stats-table-wrap" style="margin:0 16px 10px">
    <table class="stats-table">
      <thead><tr><th>Markt</th><th style="text-align:right">Wetten</th><th style="text-align:right">Treffer</th><th style="text-align:right">Hit-%</th><th style="text-align:right">ROI</th><th style="text-align:right">P&amp;L</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>` : '';

  // ── Lifetime-Summary + Drawdown + Histogramme ─────────────
  const summary  = wmStats.summary  || {};
  const drawdown = wmStats.drawdown || {};
  const clvDist  = wmStats.clv_dist  || {};
  const edgeDist = wmStats.edge_dist || {};

  let summaryHtml = '';
  if (summary.n_settled > 0) {
    const yieldVal = summary.yield_pct;
    const yieldCls = yieldVal == null ? '' : yieldVal >= 0 ? 'pos' : 'neg';
    const yieldStr = yieldVal == null ? '—' : (yieldVal >= 0 ? '+' : '') + yieldVal.toFixed(1) + '%';
    const ddCls = (drawdown.max_dd || 0) > 0 ? 'neg' : '';
    const ddStr = drawdown.max_dd_pct != null ? '−' + drawdown.max_dd_pct.toFixed(1) + '%' : '—';
    const ddSub = drawdown.max_dd != null
      ? `−€${drawdown.max_dd.toFixed(2)} · Peak €${(drawdown.peak||0).toFixed(2)}`
      : '';
    const clvVal = summary.mean_clv;
    const clvCls = clvVal == null ? '' : clvVal >= 0 ? 'pos' : 'neg';
    const clvStr = clvVal == null ? '—' : (clvVal >= 0 ? '+' : '') + clvVal.toFixed(2) + '%';
    const clv30Val = summary.mean_clv_30d;
    const clv30Cls = clv30Val == null ? '' : clv30Val >= 0 ? 'pos' : 'neg';
    const clv30Str = clv30Val == null ? '—' : (clv30Val >= 0 ? '+' : '') + clv30Val.toFixed(2) + '%';
    const n_clv_30d = summary.n_clv_30d || 0;
    const edgeVal = summary.mean_edge;
    const edgeCls = edgeVal == null ? '' : edgeVal >= 0 ? 'pos' : 'neg';
    const edgeStr = edgeVal == null ? '—' : (edgeVal >= 0 ? '+' : '') + edgeVal.toFixed(1) + 'pp';
    summaryHtml = `<div class="journal-extra">
      <div class="je-card hero">
        <div class="je-label">📈 Yield Lifetime</div>
        <div class="je-val ${yieldCls}">${yieldStr}</div>
        <div class="je-sub">${summary.n_settled} Wetten · €${(summary.staked||0).toFixed(0)} eingesetzt</div>
      </div>
      <div class="je-card">
        <div class="je-label">Max Drawdown</div>
        <div class="je-val ${ddCls}">${ddStr}</div>
        <div class="je-sub">${ddSub}</div>
      </div>
      <div class="je-card">
        <div class="je-label">Ø CLV Lifetime</div>
        <div class="je-val ${clvCls}">${clvStr}</div>
        <div class="je-sub">${summary.n_clv} Wetten gesamt</div>
      </div>
      <div class="je-card">
        <div class="je-label">Ø CLV letzte 30 Tage</div>
        <div class="je-val ${clv30Cls}">${clv30Str}</div>
        <div class="je-sub">${n_clv_30d} Wetten</div>
      </div>
      <div class="je-card">
        <div class="je-label">Ø Edge</div>
        <div class="je-val ${edgeCls}">${edgeStr}</div>
        <div class="je-sub">${summary.n_edge} Wetten mit Modell-%</div>
      </div>
    </div>`;
  }

  const histHtml = (title, dist, signClass) => {
    if (!dist || !dist.bins || !dist.bins.length) return '';
    const total = dist.bins.reduce((s, n) => s + n, 0);
    if (total === 0) return '';
    const maxV = Math.max(...dist.bins, 1);
    const cols = dist.bins.map((n, i) => {
      const h = n === 0 ? 0 : Math.max(4, (n / maxV) * 44);
      // Klasse: bei CLV: index < 3 (also <0%) negativ; bei Edge: index 0 (≤0pp) negativ; sonst positiv
      let cls = 'zero';
      if (n > 0) cls = signClass(i) ? 'neg' : '';
      return `<div class="hist-col">
        <div class="hist-count">${n || ''}</div>
        <div class="hist-bar ${cls}" style="height:${h}px"></div>
        <div class="hist-label">${dist.labels[i] || ''}</div>
      </div>`;
    }).join('');
    return `<div class="je-card full">
      <div class="je-label">${title} · n=${total}</div>
      <div class="hist-row">${cols}</div>
    </div>`;
  };
  // CLV: bins 0..2 → negativ (<0%), bins 3..5 → positiv
  const clvHist  = histHtml('CLV-Verteilung', clvDist, (i) => i < 3);
  // Edge: bin 0 (≤0pp) → negativ, Rest positiv
  const edgeHist = histHtml('Edge-Verteilung', edgeDist, (i) => i === 0);
  const histsHtml = (clvHist || edgeHist)
    ? `<div class="journal-extra">${clvHist}${edgeHist}</div>`
    : '';

  // Konföderations-Tabelle (#7)
  const byC = wmStats.by_confederation || {};
  let confedHtml = '';
  const confedRows = Object.entries(byC).filter(([_, d]) => d.n > 0);
  if (confedRows.length) {
    // Sortiere nach ROI desc
    confedRows.sort(([, a], [, b]) => (b.roi == null ? -999 : b.roi) - (a.roi == null ? -999 : a.roi));
    const rows = confedRows.map(([c, d]) => {
      const roiCls = d.roi == null ? '' : d.roi >= 0 ? 'pos' : 'neg';
      const pnlCls = d.pnl >= 0 ? 'pos' : 'neg';
      return `<tr>
        <td>${esc(c)}</td>
        <td>${d.n}</td>
        <td>${d.won}/${d.n}</td>
        <td>${d.hit_rate != null ? d.hit_rate : '—'}%</td>
        <td class="fc-pct ${roiCls}" style="font-weight:800">${d.roi != null ? (d.roi >= 0 ? '+' : '') + d.roi.toFixed(1) + '%' : '—'}</td>
        <td class="fc-pct ${pnlCls}" style="font-weight:800">${d.pnl >= 0 ? '+' : ''}${d.pnl.toFixed(2)}€</td>
      </tr>`;
    }).join('');
    confedHtml = `<div class="stats-table-wrap" style="margin:0 16px 10px">
      <div style="font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;padding:8px 10px 2px">🌍 ROI pro Konföderation</div>
      <table class="stats-table">
        <thead><tr><th>Konföd.</th><th style="text-align:right">Wetten</th><th style="text-align:right">Treffer</th><th style="text-align:right">Hit-%</th><th style="text-align:right">ROI</th><th style="text-align:right">P&amp;L</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }

  wrap.innerHTML = (sparkHtml || summaryHtml || tableHtml || histsHtml || confedHtml)
    ? `<div style="padding:12px 16px 4px">${sparkHtml}</div>${summaryHtml}${histsHtml}${tableHtml}${confedHtml}`
    : '';
}

// ── Render Journal ───────────────────────────────────────────
let _journalHistory = [];
const _WD_DE = ['So','Mo','Di','Mi','Do','Fr','Sa'];

function _calcStreak(rows) {
  const sorted = [...rows].sort((a,b) => a.date > b.date ? -1 : 1);
  if (!sorted.length) return { n: 0, type: null };
  const type = sorted[0].pnl > 0 ? 'win' : sorted[0].pnl < 0 ? 'loss' : null;
  if (!type) return { n: 0, type: null };
  let n = 0;
  for (const d of sorted) {
    const t = d.pnl > 0 ? 'win' : d.pnl < 0 ? 'loss' : null;
    if (t === type) n++;
    else break;
  }
  return { n, type };
}

function _buildJournalKpis(rows) {
  if (!rows.length) return '';
  const days = rows.length;
  const winDays = rows.filter(d => d.pnl > 0).length;
  const winRate = days ? (winDays / days * 100) : 0;
  const totPnl = rows.reduce((s,d) => s + (d.pnl || 0), 0);
  const totBets = rows.reduce((s,d) => s + (d.n_bets || 0), 0);
  const avgRoi = days ? rows.reduce((s,d) => s + (d.roi_pct || 0), 0) / days : 0;
  const best = rows.reduce((b,d) => (b == null || d.pnl > b.pnl) ? d : b, null);
  const worst = rows.reduce((b,d) => (b == null || d.pnl < b.pnl) ? d : b, null);
  const fmtDay = (d) => d ? d.date.slice(8,10)+'.'+d.date.slice(5,7)+'.' : '—';

  const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
  const totSign = totPnl >= 0 ? '+' : '';
  return `<div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Gesamt P&L</div>
      <div class="kpi-val ${cls(totPnl)}">${totSign}€${totPnl.toFixed(2)}</div>
      <div class="kpi-sub">${totBets} Wetten · ${days} Tage</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Ø ROI / Tag</div>
      <div class="kpi-val ${cls(avgRoi)}">${avgRoi>=0?'+':''}${avgRoi.toFixed(1)}%</div>
      <div class="kpi-sub">${(() => { const st = _calcStreak(rows); return st.n > 1 ? `${st.n}× ${st.type === 'win' ? '🔥' : '❄️'} Serie` : `${winRate.toFixed(0)}% Gewinn-Tage`; })()}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Bester Tag</div>
      <div class="kpi-val ${cls(best?.pnl ?? 0)}">${best && best.pnl >= 0 ? '+' : ''}€${best ? best.pnl.toFixed(2) : '0.00'}</div>
      <div class="kpi-sub">${fmtDay(best)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Schlecht. Tag</div>
      <div class="kpi-val ${cls(worst?.pnl ?? 0)}">${worst && worst.pnl >= 0 ? '+' : ''}€${worst ? worst.pnl.toFixed(2) : '0.00'}</div>
      <div class="kpi-sub">${fmtDay(worst)}</div>
    </div>
  </div>`;
}

function _buildWeeklyRecap() {
  const sb = _settledBets || [];
  if (!sb.length) return '';
  const cutoff = Date.now() - 7*86400000;
  const recent = sb.filter(b => {
    if (!b.match_date) return false;
    const t = new Date(b.match_date + 'T00:00:00Z').getTime();
    return t >= cutoff;
  });
  if (!recent.length) return '';
  let w=0, l=0, v=0, stake=0, pnl=0, clvSum=0, clvN=0;
  for (const b of recent) {
    const s = (b.status || '').toLowerCase();
    if (s === 'won') w++; else if (s === 'lost') l++; else if (s === 'void') v++;
    stake += +b.stake || 0;
    pnl   += +b.pnl   || 0;
    if (b.clv != null && Number.isFinite(+b.clv)) { clvSum += +b.clv; clvN++; }
  }
  const roi = stake > 0 ? (pnl / stake * 100) : 0;
  const clvAvg = clvN > 0 ? (clvSum / clvN * 100) : null;
  const pnlCls = pnl >= 0 ? 'pos' : 'neg';
  const roiCls = roi >= 0 ? 'pos' : 'neg';
  const clvCls = clvAvg == null ? 'neu' : clvAvg > 0.5 ? 'pos' : clvAvg < -0.5 ? 'neg' : 'neu';
  const clvStr = clvAvg == null ? '—' : ((clvAvg >= 0 ? '+' : '') + clvAvg.toFixed(1) + '%');
  return `<div class="je-card" style="margin-bottom:12px">
    <div class="je-title">📅 Letzte 7 Tage</div>
    <div class="je-sub">${recent.length} Wett${recent.length===1?'e':'en'} · ${w}W / ${v}V / ${l}L</div>
    <div style="display:flex;gap:14px;margin-top:8px;flex-wrap:wrap">
      <div><div class="je-sub">P&amp;L</div><div class="${pnlCls}" style="font-weight:800">${pnl>=0?'+':''}€${pnl.toFixed(2)}</div></div>
      <div><div class="je-sub">ROI</div><div class="${roiCls}" style="font-weight:800">${roi>=0?'+':''}${roi.toFixed(1)}%</div></div>
      <div><div class="je-sub">Ø CLV</div><div class="${clvCls}" style="font-weight:800">${clvStr}</div></div>
    </div>
  </div>`;
}

function _buildDrawdownBanner() {
  const start = (_bankrollState && _bankrollState.start) ? Number(_bankrollState.start) : BANKROLL_START;
  const pnl   = (_bankrollState && typeof _bankrollState.pnl_closed === 'number') ? Number(_bankrollState.pnl_closed) : 0;
  if (!start) return '';
  const equity = start + pnl;
  const ratio  = equity / start;
  if (ratio >= 0.85) return '';
  const ddPct = ((1 - ratio) * 100).toFixed(1);
  const eqStr = equity.toFixed(2);
  const stStr = start.toFixed(2);
  return `<div class="dd-banner" role="alert" aria-live="polite">
    <div class="dd-banner-icon">⚠️</div>
    <div class="dd-banner-body">
      <div class="dd-banner-title">Drawdown-Warnung</div>
      <div class="dd-banner-sub">Aktuelle Bankroll <b>${eqStr}€</b> liegt <span class="dd-banner-num">-${ddPct}%</span> unter Startwert (${stStr}€).</div>
      <div class="dd-banner-hint">Kein Auto-Stop — nur ein Disziplin-Hinweis. Stake-Größen prüfen, keine Tilt-Wetten.</div>
    </div>
  </div>`;
}

function renderJournal(history) {
  if (history) _journalHistory = history;
  const c = document.getElementById('journal-container');
  const all = _journalHistory || [];
  const ddHtml = _buildDrawdownBanner();
  const recapHtml = _buildWeeklyRecap();
  const chipsHtml = `<div class="filter-chips" role="tablist" aria-label="Journal-Filter">
    ${['all','win','loss','7d'].map(f => {
      const lbl = f==='all'?'Alle':f==='win'?'Gewinn-Tage':f==='loss'?'Verlust-Tage':'Letzte 7 Tage';
      return `<span class="filter-chip${_journalFilter===f?' active':''}" role="tab" tabindex="0" aria-selected="${_journalFilter===f}" data-jf="${f}">${lbl}</span>`;
    }).join('')}
  </div>`;

  if (!all.length) {
    c.innerHTML = ddHtml + recapHtml + chipsHtml + `<div class="empty"><div class="icon">📭</div><div>Noch keine abgeschlossenen Wetten.</div></div>`;
    _bindJournalChips();
    return;
  }

  let filtered = all;
  if (_journalFilter === 'win')  filtered = all.filter(d => d.pnl > 0);
  else if (_journalFilter === 'loss') filtered = all.filter(d => d.pnl < 0);
  else if (_journalFilter === '7d') {
    const cutoff = Date.now() - 7*86400000;
    filtered = all.filter(d => new Date(d.date).getTime() >= cutoff);
  }

  const kpisHtml = _buildJournalKpis(filtered);

  if (!filtered.length) {
    c.innerHTML = ddHtml + recapHtml + chipsHtml + kpisHtml + `<div class="empty"><div class="icon">📭</div><div>Keine Einträge in diesem Filter.</div></div>`;
    _bindJournalChips();
    return;
  }

  // Skaliere Trend-Bars relativ zum größten Absolutwert
  const maxAbs = filtered.reduce((m,d) => Math.max(m, Math.abs(d.pnl || 0)), 0) || 1;

  // Aktuellste Tage oben
  const sorted = [...filtered].sort((a,b) => (a.date < b.date ? 1 : -1));

  const rows = sorted.map(d => {
    const pc = d.pnl > 0 ? 'pos' : d.pnl < 0 ? 'neg' : 'neu';
    const rc = d.roi_pct > 0 ? 'pos' : d.roi_pct < 0 ? 'neg' : 'neu';
    const ds = d.date.slice(8,10)+'.'+d.date.slice(5,7)+'.';
    const wd = _WD_DE[(new Date(d.date + 'T00:00:00').getDay())] || '';
    const widthPct = Math.min(50, (Math.abs(d.pnl) / maxAbs) * 50);
    const barCls = d.pnl > 0 ? 'pos' : d.pnl < 0 ? 'neg' : '';
    const barHtml = d.pnl === 0
      ? '<div class="trend-bar-wrap"><div class="trend-bar-mid"></div></div>'
      : `<div class="trend-bar-wrap"><div class="trend-bar-mid"></div><div class="trend-bar-fill ${barCls}" style="width:${widthPct}%"></div></div>`;
    return `<tr>
      <td><span class="journal-day-wd">${wd}</span><span class="journal-day-date">${ds}</span></td>
      <td>${d.n_bets}</td>
      <td class="${pc}" style="font-weight:800">${d.pnl>=0?'+':''}${d.pnl.toFixed(2)}€</td>
      <td class="${rc}" style="font-weight:700">${d.roi_pct>=0?'+':''}${d.roi_pct.toFixed(1)}%</td>
      <td class="trend-cell">${barHtml}</td>
    </tr>`;
  }).join('');

  const tot = filtered.reduce((s,d)=>s+d.pnl,0);
  const tc = tot >= 0 ? 'pos' : 'neg';
  c.innerHTML = ddHtml + recapHtml + chipsHtml + kpisHtml + `<div class="journal-wrap">
    <table class="journal-table">
      <thead><tr><th>Tag</th><th>#</th><th>P&amp;L</th><th>ROI</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="journal-total">
      <span>Gesamt (${filtered.length} Tag${filtered.length===1?'':'e'})</span>
      <span class="${tc}" style="font-weight:800">${tot>=0?'+':''}${tot.toFixed(2)}€</span>
    </div>
  </div>`;
  _bindJournalChips();
}
function _bindJournalChips() {
  document.querySelectorAll('#journal-container .filter-chip[data-jf]').forEach(el => {
    // cloneNode removes accumulated listeners from previous renderJournal() calls
    const fresh = el.cloneNode(true);
    el.parentNode.replaceChild(fresh, el);
    const handler = () => { _journalFilter = fresh.dataset.jf; renderJournal(); };
    fresh.addEventListener('click', handler);
    fresh.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); } });
  });
}

// ── Group Standings ──────────────────────────────────────────
const WM_GROUPS = {
  A:['Mexico','South Africa','South Korea','Czechia'],
  B:['Canada','Bosnia and Herzegovina','Qatar','Switzerland'],
  C:['Brazil','Morocco','Haiti','Scotland'],
  D:['United States','Paraguay','Australia','Turkey'],
  E:['Germany','Curaçao',"Cote d'Ivoire",'Ecuador'],
  F:['Netherlands','Japan','Sweden','Tunisia'],
  G:['Belgium','Egypt','Iran','New Zealand'],
  H:['Spain','Cape Verde','Saudi Arabia','Uruguay'],
  I:['France','Senegal','Iraq','Norway'],
  J:['Argentina','Algeria','Austria','Jordan'],
  K:['Portugal','DR Congo','Uzbekistan','Colombia'],
  L:['England','Croatia','Ghana','Panama'],
};

function renderStandings(targetId = 'standings-container') {
  const c = document.getElementById(targetId);
  if (!c) return;
  const results = _wmResults;
  if (!Object.keys(results).length) {
    c.innerHTML = `<div style="font-size:12px;color:var(--muted);padding:8px 0">Noch keine Ergebnisse — WM startet 11. Juni 2026.</div>`;
    return;
  }

  // Build standings per team from results
  const stats = {};
  // Inverse lookup: any raw team-name (normalized + aliased + reverse-aliased) → canonical group-team name
  const _groupTeamLookup = {};
  const _revAlias = {};
  for (const [k, v] of Object.entries(TEAM_ALIASES)) _revAlias[v] = k;
  for (const teams of Object.values(WM_GROUPS)) {
    for (const t of teams) {
      stats[t] = {p:0,w:0,d:0,l:0,gf:0,ga:0,pts:0,form:[]};
      const n = normTeam(t);
      _groupTeamLookup[n] = t;
      if (TEAM_ALIASES[n]) _groupTeamLookup[TEAM_ALIASES[n]] = t;
      if (_revAlias[n])     _groupTeamLookup[_revAlias[n]]    = t;
    }
  }
  const _toGroupTeam = (raw) => {
    if (!raw) return null;
    const n = normTeam(raw);
    return _groupTeamLookup[n]
        || _groupTeamLookup[TEAM_ALIASES[n] || '']
        || _groupTeamLookup[_revAlias[n]     || '']
        || null;
  };
  for (const r of Object.values(results)) {
    if (r.home_score == null || r.away_score == null) continue;
    const h = _toGroupTeam(r.home), a = _toGroupTeam(r.away);
    const hs = r.home_score, as_ = r.away_score;
    if (!h || !a || !stats[h] || !stats[a]) continue;
    stats[h].p++; stats[h].gf += hs; stats[h].ga += as_;
    stats[a].p++; stats[a].gf += as_; stats[a].ga += hs;
    if (hs > as_) {
      stats[h].w++; stats[h].pts += 3; stats[h].form.push('W');
      stats[a].l++; stats[a].form.push('L');
    } else if (hs === as_) {
      stats[h].d++; stats[h].pts++; stats[h].form.push('D');
      stats[a].d++; stats[a].pts++; stats[a].form.push('D');
    } else {
      stats[a].w++; stats[a].pts += 3; stats[a].form.push('W');
      stats[h].l++; stats[h].form.push('L');
    }
  }

  let html = '';
  for (const [grp, teams] of Object.entries(WM_GROUPS)) {
    const hasAnyMatch = teams.some(t => stats[t] && stats[t].p > 0);
    if (!hasAnyMatch) {
      html += `<div class="standings-group">
        <div class="standings-group-header">Gruppe ${grp}</div>
        <div style="font-size:12px;color:var(--muted);padding:10px 12px;font-weight:600">Noch nicht gespielt</div>
      </div>`;
      continue;
    }
    const sorted = [...teams].sort((a,b) => {
      const sa = stats[a], sb = stats[b];
      if (sb.pts !== sa.pts) return sb.pts - sa.pts;
      const gdA = sa.gf - sa.ga, gdB = sb.gf - sb.ga;
      if (gdB !== gdA) return gdB - gdA;
      return sb.gf - sa.gf;
    });
    const rows = sorted.map((t, i) => {
      const s = stats[t];
      const rowCls = i < 2 ? 'qualify-q' : i === 2 ? 'qualify-po' : '';
      const formHtml = s.form.slice(-3).map(f=>`<span class="form-${f}">${f}</span>`).join('');
      const gd = s.gf - s.ga;
      return `<tr class="${rowCls}">
        <td><span style="color:var(--muted);font-size:10px;font-weight:700;margin-right:6px">${i+1}</span>${esc(t)}</td>
        <td>${s.p}</td><td>${s.w}</td><td>${s.d}</td><td>${s.l}</td>
        <td>${s.gf}:${s.ga}</td>
        <td style="color:${gd>0?'var(--green)':gd<0?'var(--red)':'var(--muted)'}">${gd>0?'+':''}${gd}</td>
        <td class="standings-pts">${s.pts}</td>
        <td class="standings-form">${formHtml}</td>
      </tr>`;
    }).join('');
    html += `<div class="standings-group">
      <div class="standings-group-header">Gruppe ${grp}</div>
      <table class="standings-table">
        <thead><tr><th>Team</th><th>Sp</th><th>S</th><th>U</th><th>N</th><th>Tore</th><th>TD</th><th>Pkt</th><th>Form</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }
  c.innerHTML = html;
}

// ── Forecast Tab (WM 2026 Monte-Carlo) ───────────────────────
let _forecastData = null;
const _FC_SORT_KEY = 'sb_fc_sort_v1';
let _fcSort = (() => {
  try { return JSON.parse(localStorage.getItem(_FC_SORT_KEY)) || { col: 'p_champion', dir: 'desc' }; }
  catch { return { col: 'p_champion', dir: 'desc' }; }
})();

const _FC_COLS = [
  { key: 'group',      lbl: 'G',    fmt: v => v,                                    tip: 'WM-Gruppe' },
  { key: 'team',       lbl: 'Team', fmt: v => `${teamFlag(v)} ${esc(v)}`,           tip: 'Nation' },
  { key: 'p_first',    lbl: '1.',   pct: true,                                      tip: 'P(Gruppensieger)' },
  { key: 'p_advance',  lbl: 'R32',  pct: true,                                      tip: 'P(Einzug in die K.o.-Runde der letzten 32)' },
  { key: 'p_r16',      lbl: 'R16',  pct: true,                                      tip: 'P(Achtelfinale oder weiter)' },
  { key: 'p_qf',       lbl: 'QF',   pct: true,                                      tip: 'P(Viertelfinale oder weiter)' },
  { key: 'p_sf',       lbl: 'SF',   pct: true,                                      tip: 'P(Halbfinale oder weiter)' },
  { key: 'p_final',    lbl: 'Final', pct: true,                                     tip: 'P(Finale oder Titel)' },
  { key: 'p_champion', lbl: '🏆',   pct: true, isChamp: true,                       tip: 'P(Weltmeister)' },
];

function _fcCls(v, isChamp) {
  if (v == null) return 'lo';
  if (isChamp) {
    if (v >= 10) return 'peak';
    if (v >= 3)  return 'hi';
    if (v >= 1)  return 'mid';
    return 'lo';
  }
  if (v >= 70) return 'peak';
  if (v >= 40) return 'hi';
  if (v >= 15) return 'mid';
  return 'lo';
}

async function loadForecast() {
  if (_forecastData) return _forecastData;
  try {
    const r = await fetch('data/wm_forecast.json?t=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _forecastData = await r.json();
  } catch (e) {
    _forecastData = { error: e.message };
  }
  return _forecastData;
}

function renderForecast() {
  const c = document.getElementById('forecast-container');
  if (!c) return;
  if (!_forecastData) {
    c.innerHTML = `<div class="empty"><div class="icon">⏳</div><div>Lade Forecast …</div></div>`;
    loadForecast().then(() => renderForecast());
    return;
  }
  if (_forecastData.error) {
    c.innerHTML = `<div class="empty"><div class="icon">⚠️</div><div>Forecast nicht verfügbar.<br><small>${esc(_forecastData.error)}</small></div></div>`;
    return;
  }
  const d = _forecastData;
  const teams = [...(d.teams || [])];
  // Sortierung
  const col = _fcSort.col, dir = _fcSort.dir;
  teams.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb||'').toLowerCase(); }
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
  const updated = d.updated ? new Date(d.updated).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
  const head = _FC_COLS.map(col => {
    let sortCls = '';
    if (_fcSort.col === col.key) sortCls = _fcSort.dir === 'asc' ? 'sorted-asc' : 'sorted-desc';
    const tip = col.tip ? ` title="${esc(col.tip)}"` : '';
    return `<th class="${sortCls}" data-fc-sort="${col.key}"${tip}>${col.lbl}</th>`;
  }).join('');
  const rows = teams.map(t => {
    const cells = _FC_COLS.map(col => {
      if (col.key === 'group') return `<td>${esc(t.group || '')}</td>`;
      if (col.key === 'team')  return `<td>${col.fmt(t.team)}</td>`;
      const v = t[col.key];
      const cls = _fcCls(v, col.isChamp);
      const txt = v == null ? '—' : v.toFixed(col.isChamp ? 2 : 1) + '%';
      return `<td><span class="fc-pct ${cls}">${txt}</span></td>`;
    }).join('');
    const elimCls = (t.p_advance != null && t.p_advance < 1) ? 'fc-eliminated' : '';
    return `<tr class="${elimCls}">${cells}</tr>`;
  }).join('');
  // xPoints / Modell-Performance
  const xp = d.xpoints || [];
  let xpHtml = '';
  if (xp.length) {
    const xpRows = xp.map(t => {
      const diff = t.diff || 0;
      const diffCls = diff > 0.5 ? 'fc-pct hi' : diff < -0.5 ? 'fc-pct lo' : 'fc-pct mid';
      const diffStr = (diff >= 0 ? '+' : '') + diff.toFixed(2);
      const gfDiff = t.gf_diff || 0;
      const gfDiffCls = gfDiff > 0.3 ? 'fc-pct hi' : gfDiff < -0.3 ? 'fc-pct lo' : 'fc-pct mid';
      const gfDiffStr = (gfDiff >= 0 ? '+' : '') + gfDiff.toFixed(2);
      return `<tr>
        <td>${esc(t.group||'')}</td>
        <td>${teamFlag(t.team)} ${esc(t.team)}</td>
        <td>${t.n}</td>
        <td>${t.pts}</td>
        <td>${(t.xpts||0).toFixed(2)}</td>
        <td><span class="${diffCls}">${diffStr}</span></td>
        <td>${t.gf}</td>
        <td>${(t.xgf||0).toFixed(2)}</td>
        <td><span class="${gfDiffCls}">${gfDiffStr}</span></td>
      </tr>`;
    }).join('');
    xpHtml = `<details class="fc-collapsible" style="margin-top:18px">
      <summary style="cursor:pointer;padding:10px 12px;background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);font-weight:800;color:var(--text);font-size:13px;list-style:none;display:flex;justify-content:space-between;align-items:center">
        <span>📊 Modell-Performance (xPoints)</span>
        <span style="color:var(--muted);font-size:11px;font-weight:600">aufklappen</span>
      </summary>
      <div style="padding-top:10px">
        <div class="fc-sub" style="padding:0 4px 8px">Realisierte Punkte vs DC-Erwartung — positiv = Überperformer, negativ = Unterperformer.</div>
        <div class="fc-table-wrap">
          <table class="fc-table">
            <thead><tr>
              <th>G</th><th>Team</th><th>Sp</th><th>Pkt</th><th>xPkt</th><th>Diff</th><th>Tore</th><th>xG</th><th>ΔxG</th>
            </tr></thead>
            <tbody>${xpRows}</tbody>
          </table>
        </div>
      </div>
    </details>`;
  }

  c.innerHTML = `<div class="fc-header">
    <div class="fc-title">🔮 WM 2026 Forecast</div>
    <div class="fc-sub">Monte-Carlo, ${d.n_trials || 0} Simulationen · ${d.n_played || 0} Spiele gespielt · ${d.n_remaining_group || 0} Gruppenspiele offen · zuletzt ${updated}</div>
  </div>

  <div class="section-title" style="margin-top:14px">Gruppen-Stände (live)</div>
  <div id="forecast-standings-container" style="padding:0 2px 8px"></div>

  <div class="section-title" style="margin-top:14px">⚽ Anstehende Gruppenspiele</div>
  <div id="forecast-group-matches-container" style="padding:0 2px 8px"></div>

  <div class="section-title" style="margin-top:14px">🏆 Vorhersage-Bracket</div>
  <div id="forecast-bracket-container" style="padding:0 2px 8px"></div>

  <div class="section-title" style="margin-top:14px">Stage-Wahrscheinlichkeiten</div>
  <div class="fc-sub" style="padding:0 2px 8px;color:var(--muted)">Jede Spalte = P(mindestens diese Runde erreichen). Spaltenkopf antippen zum Sortieren / Tooltip.</div>
  <div class="fc-table-wrap">
    <table class="fc-table">
      <thead><tr>${head}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>
  <div class="fc-note">P-Werte sind kumulativ: <b>R16</b> = P(im Achtelfinale oder weiter), <b>🏆</b> = P(Weltmeister).
  KO-Bracket nutzt zufällige Paarungen pro Simulation (FIFA-Bracket-Mapping vereinfacht). Bei KO-Unentschieden 50/50-Münzwurf (Elfmeterschießen).</div>
  ${xpHtml}`;
  // Embed live group standings at top
  try { renderStandings('forecast-standings-container'); } catch {}
  // Render upcoming group matches
  try { _renderGroupMatches(d.group_matches); } catch (e) { console.warn('group matches render failed', e); }
  // Render bracket preview
  try { _renderBracket(d.bracket); } catch (e) { console.warn('bracket render failed', e); }
  _bindForecastHeaders();
}

function _renderGroupMatches(matches) {
  const c = document.getElementById('forecast-group-matches-container');
  if (!c) return;
  const pending = (matches || []).filter(m => !m.played);
  if (!pending.length) {
    c.innerHTML = `<div class="fc-sub" style="color:var(--muted);padding:4px">Alle Gruppenspiele gespielt.</div>`;
    return;
  }
  // Group by group letter
  const byGrp = {};
  pending.forEach(m => { (byGrp[m.group] = byGrp[m.group] || []).push(m); });
  let html = '';
  Object.keys(byGrp).sort().forEach(grp => {
    html += `<div style="font-size:11px;font-weight:800;color:var(--muted);letter-spacing:.5px;text-transform:uppercase;margin:8px 0 4px 2px">Gruppe ${esc(grp)}</div>`;
    byGrp[grp].forEach(m => {
      const hasSL = m.scoreline && (m.scoreline.top_scores || []).length > 0;
      const topScore = hasSL ? m.scoreline.top_scores[0] : null;
      const topScoreHtml = topScore
        ? `<span style="font-size:11px;color:var(--muted);font-weight:600">${topScore.h}–${topScore.a} (${topScore.p}%)</span>`
        : '';
      html += `<div class="br-match" style="cursor:pointer;margin-bottom:6px" onclick="_openMatchDetail(${JSON.stringify(m).replace(/</g,'\\u003c')})" title="Match-Details">
        <div class="br-team">
          <span class="br-flag">${teamFlag(m.home)}</span>
          <span class="br-name">${esc(m.home)}</span>
          <span class="br-pct" style="color:var(--muted)">${topScoreHtml}</span>
        </div>
        <div class="br-team">
          <span class="br-flag">${teamFlag(m.away)}</span>
          <span class="br-name">${esc(m.away)}</span>
          <span class="br-pct" style="font-size:10px;color:var(--muted)">Tippen für Details</span>
        </div>
      </div>`;
    });
  });
  c.innerHTML = html;
}

function _renderBracket(bracket) {
  const c = document.getElementById('forecast-bracket-container');
  if (!c) return;
  if (!bracket || bracket.error || !Array.isArray(bracket.rounds) || !bracket.rounds.length) {
    c.innerHTML = `<div class="fc-sub" style="color:var(--muted);padding:8px 4px">Bracket-Vorschau noch nicht verfügbar.</div>`;
    return;
  }
  const champ = bracket.champion;
  let html = '';
  if (champ) {
    html += `<div style="background:linear-gradient(135deg,rgba(0,200,83,.12),rgba(0,200,83,.04));border:1px solid rgba(0,200,83,.3);border-radius:var(--r-lg);padding:12px 14px;margin-bottom:10px;display:flex;align-items:center;gap:12px">
      <div style="font-size:28px">🏆</div>
      <div>
        <div style="font-size:11px;font-weight:700;letter-spacing:.4px;color:var(--green);text-transform:uppercase">Vorhersage Champion</div>
        <div style="font-size:18px;font-weight:900;color:var(--text);margin-top:2px">${teamFlag(champ.team)} ${esc(champ.team)} <span style="font-size:11px;font-weight:700;color:var(--muted);margin-left:6px">Gruppe ${esc(champ.group)}</span></div>
      </div>
    </div>`;
  }
  bracket.rounds.forEach((r, idx) => {
    const isLast = idx === bracket.rounds.length - 1;
    const openAttr = (r.stage === 'r32' || r.stage === 'final') ? 'open' : '';
    const matchHtml = r.matches.map(m => {
      const homeWin = m.winner === m.home;
      const hCls = homeWin ? 'br-team br-win' : 'br-team';
      const aCls = !homeWin ? 'br-team br-win' : 'br-team';
      const hasSL = m.scoreline && (m.scoreline.top_scores || []).length > 0;
      const slHint = hasSL ? `<div style="font-size:10px;color:var(--muted);font-weight:600;margin-top:4px;text-align:right">Tippen für Details</div>` : '';
      return `<div class="br-match" style="cursor:pointer" onclick="_openMatchDetail(${JSON.stringify(m).replace(/</g,'\\u003c')})" title="Match-Details anzeigen">
        <div class="${hCls}">
          <span class="br-seed">${m.home_seed}</span>
          <span class="br-flag">${teamFlag(m.home)}</span>
          <span class="br-name">${esc(m.home)}</span>
          <span class="br-pct">${m.p_home.toFixed(1)}%</span>
        </div>
        <div class="${aCls}">
          <span class="br-seed">${m.away_seed}</span>
          <span class="br-flag">${teamFlag(m.away)}</span>
          <span class="br-name">${esc(m.away)}</span>
          <span class="br-pct">${m.p_away.toFixed(1)}%</span>
        </div>
        ${slHint}
      </div>`;
    }).join('');
    html += `<details class="br-round" ${openAttr} style="background:var(--card);border:1px solid var(--border);border-radius:var(--r-lg);margin-bottom:8px;overflow:hidden">
      <summary style="cursor:pointer;list-style:none;padding:10px 14px;font-weight:800;color:var(--text);font-size:13px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)">
        <span>${esc(r.label)} <span style="color:var(--muted);font-weight:600;font-size:11px;margin-left:6px">${r.matches.length} ${r.matches.length === 1 ? 'Spiel' : 'Spiele'}</span></span>
        <span style="color:var(--muted);font-size:11px;font-weight:600">▾</span>
      </summary>
      <div style="padding:8px">${matchHtml}</div>
    </details>`;
  });
  if (bracket.note) {
    html += `<div class="fc-note" style="margin-top:8px;font-style:italic;font-size:11px;color:var(--muted)">ℹ️ ${esc(bracket.note)}</div>`;
  }
  c.innerHTML = html;
}

function _bindForecastHeaders() {
  document.querySelectorAll('.fc-table th[data-fc-sort]').forEach(el => {
    const fresh = el.cloneNode(true);
    el.parentNode.replaceChild(fresh, el);
    fresh.addEventListener('click', () => {
      const key = fresh.dataset.fcSort;
      if (_fcSort.col === key) {
        _fcSort.dir = _fcSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        _fcSort = { col: key, dir: (key === 'team' || key === 'group') ? 'asc' : 'desc' };
      }
      try { localStorage.setItem(_FC_SORT_KEY, JSON.stringify(_fcSort)); } catch {}
      renderForecast();
    });
  });
}

// ── Squad section for match detail ───────────────────────────
function squadSection(home, away) {
  if (!Object.keys(_squads).length) return '';
  const posLabels = {GK:'TW',DEF:'ABW',MID:'MIT',FWD:'STU'};

  function teamBlock(teamName) {
    // Fuzzy match team name
    const key = Object.keys(_squads).find(k =>
      k.toLowerCase() === teamName.toLowerCase() ||
      k.toLowerCase().includes(teamName.toLowerCase()) ||
      teamName.toLowerCase().includes(k.toLowerCase())
    );
    if (!key) return '';
    const d = _squads[key];
    const players = (d.players || []).slice().sort((a,b) => {
      const po = {GK:0,DEF:1,MID:2,FWD:3};
      return (po[a.pos]??9) - (po[b.pos]??9);
    });
    const injured = players.filter(p => p.status !== 'fit');
    const susp = d.suspended || [];
    let rows = '';
    let lastPos = '';
    for (const p of players) {
      if (p.status !== 'fit' || susp.includes(p.name)) {
        if (p.pos !== lastPos) {
          rows += `<div style="font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;padding:6px 12px 3px;border-bottom:1px solid rgba(48,54,61,.3)">${esc(posLabels[p.pos]||p.pos)}</div>`;
          lastPos = p.pos;
        }
        const sc = p.status === 'injured' ? 'status-injured' : p.status === 'suspended' ? 'status-suspended' : 'status-doubtful';
        const sl = p.status === 'injured' ? '🤕' : p.status === 'suspended' ? '🟡' : '❓';
        rows += `<div class="squad-player-row" style="padding:7px 12px">
          <span class="squad-player-pos">${esc(p.pos||'')}</span>
          <span class="squad-player-name">${esc(p.name)}</span>
          <span class="squad-player-status ${sc}">${sl} ${p.status}</span>
        </div>`;
      }
    }
    for (const s of susp) {
      rows += `<div class="squad-player-row" style="padding:7px 12px">
        <span class="squad-player-pos">—</span>
        <span class="squad-player-name">${esc(s)}</span>
        <span class="squad-player-status status-suspended">🟡 suspended</span>
      </div>`;
    }
    const statusLine = injured.length > 0 || susp.length > 0
      ? `<span style="color:var(--red);font-size:11px">${injured.length + susp.length} Ausfall${injured.length + susp.length !== 1 ? 'e' : ''}</span>`
      : `<span style="color:var(--green);font-size:11px">Kein Ausfall ${d.ampel||'🟢'}</span>`;
    return `<div style="flex:1;min-width:0">
      <div style="font-size:12px;font-weight:800;padding:0 0 5px">${esc(key)} ${statusLine}</div>
      ${rows || `<div style="font-size:11px;color:var(--muted);padding:6px 0">Alle fit 🟢</div>`}
    </div>`;
  }

  const homeBlock = teamBlock(home);
  const awayBlock = teamBlock(away);
  if (!homeBlock && !awayBlock) return '';

  return `<div style="margin-top:4px">
    <div class="section-title" style="padding-top:4px">👥 Kader & Ausfälle</div>
    <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px;display:flex;gap:12px;align-items:flex-start">
      ${homeBlock}
      <div style="width:1px;background:var(--border);align-self:stretch"></div>
      ${awayBlock}
    </div>
  </div>`;
}

// ── D6: Invite-Link-Handling beim ersten Aufruf ──
// URL-Param ?invite=TOKEN setzt sb_invite_pending, räumt URL und zeigt
// Username-Step im Onboarding.
(function _consumeInviteParam() {
  try {
    const u = new URL(window.location.href);
    const inv = u.searchParams.get('invite');
    if (inv && inv.length >= 16) {
      localStorage.setItem('sb_invite_pending', inv);
      u.searchParams.delete('invite');
      const clean = u.pathname + (u.searchParams.toString() ? '?' + u.searchParams.toString() : '') + u.hash;
      window.history.replaceState({}, '', clean);
    }
  } catch {}
})();

// ── M3: Onboarding-Overlay (noob-freundlich, einmalig + nachträglich startbar) ──
const _HAS_INVITE = (() => {
  try { return !!localStorage.getItem('sb_invite_pending'); } catch { return false; }
})();
const ONB_STEPS = [
  {
    title: '👋 Hi, willkommen!',
    text: 'SportsBrain ist deine KI-Assistenz für Sportwetten. Wir analysieren tausende Spiele und zeigen dir nur Wetten, bei denen die KI denkt: <b>hier liegt der Markt daneben</b>. <br><br>⚠️ <b>Wichtig:</b> Auch bei positivem Edge gibt es <u>keine Gewinngarantie</u>. Sportwetten bleiben Glücksspiel mit Risiko — einzelne Wetten können verlieren, und auch über viele Wetten kann die Bilanz negativ sein. Setze nur Geld ein, dessen Verlust du verkraften kannst.'
  },
  ...(_HAS_INVITE ? [{
    title: '👤 Wähle deinen Username',
    text: 'Dein Username ist der Schlüssel zu allen deinen Daten — Ledger, Wetten, Bankroll. Wähle einen kurzen Namen, den nur du kennst.',
    input: 'username'
  }] : []),
  {
    title: '💰 Deine Startbankroll',
    text: 'Wie viel Kapital möchtest du für Sportwetten einsetzen? Der Betrag wird lokal gespeichert und für Einsatz-Empfehlungen (Kelly-Criterion) verwendet.',
    input: 'bankroll'
  },
  {
    title: '🟢 Die wichtigsten Zahlen',
    text: '<b>EV +5%</b> = erwarteter Gewinn pro Wette.<br><b>Edge +3pp</b> = Vorteil ggü. den Buchmachern.<br><b>HIGH/MED/LOW</b> = wie sicher sich die KI ist.<br><br>Keine Sorge — alles ist auch live in der App erklärt (💡-Drawer auf jeder Karte, 📖 Begriffe im Footer).'
  },
  {
    title: '💰 So setzt du eine Wette',
    text: '1. Wette antippen → Details öffnen sich<br>2. „Warum diese Wette?" lesen → KI vs Markt<br>3. Stake bestätigen → wird ins Journal geschrieben<br><br>Den echten Einsatz setzt du beim Buchmacher. Wir tracken nur P&L + CLV.'
  },
  {
    title: '📅 Journal & Tour',
    text: 'Im <b>Journal</b> siehst du dein P&L, ROI der letzten 7 Tage und den Closing-Line-Value (CLV) — der wichtigste Profit-Indikator langfristig.<br><br>Möchtest du eine geführte App-Tour mit Highlights auf den echten Buttons?'
  },
];
let _onbStep = 0;
function _onbRender() {
  const s = ONB_STEPS[_onbStep];
  document.getElementById('onb-modal-title').textContent = s.title;
  document.getElementById('onb-step-text').innerHTML = s.text;
  const brEl = document.getElementById('onb-bankroll-input');
  const brVal = document.getElementById('onb-bankroll-val');
  const unEl = document.getElementById('onb-username-input');
  const unVal = document.getElementById('onb-username-val');
  const unErr = document.getElementById('onb-username-err');
  brEl.style.display = (s.input === 'bankroll') ? 'block' : 'none';
  unEl.style.display = (s.input === 'username') ? 'block' : 'none';
  if (s.input === 'bankroll') {
    const saved = localStorage.getItem('sb_bankroll_start');
    if (saved && !brVal.value) brVal.value = saved;
    setTimeout(() => brVal.focus(), 100);
  } else if (s.input === 'username') {
    unErr.style.display = 'none';
    setTimeout(() => unVal.focus(), 100);
  }
  document.getElementById('onb-step-indicator').innerHTML = ONB_STEPS.map((_,i) =>
    `<span style="width:8px;height:8px;border-radius:50%;background:${i===_onbStep?'var(--green)':'var(--border)'}"></span>`
  ).join('');
  const isLast = _onbStep === ONB_STEPS.length - 1;
  document.getElementById('onb-next').textContent = isLast ? '🎯 Tour starten' : 'Weiter';
  document.getElementById('onb-skip').textContent = isLast ? 'Ohne Tour starten' : 'Überspringen';
}
function _onbSaveBankrollIfNeeded() {
  const s = ONB_STEPS[_onbStep];
  if (s.input !== 'bankroll') return;
  const v = parseFloat(document.getElementById('onb-bankroll-val').value);
  if (v >= 10) {
    try { localStorage.setItem('sb_bankroll_start', String(v)); } catch {}
    _applyUserBankroll(v);
  }
}
// D6: konsumiert sb_invite_pending + gewählten Username, registriert beim Worker.
// Bei Erfolg: speichert sb_user + sb_token, räumt invite. Liefert true/false.
async function _onbRegisterUserIfNeeded() {
  const s = ONB_STEPS[_onbStep];
  if (s.input !== 'username') return true;
  const unVal = document.getElementById('onb-username-val');
  const unErr = document.getElementById('onb-username-err');
  const showErr = (m) => { unErr.textContent = m; unErr.style.display = 'block'; };
  const raw = (unVal.value || '').trim().toLowerCase();
  const clean = raw.replace(/[^a-z0-9_-]/g, '');
  if (clean.length < 3 || clean.length > 20) { showErr('Username muss 3–20 Zeichen lang sein.'); return false; }
  if (clean === 'philip') { showErr('Dieser Username ist reserviert.'); return false; }
  let invite = '';
  try { invite = localStorage.getItem('sb_invite_pending') || ''; } catch {}
  if (!invite) { showErr('Kein Invite-Token gefunden. Bitte Link erneut anklicken.'); return false; }
  try {
    const r = await fetch(WORKER_BASE + '/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ invite, user: clean }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      showErr(j.error ? `Fehler: ${j.error}` : `HTTP ${r.status}`);
      return false;
    }
    try {
      localStorage.setItem('sb_user', j.user);
      localStorage.setItem('sb_token', j.token);
      localStorage.removeItem('sb_invite_pending');
    } catch {}
    return true;
  } catch (e) {
    showErr('Netzwerkfehler: ' + (e.message || e));
    return false;
  }
}
function _onbClose(startWalk) {
  document.getElementById('onb-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
  try { localStorage.setItem('sb_seen_onboarding', '1'); } catch {}
  if (startWalk) setTimeout(_startWalkthrough, 250);
}
function _onbShow() {
  _onbStep = 0;
  _onbRender();
  document.getElementById('onb-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
}
function _onbMaybeShow() {
  // D6: invite pending überstimmt seen-onboarding → Username-Step zeigen.
  try {
    if (localStorage.getItem('sb_invite_pending')) { _onbShow(); return; }
    if (localStorage.getItem('sb_seen_onboarding') === '1') return;
  } catch { return; }
  _onbShow();
}
function _restartOnboarding() {
  try { localStorage.removeItem('sb_seen_onboarding'); } catch {}
  _onbShow();
}
document.getElementById('onb-next').addEventListener('click', async () => {
  const ok = await _onbRegisterUserIfNeeded();
  if (!ok) return;  // Stay on username step until backend confirms
  _onbSaveBankrollIfNeeded();
  if (_onbStep < ONB_STEPS.length - 1) { _onbStep++; _onbRender(); }
  else { _onbClose(true); }
});
document.getElementById('onb-skip').addEventListener('click', () => _onbClose(false));
document.getElementById('onb-modal-bd').addEventListener('click', (e) => {
  if (e.target.id === 'onb-modal-bd') _onbClose(false);
});

// ── M4: Glossar-Modal ──
const GLOSSARY = [
  { term: 'EV (Expected Value)', def: 'Der erwartete Gewinn pro Wette, in Prozent vom Einsatz.', example: 'EV +5% bei €10 Einsatz = im Schnitt +€0.50 Gewinn pro Wette (über viele Wetten gemittelt).' },
  { term: 'Edge', def: 'Wie viel besser die KI-Wahrscheinlichkeit ist als die Markt-Wahrscheinlichkeit, in Prozentpunkten (pp).', example: 'KI: 55%, Markt: 50% → Edge = +5pp. Je größer, desto besser.' },
  { term: 'CLV (Closing Line Value)', def: 'Vergleicht deine Einstiegsquote mit der Endquote kurz vor Anpfiff. Der langfristig wichtigste Profit-Indikator.', example: 'Du wettest @2.50, Quote fällt auf @2.30 → CLV +8.7% = du warst früher schlauer als der Markt.' },
  { term: 'ROI (Return on Investment)', def: 'Dein realer Gewinn geteilt durch den gesamten Einsatz, in Prozent.', example: '€100 eingesetzt, €108 zurück → ROI +8%.' },
  { term: 'Stake (Einsatz)', def: 'Der Geldbetrag, den du auf eine Wette setzt. Bei uns: automatisch via fractional Kelly auf 5% Bankroll begrenzt.', example: 'Bei €100 Bankroll: Stake meist €5–15, max €25.' },
  { term: 'Tier (HIGH/MED/LOW)', def: 'Vertrauenswert der KI. HIGH = mehrere Modelle einig, LOW = grenzwertig.', example: 'HIGH → Einsatz +10%, LOW → Einsatz auf €5 gedeckelt.' },
  { term: 'Quote (Odds)', def: 'Wie viel der Buchmacher pro €1 Einsatz auszahlt, wenn du gewinnst.', example: 'Quote 2.50 + €10 Einsatz = €25 Rückgabe (€15 Gewinn).' },
  { term: 'Fair-Quote / Fair-Probability', def: 'Die „echte" Markt-Wahrscheinlichkeit ohne Buchmacher-Marge (Vig). Berechnet via Shin-Methode aus mehreren Bookies.', example: 'Quote 2.00 → 50% implied. Nach Vig-Bereinigung: 47% fair.' },
  { term: 'Vig / Margin', def: 'Die Buchmacher-Marge — der Aufschlag, der ihre Gewinn-Garantie ist. Typisch 4–8%.', example: 'Heim+Unentschieden+Auswärts addieren in implied % zu ~105% — die 5% sind die Vig.' },
  { term: 'Kelly-Criterion', def: 'Mathematische Formel für die optimale Einsatzgröße basierend auf Edge und Quote. Wir nutzen 25% fractional Kelly = vorsichtig.', example: 'Bei EV +10% und Quote 2.00: Voll-Kelly = 10%, wir wetten 2.5%.' },
  { term: 'Bankroll', def: 'Dein gesamtes Wett-Kapital. Max 5% pro Einzelwette, max 3 aktive Wetten gleichzeitig.', example: '€100 Bankroll → max €5 pro Wette, max €15 in offenen Wetten gleichzeitig.' },
  { term: 'Void (annulliert)', def: 'Wette wird ohne Gewinn/Verlust zurückgegeben — Einsatz kommt zurück. Tritt bei Spielabsagen oder bestimmten Push-Märkten auf.', example: 'AH+0.0 endet 1:1 → void, du bekommst deinen Stake zurück.' },
  { term: 'Hit-Rate', def: 'Anteil der gewonnenen Wetten. Hohe Hit-Rate ≠ profitabel — entscheidend ist EV × Stake.', example: '40% Hit-Rate bei Ø-Quote 3.0 = profitabel (0.40 × 3.0 = 1.20 → +20% ROI).' },
];
function _renderGlossary() {
  document.getElementById('glossary-list').innerHTML = GLOSSARY.map(g =>
    `<div class="glossary-item">
      <div class="glossary-term">${g.term}</div>
      <div class="glossary-def">${g.def}</div>
      <div class="glossary-example">📌 ${g.example}</div>
    </div>`
  ).join('');
}
function _openGlossary() {
  _renderGlossary();
  document.getElementById('glossary-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
}
function _closeGlossary() {
  document.getElementById('glossary-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
}
document.getElementById('glossary-modal-bd').addEventListener('click', (e) => {
  if (e.target.id === 'glossary-modal-bd') _closeGlossary();
});

// ── Match-Detail-Modal (I7) ──
function _openMatchDetail(match) {
  const title = `${teamFlag(match.home)} ${esc(match.home)} vs ${teamFlag(match.away)} ${esc(match.away)}`;
  document.getElementById('match-detail-modal-title').innerHTML = title;

  const groupPart = match.group ? `Gruppe ${esc(match.group)} · ` : '';
  const sub = match.played
    ? `${groupPart}Gespielt: ${match.home_score}–${match.away_score}`
    : `${groupPart}DC-Modell Prognose`;
  document.getElementById('match-detail-modal-sub').textContent = sub;

  let body = '';
  if (match.played) {
    body = `<div class="modal-info" style="text-align:center;font-size:22px;font-weight:900;letter-spacing:1px">${match.home_score} – ${match.away_score}</div>`;
  } else if (match.p_home !== undefined) {
    const ph = match.p_home, pa = match.p_away, pd = match.p_draw ?? 0;
    body += `<div style="display:flex;gap:6px;margin-bottom:12px">
      <div style="flex:1;text-align:center;background:var(--surface);border-radius:var(--r-md);padding:10px 4px">
        <div style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase">Heim</div>
        <div style="font-size:22px;font-weight:900;color:var(--green)">${ph.toFixed(1)}%</div>
        <div style="font-size:11px;color:var(--text);font-weight:600">${esc(match.home)}</div>
      </div>
      <div style="flex:1;text-align:center;background:var(--surface);border-radius:var(--r-md);padding:10px 4px">
        <div style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase">Unentsch.</div>
        <div style="font-size:22px;font-weight:900;color:var(--muted)">${pd.toFixed(1)}%</div>
      </div>
      <div style="flex:1;text-align:center;background:var(--surface);border-radius:var(--r-md);padding:10px 4px">
        <div style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase">Auswärts</div>
        <div style="font-size:22px;font-weight:900;color:var(--amber)">${pa.toFixed(1)}%</div>
        <div style="font-size:11px;color:var(--text);font-weight:600">${esc(match.away)}</div>
      </div>
    </div>`;
  }

  const sl = match.scoreline;
  if (sl) {
    const nMc = sl.n_mc ? sl.n_mc.toLocaleString('de') : '10.000';

    // ── Wahrscheinlichste Ergebnisse: Analytisch vs MC ──
    body += `<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Wahrscheinlichste Ergebnisse</div>`;

    const priorUsed = sl.prior_used === true;
    const dcLabel = priorUsed ? `DC + WC-Prior (${Math.round(sl.alpha * 100)}/${Math.round((1 - sl.alpha) * 100)})` : 'Analytisch (DC)';
    // Header-Zeile
    body += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:4px">
      <div style="text-align:center;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.3px">${esc(dcLabel)}</div>
      <div style="text-align:center;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.3px">Monte Carlo (${nMc} Sims)</div>
    </div>`;

    // Top-3 Scores: analytical links, MC rechts
    const aScores = sl.top_scores || [];
    const mScores = sl.mc_top_scores || [];
    const maxRows = Math.max(aScores.length, mScores.length);
    body += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:12px">`;
    for (let i = 0; i < maxRows; i++) {
      const a = aScores[i], m = mScores[i];
      const aBg = i === 0 ? 'rgba(0,200,83,.12)' : 'var(--surface)';
      const aBorder = i === 0 ? '1px solid rgba(0,200,83,.3)' : '1px solid var(--border)';
      const mBg = i === 0 ? 'rgba(56,139,253,.12)' : 'var(--surface)';
      const mBorder = i === 0 ? '1px solid rgba(56,139,253,.3)' : '1px solid var(--border)';
      body += a ? `<div style="text-align:center;background:${aBg};border:${aBorder};border-radius:var(--r-md);padding:7px 4px">
        <div style="font-size:17px;font-weight:900;color:var(--text)">${a.h}–${a.a}</div>
        <div style="font-size:11px;color:var(--muted);font-weight:600">${a.p}%</div>
      </div>` : '<div></div>';
      body += m ? `<div style="text-align:center;background:${mBg};border:${mBorder};border-radius:var(--r-md);padding:7px 4px">
        <div style="font-size:17px;font-weight:900;color:var(--text)">${m.h}–${m.a}</div>
        <div style="font-size:11px;color:rgba(56,139,253,.9);font-weight:600">${m.p}%</div>
      </div>` : '<div></div>';
    }
    body += `</div>`;

    // ── Tor-Verteilung: beide Methoden als Doppelbalken ──
    const aGd = sl.goal_dist || {};
    const mGd = sl.mc_goal_dist || {};
    const gdLabels = [['0 Tore', '0'], ['1 Tor', '1'], ['2 Tore', '2'], ['3+ Tore', '3+']];
    const hasGd = gdLabels.some(([, k]) => aGd[k] !== undefined || mGd[k] !== undefined);
    if (hasGd) {
      body += `<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Tor-Verteilung (gesamt)</div>`;
      body += `<div style="display:flex;gap:8px;margin-bottom:6px">
        <div style="display:flex;align-items:center;gap:4px"><div style="width:10px;height:10px;border-radius:2px;background:rgba(0,200,83,.7)"></div><span style="font-size:10px;color:var(--muted);font-weight:600">${esc(dcLabel)}</span></div>
        <div style="display:flex;align-items:center;gap:4px"><div style="width:10px;height:10px;border-radius:2px;background:rgba(56,139,253,.7)"></div><span style="font-size:10px;color:var(--muted);font-weight:600">Monte Carlo</span></div>
      </div>`;
      body += `<div style="display:flex;flex-direction:column;gap:6px">`;
      gdLabels.forEach(([label, key]) => {
        const av = aGd[key] ?? 0, mv = mGd[key] ?? 0;
        body += `<div style="display:flex;align-items:center;gap:8px">
          <div style="width:52px;font-size:11px;color:var(--muted);font-weight:600;text-align:right">${esc(label)}</div>
          <div style="flex:1;display:flex;flex-direction:column;gap:2px">
            <div style="background:var(--surface);border-radius:3px;height:8px;overflow:hidden">
              <div style="width:${Math.min(100, av)}%;height:100%;background:rgba(0,200,83,.7);border-radius:3px"></div>
            </div>
            <div style="background:var(--surface);border-radius:3px;height:8px;overflow:hidden">
              <div style="width:${Math.min(100, mv)}%;height:100%;background:rgba(56,139,253,.7);border-radius:3px"></div>
            </div>
          </div>
          <div style="width:72px;font-size:10px;font-weight:700;color:var(--muted);text-align:right">${av}% / ${mv}%</div>
        </div>`;
      });
      body += `</div>`;
    }
  }

  if (!body) body = `<div class="modal-sub" style="text-align:center">Keine Prognose-Daten verfügbar.</div>`;

  document.getElementById('match-detail-modal-body').innerHTML = body;
  document.getElementById('match-detail-modal-bd').classList.add('show');
  document.body.style.overflow = 'hidden';
}
function _closeMatchDetail() {
  document.getElementById('match-detail-modal-bd').classList.remove('show');
  document.body.style.overflow = '';
}

async function _openMatchDetailFromSignal(home, away) {
  const fd = _forecastData || await loadForecast();
  let match = null;
  const nk = matchKey(home, away);
  const nkRev = matchKey(away, home);
  const _mkMatch = m => { const k = matchKey(m.home, m.away); return k === nk || k === nkRev; };
  for (const m of (fd && fd.group_matches) || []) {
    if (_mkMatch(m)) { match = m; break; }
  }
  if (!match && fd && fd.bracket) {
    outer: for (const round of fd.bracket.rounds || []) {
      for (const m of round.matches || []) {
        if (_mkMatch(m)) { match = m; break outer; }
      }
    }
  }
  if (!match) {
    const sigs = _signals.filter(s => {
      const [sh, sa] = s.match.split(' vs ').map(x => x.trim());
      return matchKey(sh, sa) === nk;
    });
    const get = mkt => sigs.find(s => s.market === mkt);
    const hS = get('home'), aS = get('away'), dS = get('draw');
    match = {
      home, away,
      p_home: hS?.model_prob ?? null,
      p_away: aS?.model_prob ?? null,
      p_draw: dS?.model_prob ?? null,
    };
  }
  _openMatchDetail(match);
}
document.getElementById('match-detail-modal-bd').addEventListener('click', (e) => {
  if (e.target.id === 'match-detail-modal-bd') _closeMatchDetail();
});

// ── M3: Walkthrough mit Spotlight ──
// Demo-Modus: injiziert synthetische Daten in alle Views, damit die Tour
// auch ohne aktive Spiele/Wetten konsistent läuft. Wird beim Tour-Ende
// vollständig zurückgesetzt; _load() überspringt Daten-Updates während Demo.
let _walkDemoActive = false;
let _walkDemoBackup = null;

function _walkDemoEnable() {
  if (_walkDemoActive) return;
  _walkDemoBackup = {
    signals: _signals, schedule: _schedule, allOdds: _allOdds,
    openBets: _openBets, settledBets: _settledBets,
    liveScores: _liveScores, journalHistory: _journalHistory,
  };
  const koSoon = new Date(Date.now() + 3*3600000).toISOString();
  const todayIso = new Date().toISOString();
  const yIso = new Date(Date.now() - 86400000).toISOString();
  const y2Iso = new Date(Date.now() - 2*86400000).toISOString();
  const tomIso = new Date(Date.now() + 26*3600000).toISOString();
  _signals = [{
    sport: 'football', match: 'Demo United vs Tour Town', kickoff: koSoon,
    market: 'home', odds: 2.10, ev_pct: 8.5, confidence: 'HIGH',
    stake_eur: 10, stake_pct: 1.5, model_prob: 55.5, fair_prob: 47.0,
    n_models_agree: 3, tour: 'WM 2026',
  }];
  _schedule = [{ sport: 'football', home: 'Demo United', away: 'Tour Town', kickoff: koSoon, tour: 'WM 2026' }];
  _allOdds = { 'Demo United vs Tour Town': { home: 2.10, draw: 3.40, away: 3.20 } };
  _openBets = [
    { home: 'Demo United', away: 'Tour Town', match: 'Demo United vs Tour Town',
      market: 'home', entry_odds: 2.10, stake: 10, confidence: 'HIGH',
      match_date: tomIso, drift_pct: -2.5, clv_signal: 'pending',
      current_odds: 2.05, is_live: false, model_edge_pct: 8.5 },
    { home: 'Beispiel FC', away: 'Demo City', match: 'Beispiel FC vs Demo City',
      market: 'away', entry_odds: 2.80, stake: 8, confidence: 'MEDIUM',
      match_date: todayIso, drift_pct: 1.2, clv_signal: 'pending',
      current_odds: 2.85, is_live: true, model_edge_pct: 4.2 },
  ];
  _settledBets = [
    { home: 'Test Athletic', away: 'Walkthrough FC', market: 'home',
      entry_odds: 2.20, stake: 10, status: 'won', pnl: 12,
      clv: 0.025, closing_odds: 2.15, match_date: yIso },
    { home: 'Showcase SC', away: 'Demo Wanderers', market: 'away',
      entry_odds: 3.10, stake: 8, status: 'lost', pnl: -8,
      clv: -0.015, closing_odds: 3.20, match_date: y2Iso },
    { home: 'Tutorial Town', away: 'Onboarding United', market: 'draw',
      entry_odds: 3.50, stake: 5, status: 'void', pnl: 0,
      clv: 0, closing_odds: 3.50, match_date: y2Iso },
  ];
  _liveScores = { 'beispielfc_vs_democity': { home_score: 0, away_score: 1 } };
  _journalHistory = [
    { date: todayIso.slice(0,10), pnl: 4, bets: 2, roi: 5.0 },
    { date: yIso.slice(0,10), pnl: 12, bets: 1, roi: 10.0 },
    { date: y2Iso.slice(0,10), pnl: -8, bets: 2, roi: -8.0 },
  ];
  _walkDemoActive = true;
  try { renderHome(); } catch {}
  try { renderSport('football'); } catch {}
  try { renderBets(); } catch {}
  try { renderJournal(_journalHistory); } catch {}
}

function _walkDemoDisable() {
  if (!_walkDemoActive || !_walkDemoBackup) return;
  _signals = _walkDemoBackup.signals;
  _schedule = _walkDemoBackup.schedule;
  _allOdds = _walkDemoBackup.allOdds;
  _openBets = _walkDemoBackup.openBets;
  _settledBets = _walkDemoBackup.settledBets;
  _liveScores = _walkDemoBackup.liveScores;
  _journalHistory = _walkDemoBackup.journalHistory;
  _walkDemoBackup = null;
  _walkDemoActive = false;
  try { renderHome(); } catch {}
  try { renderSport('football'); } catch {}
  try { renderBets(); } catch {}
  try { renderJournal(_journalHistory); } catch {}
}

const WALK_STEPS = [
  { sel: '.nav-tab[data-view="home"]',
    title: '🏠 Schritt 1: Home',
    text: 'Hier startest du. Home zeigt alle Spiele für heute & morgen mit den Markt-Quoten. Grüne Quoten = Value-Bet-Kandidat. Tippst du auf ein Spiel, kommst du in die Detail-Ansicht mit allen Signal-Karten.',
    before: () => { try { const t = document.querySelector('.nav-tab[data-view="home"]'); if (t && typeof navTo === 'function') navTo(t); } catch {} } },
  { sel: '.sig-card',
    title: '🎯 Schritt 2: Eine Signal-Karte',
    text: 'Jetzt sind wir im Fußball-Tab — hier siehst du alle Value-Signale als Karten gebündelt. Jede Karte = ein Wett-Vorschlag. Wir gehen jedes Element einzeln durch.',
    before: () => { try { const t = document.querySelector('.nav-tab[data-view="football"]'); if (t && typeof navTo === 'function') navTo(t); } catch {} } },
  { sel: '.sig-card .conf-badge',
    title: '🏷️ Schritt 3: Tier-Badge',
    text: 'HIGH / MEDIUM / LOW = wie überzeugt die KI ist. HIGH = mehrere Modelle stimmen überein (+10% Einsatz). LOW = grenzwertig, Mini-Einsatz ≤€5. MEDIUM = solide, Standard-Einsatz.' },
  { sel: '.sig-card .ev-chip',
    title: '📈 Schritt 4: EV% (Expected Value)',
    text: 'EV% = der erwartete Vorteil über den Markt. EV +5% heißt: würdest du diese Wette 100× spielen, wärst du am Ende durchschnittlich +5% vom Einsatz vorn — wenn die KI im Schnitt recht hat. Wir zeigen nur Wetten ab EV ≥3%.' },
  { sel: '.sig-card .edge-bars',
    title: '📊 Schritt 5: Markt vs Modell',
    text: 'Die zwei Balken sind das Herzstück: oben die Markt-Wahrscheinlichkeit (was die Buchmacher denken), unten die KI-Wahrscheinlichkeit. Der Unterschied = der Edge in Prozentpunkten. Je größer, desto mehr Vorteil.' },
  { sel: '.sig-card .why-inline',
    title: '💡 Schritt 6: „Warum diese Wette?"',
    text: 'Tippe auf diesen Drawer für die Begründung im Klartext. Bei HIGH-Tier ist er standardmäßig offen — du siehst sofort die Logik hinter dem Vorschlag.' },
  { sel: '.sig-card .place-bet-btn',
    title: '✅ Schritt 7: Wette platzieren',
    text: 'Bist du überzeugt? Hier startest du die Wette. Tippe „Weiter" — wir öffnen das Bet-Modal automatisch.',
    after: () => { try {
      const cards = document.querySelectorAll('.sig-card .place-bet-btn');
      for (const b of cards) { const r = b.getBoundingClientRect(); if (r.width > 0) { b.click(); break; } }
    } catch {} } },
  { sel: '#bet-modal-odds-input',
    title: '💰 Schritt 8: Quote anpassen',
    text: 'Falls dein Bookmaker eine etwas andere Quote anbietet, kannst du sie hier ändern — EV & Stake-Vorschlag berechnen sich live neu.',
    before: () => {} },
  { sel: '#bet-modal-stake',
    title: '🪙 Schritt 9: Einsatz',
    text: 'Der vorgeschlagene Einsatz kommt aus Kelly-Criterion + Tier-Anpassung + Bankroll-Schutz (max 5%). Du kannst ihn anpassen — Quick-Buttons darunter helfen.' },
  { sel: '#bet-modal-confirm',
    title: '📥 Schritt 10: Eintragen',
    text: 'Klick speichert die Wette als „pending" im Journal. Den echten Stake setzt du wie gewohnt im Bookmaker. Beim nächsten Sync (alle 2 Min) wandert die Wette in „Offen".',
    after: () => { try { document.getElementById('bet-modal-cancel')?.click(); } catch {} } },
  { sel: '.nav-tab[data-view="bets"]',
    title: '📋 Schritt 11: Wetten-Tab',
    text: 'Hier verfolgst du den Status all deiner Wetten in 3 Phasen — wir schauen sie uns kurz an.',
    before: () => { try { const t = document.querySelector('.nav-tab[data-view="bets"]'); if (t && typeof navTo === 'function') navTo(t); } catch {} } },
  { sel: '.bet-tab:nth-child(1)',
    title: '📂 Schritt 12: „Offen"',
    text: 'Hier landen Wetten, die du eingetragen hast, aber das Spiel hat noch nicht angefangen. Du kannst die Quote noch im Bookmaker setzen.',
    before: () => { try { if (typeof _setBetTab === 'function') _setBetTab('open'); } catch {} } },
  { sel: '.bet-tab:nth-child(2)',
    title: '🔴 Schritt 13: „Live"',
    text: 'Sobald das Spiel läuft, wandert die Wette hierher. Mit Live-Score-Push (Push-Benachrichtigung bei Toren) und Echtzeit-Status.',
    before: () => { try { if (typeof _setBetTab === 'function') _setBetTab('live'); } catch {} } },
  { sel: '.bet-tab:nth-child(3)',
    title: '✅ Schritt 14: „Abgerechnet"',
    text: 'Nach Abpfiff: gewonnen / verloren / void, mit P&L in € und CLV-Pille. Hier siehst du die Ergebnis-Historie und kannst deine Performance bewerten.',
    before: () => { try { if (typeof _setBetTab === 'function') _setBetTab('settled'); } catch {} } },
  { sel: '.nav-tab[data-view="journal"]',
    title: '📅 Schritt 15: Journal',
    text: 'Das Journal zeigt deinen 7-Tage-Recap: Bankroll-Verlauf, ROI, Hit-Rate und vor allem CLV — Closing Line Value.',
    before: () => { try { const t = document.querySelector('.nav-tab[data-view="journal"]'); if (t && typeof navTo === 'function') navTo(t); } catch {} } },
  { sel: '.kpi-card',
    title: '📊 Schritt 16: KPI-Karten & CLV',
    text: 'CLV ist der wichtigste Langzeit-Indikator. Positiver CLV = du hast besser eingeschätzt als der Schluss-Markt = du wirst langfristig wahrscheinlich profitabel sein. Negativer CLV = umgekehrt. Setzt sich erst nach 30-50 Wetten als Signal durch — bis dahin: Varianz!' },
  { sel: null,
    title: '🚀 Du bist startklar!',
    text: 'Das war\'s — geh jetzt auf Home, suche dir deine erste Value-Bet (am besten HIGH-Tier mit Edge ≥5pp) und fang an, den Markt systematisch zu schlagen. Disziplin > Bauchgefühl. Tour jederzeit über ⚙ Settings neu starten. Viel Erfolg! 🎯',
    before: () => { try { const t = document.querySelector('.nav-tab[data-view="home"]'); if (t && typeof navTo === 'function') navTo(t); } catch {} } },
];
let _walkStep = 0;
let _walkBackdropEl = null;
let _walkTooltipEl = null;
let _walkFabEl = null;
let _walkSkipEl = null;

function _walkFindEl(selectors) {
  for (const sel of selectors.split(',')) {
    const all = document.querySelectorAll(sel.trim());
    for (const el of all) {
      // offsetParent reicht nicht: kann auch bei fixed/sticky null sein.
      // Wir prüfen reale Sichtbarkeit per BoundingRect.
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return el;
    }
  }
  return null;
}

function _walkPlaceTooltip(targetRect) {
  const tt = _walkTooltipEl;
  const vh = window.innerHeight, vw = window.innerWidth;
  const ttRect = tt.getBoundingClientRect();
  // Echte FAB-Position dynamisch messen statt fester Guard
  let fabTop = vh - 60;
  if (_walkFabEl) {
    const fr = _walkFabEl.getBoundingClientRect();
    if (fr.height > 0) fabTop = fr.top;
  }
  const maxBottom = fabTop - 12;
  const below = targetRect.bottom + 12;
  const above = targetRect.top - ttRect.height - 12;
  const fitsBelow = below + ttRect.height < maxBottom;
  const fitsAbove = above > 60;
  let top;
  if (fitsBelow) top = below;
  else if (fitsAbove) top = above;
  else {
    // Weder oben noch unten Platz → oben pinnen
    top = 60;
  }
  // Harte obere & untere Grenzen
  top = Math.max(60, Math.min(maxBottom - ttRect.height, top));
  tt.style.top = top + 'px';
  let left = targetRect.left + targetRect.width/2 - ttRect.width/2;
  left = Math.max(8, Math.min(vw - ttRect.width - 8, left));
  tt.style.left = left + 'px';
}

let _walkTargetEl = null;
let _walkRaf = null;
function _walkReposition() {
  if (!_walkTargetEl || !_walkBackdropEl) return;
  const r = _walkTargetEl.getBoundingClientRect();
  const spot = _walkBackdropEl.querySelector('.walk-spot');
  const vh = window.innerHeight, vw = window.innerWidth;
  const offscreen = r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw || (r.width === 0 && r.height === 0);
  if (offscreen) {
    spot.style.display = 'none';
    if (_walkTooltipEl) _walkTooltipEl.style.display = 'none';
    return;
  }
  spot.style.display = 'block';
  if (_walkTooltipEl) _walkTooltipEl.style.display = 'block';
  const pad = 6;
  spot.style.top = (r.top - pad) + 'px';
  spot.style.left = (r.left - pad) + 'px';
  spot.style.width = (r.width + pad*2) + 'px';
  spot.style.height = (r.height + pad*2) + 'px';
  _walkPlaceTooltip(r);
}
function _walkRender() {
  const step = WALK_STEPS[_walkStep];
  if (step.before) { try { step.before(); } catch {} }
  // Delay element lookup so before-hooks (modal open, tab switch) have time to render
  setTimeout(() => {
    const spot = _walkBackdropEl.querySelector('.walk-spot');
    // Step ohne Target: zentriertes Tooltip ohne Spot
    if (!step.sel) {
      _walkTargetEl = null;
      spot.style.display = 'none';
      _walkTooltipEl.style.display = 'block';
      _walkTooltipEl.querySelector('.walk-tooltip-title').textContent = step.title;
      _walkTooltipEl.querySelector('.walk-tooltip-text').textContent = step.text;
      _walkTooltipEl.querySelector('.walk-step-dots').innerHTML = WALK_STEPS.map((_,i) =>
        `<span class="${i===_walkStep?'active':''}"></span>`).join('');
      if (_walkFabEl) _walkFabEl.textContent = _walkStep < WALK_STEPS.length - 1 ? 'Weiter →' : 'Fertig ✓';
      // zentriert
      const ttRect = _walkTooltipEl.getBoundingClientRect();
      _walkTooltipEl.style.top = Math.max(80, (window.innerHeight - ttRect.height) / 2 - 60) + 'px';
      _walkTooltipEl.style.left = Math.max(8, (window.innerWidth - ttRect.width) / 2) + 'px';
      return;
    }
    const el = _walkFindEl(step.sel);
    if (el) {
      _walkTargetEl = el;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(() => {
        spot.style.display = 'block';
        _walkTooltipEl.style.display = 'block';
        _walkTooltipEl.querySelector('.walk-tooltip-title').textContent = step.title;
        _walkTooltipEl.querySelector('.walk-tooltip-text').textContent = step.text;
        _walkTooltipEl.querySelector('.walk-step-dots').innerHTML = WALK_STEPS.map((_,i) =>
          `<span class="${i===_walkStep?'active':''}"></span>`).join('');
        if (_walkFabEl) _walkFabEl.textContent = _walkStep < WALK_STEPS.length - 1 ? 'Weiter →' : 'Fertig ✓';
        _walkReposition();
      }, 350);
    } else {
      if (_walkStep < WALK_STEPS.length - 1) { _walkStep++; _walkRender(); }
      else { _walkEnd(); }
    }
  }, step.before ? 500 : 0);
}
function _walkAdvance() {
  const cur = WALK_STEPS[_walkStep];
  if (cur && cur.after) { try { cur.after(); } catch {} }
  if (_walkStep < WALK_STEPS.length - 1) { _walkStep++; _walkRender(); }
  else { _walkEnd(); }
}
function _walkTick() {
  _walkReposition();
  _walkRaf = requestAnimationFrame(_walkTick);
}

function _startWalkthrough() {
  if (_walkBackdropEl) _walkEnd();
  _walkStep = 0;
  // Demo-Daten injizieren — Tour läuft auch ohne aktive Spiele/Wetten
  try { _walkDemoEnable(); } catch {}
  // Navigate to home so .sig-card is in DOM
  try {
    const homeTab = document.querySelector('.nav-tab[data-view="home"]');
    if (homeTab && typeof navTo === 'function') navTo(homeTab);
  } catch {}
  const bd = document.createElement('div');
  bd.className = 'walk-backdrop show';
  bd.innerHTML = '<div class="walk-spot"></div>';
  document.body.appendChild(bd);
  _walkBackdropEl = bd;
  // Persistente FABs: Weiter (unten) + Skip (oben rechts)
  const fab = document.createElement('button');
  fab.type = 'button';
  fab.className = 'walk-fab';
  fab.textContent = 'Weiter →';
  fab.addEventListener('click', _walkAdvance);
  document.body.appendChild(fab);
  _walkFabEl = fab;
  const sk = document.createElement('button');
  sk.type = 'button';
  sk.className = 'walk-fab-skip';
  sk.textContent = '✕ Tour beenden';
  sk.addEventListener('click', _walkEnd);
  document.body.appendChild(sk);
  _walkSkipEl = sk;
  const tt = document.createElement('div');
  tt.className = 'walk-tooltip';
  tt.style.display = 'none';
  tt.innerHTML = `
    <div class="walk-tooltip-title">—</div>
    <div class="walk-tooltip-text">—</div>
    <div class="walk-step-dots" style="justify-content:center;margin-top:8px"></div>`;
  document.body.appendChild(tt);
  _walkTooltipEl = tt;
  setTimeout(_walkRender, 200);
  if (_walkRaf) cancelAnimationFrame(_walkRaf);
  _walkTick();
}

function _walkEnd() {
  if (_walkRaf) { cancelAnimationFrame(_walkRaf); _walkRaf = null; }
  _walkTargetEl = null;
  if (_walkBackdropEl) { _walkBackdropEl.remove(); _walkBackdropEl = null; }
  if (_walkTooltipEl) { _walkTooltipEl.remove(); _walkTooltipEl = null; }
  if (_walkFabEl) { _walkFabEl.remove(); _walkFabEl = null; }
  if (_walkSkipEl) { _walkSkipEl.remove(); _walkSkipEl = null; }
  // Demo-Daten zurücksetzen, echte Live-Daten neu zeichnen
  try { _walkDemoDisable(); } catch {}
}

