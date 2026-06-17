// SportsBrain Signals Worker
//
// Signals (existing):
//   GET  /signals.json    → reads from KV (public)
//   POST /signals         → writes to KV (Bearer token)
//   GET  /health          → reads health/status summary from KV (public)
//   POST /automation_status → writes job status to KV (Bearer token)
//
// Pending bets (PWA → local sync):
//   POST   /pending_bets       → append one bet to KV array (Bearer token)
//   GET    /pending_bets       → list (Bearer token)
//   DELETE /pending_bets/{id}  → remove by id (Bearer token)

const ALLOWED_MARKETS = new Set([
  'home', 'draw', 'away',
  'btts_yes', 'btts_no',
  'dc_1x', 'dc_x2', 'dc_12',
  'first_set_a', 'first_set_b',
  'ftts_home', 'ftts_away',
]);
const OU_RE = /^o\/u\d+(?:\.\d+)?_(over|under)$/;
const AH_RE = /^ah[+-]\d+(?:\.\d+)?_(home|away|a|b)$/;
// Player names may contain Unicode letters (João, Álvarez, Aktürkoğlu, …) and
// occasional curly apostrophes (’) — allow \p{L}/\p{M}/\p{N} via the /u flag.
const SCORER_RE = /^scorer_[\p{L}\p{M}\p{N} '’_\-\.]{1,60}$/u;

function isValidMarket(m) {
  if (typeof m !== 'string' || !m) return false;
  return ALLOWED_MARKETS.has(m) || OU_RE.test(m) || AH_RE.test(m) || SCORER_RE.test(m);
}

function minEvPctForMarket(market) {
  return market.startsWith('scorer_') ? 10 : 3;
}

function parseModelProb(value) {
  const n = Number(value);
  if (Number.isFinite(n) && n > 0 && n < 1) return n;
  if (Number.isFinite(n) && n > 1 && n <= 100) return n / 100;
  return NaN;
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...cors() },
  });
}

function requireAuth(request, env) {
  const auth = request.headers.get('Authorization') || '';
  return auth === `Bearer ${env.API_TOKEN}`;
}

async function readPending(env) {
  const raw = await env.SIGNALS.get('pending_bets');
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

async function writePending(env, arr) {
  await env.SIGNALS.put('pending_bets', JSON.stringify(arr));
}

async function readSignalsSnapshot(env) {
  const raw = await env.SIGNALS.get('signals_json');
  if (!raw) return null;
  try {
    const data = JSON.parse(raw);
    return data && typeof data === 'object' ? data : null;
  } catch {
    return null;
  }
}

function isValidAutomationStatus(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return false;
  const status = String(body.status || '').toLowerCase();
  if (!['ok', 'warn', 'error', 'running'].includes(status)) return false;
  if (body.job != null && (typeof body.job !== 'string' || body.job.length > 80)) return false;
  if (body.message != null && (typeof body.message !== 'string' || body.message.length > 500)) return false;
  if (body.generated_at != null && typeof body.generated_at !== 'string') return false;
  return true;
}

// ── Push Subscriptions (Web Push) ─────────────────────────────
// Stored as a JSON array under KV-key "push_subs":
//   [{ endpoint, keys: { p256dh, auth }, created_at, ua? }, ...]
async function readPushSubs(env) {
  const raw = await env.SIGNALS.get('push_subs');
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}
async function writePushSubs(env, arr) {
  await env.SIGNALS.put('push_subs', JSON.stringify(arr));
}
function _validSub(s) {
  return s && typeof s.endpoint === 'string' && s.endpoint.startsWith('https://')
    && s.keys && typeof s.keys.p256dh === 'string' && typeof s.keys.auth === 'string';
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: cors() });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // ── /health (public status summary for dashboard/automation checks) ──
    if (request.method === 'GET' && path === '/health') {
      const snapshot = await readSignalsSnapshot(env);
      const automationRaw = await env.SIGNALS.get('automation_status');
      let automationStatus = null;
      if (automationRaw) {
        try { automationStatus = JSON.parse(automationRaw); } catch {}
      }
      if (!snapshot) {
        return jsonResponse({ ok: false, status: 'error', error: 'no signals snapshot' }, 404);
      }
      const health = snapshot.system_health || {};
      return jsonResponse({
        ok: health.status !== 'error',
        status: health.status || 'unknown',
        updated: snapshot.updated || '',
        system_health: health,
        data_freshness: snapshot.data_freshness || {},
        model_status: snapshot.model_status || {},
        alerts: snapshot.alerts || [],
        automation: automationStatus || snapshot.automation || {},
      });
    }

    // ── /signals.json (public read of latest signals snapshot) ──
    if (request.method === 'GET' && (path === '/signals.json' || path === '/')) {
      const data = await env.SIGNALS.get('signals_json');
      if (!data) {
        return jsonResponse({ error: 'no data yet' }, 404);
      }
      return new Response(data, {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-cache, max-age=0',
          ...cors(),
        },
      });
    }

    // ── POST /signals (write signals snapshot) ──
    if (request.method === 'POST' && path === '/signals') {
      if (!requireAuth(request, env)) return new Response('Unauthorized', { status: 401 });
      const body = await request.text();
      try { JSON.parse(body); } catch { return new Response('Invalid JSON', { status: 400 }); }
      await env.SIGNALS.put('signals_json', body);
      return new Response('OK', { headers: cors() });
    }

    // ── POST /automation_status (write latest job status) ──
    if (request.method === 'POST' && path === '/automation_status') {
      if (!requireAuth(request, env)) return new Response('Unauthorized', { status: 401, headers: cors() });
      let body;
      try { body = await request.json(); } catch { return jsonResponse({ error: 'invalid json' }, 400); }
      if (!isValidAutomationStatus(body)) {
        return jsonResponse({ error: 'invalid automation status payload' }, 400);
      }
      const entry = {
        ...body,
        status: String(body.status).toLowerCase(),
        received_at: new Date().toISOString(),
      };
      await env.SIGNALS.put('automation_status', JSON.stringify(entry));
      return jsonResponse({ ok: true });
    }

    // ── /pending_bets ──
    if (path === '/pending_bets' || path.startsWith('/pending_bets/')) {
      if (!requireAuth(request, env)) {
        return new Response('Unauthorized', { status: 401, headers: cors() });
      }

      if (request.method === 'GET' && path === '/pending_bets') {
        const arr = await readPending(env);
        return jsonResponse({ bets: arr });
      }

      if (request.method === 'POST' && path === '/pending_bets') {
        let body;
        try { body = await request.json(); } catch { return jsonResponse({ error: 'invalid json' }, 400); }

        const match = (body.match || '').toString().trim();
        const market = (body.market || '').toString().trim();
        const odds = Number(body.odds);
        const stake = Number(body.stake_eur);

        if (!match || !match.includes(' vs ')) return jsonResponse({ error: 'match must be "Home vs Away"' }, 400);
        if (!isValidMarket(market)) return jsonResponse({ error: 'unknown market: ' + market }, 400);
        if (!Number.isFinite(odds) || odds < 1.01 || odds > 100) return jsonResponse({ error: 'odds out of range (1.01–100)' }, 400);
        if (!Number.isFinite(stake) || stake < 0.5 || stake > 25) return jsonResponse({ error: 'stake_eur out of range (0.5–25)' }, 400);
        const modelProb = parseModelProb(body.model_prob);
        if (!Number.isFinite(modelProb)) return jsonResponse({ error: 'model_prob required for value recheck' }, 400);
        const suppliedMinEv = Number(body.min_ev_pct);
        const minEvPct = Math.max(
          minEvPctForMarket(market),
          Number.isFinite(suppliedMinEv) ? suppliedMinEv : 0
        );
        const evPct = (modelProb * odds - 1) * 100;
        if (evPct + 1e-9 < minEvPct) {
          const minOdds = (1 + minEvPct / 100) / modelProb;
          return jsonResponse({
            error: `not a value bet after odds change (EV ${evPct.toFixed(1)}%, needs ${minEvPct.toFixed(1)}%; min odds ${minOdds.toFixed(2)}`,
            ev_pct: Number(evPct.toFixed(4)),
            min_ev_pct: minEvPct,
            min_odds: Number(minOdds.toFixed(4)),
          }, 400);
        }

        const rawSource = (body.source || 'value').toString().toLowerCase();
        const source = (rawSource === 'manual') ? 'manual' : 'value';

        const id = crypto.randomUUID();
        const entry = {
          id,
          match,
          market,
          odds,
          stake_eur: stake,
          ev_pct: Number(evPct.toFixed(4)),
          model_prob: modelProb,
          min_ev_pct: minEvPct,
          odds_bookmaker: typeof body.odds_bookmaker === 'string' ? body.odds_bookmaker : '',
          odds_source: typeof body.odds_source === 'string' ? body.odds_source : '',
          confidence: typeof body.confidence === 'string' ? body.confidence : '',
          kickoff: typeof body.kickoff === 'string' ? body.kickoff : '',
          sport: typeof body.sport === 'string' ? body.sport : '',
          source,
          origin: 'pwa',
          placed_at: new Date().toISOString(),
        };

        const arr = await readPending(env);
        // Soft duplicate guard: same match+market+odds within 60s
        const recent = arr.find(b =>
          b.match === entry.match && b.market === entry.market &&
          Math.abs(b.odds - entry.odds) < 0.001 &&
          (Date.now() - new Date(b.placed_at).getTime()) < 60_000
        );
        if (recent) return jsonResponse({ ok: true, id: recent.id, duplicate: true });

        arr.push(entry);
        await writePending(env, arr);
        return jsonResponse({ ok: true, id });
      }

      if (request.method === 'DELETE' && path.startsWith('/pending_bets/')) {
        const id = path.slice('/pending_bets/'.length);
        const arr = await readPending(env);
        const next = arr.filter(b => b.id !== id);
        await writePending(env, next);
        return jsonResponse({ ok: true, removed: arr.length - next.length });
      }
    }

    // ── /push/subscribe (PUBLIC — Browser registriert sich) ──
    if (request.method === 'POST' && path === '/push/subscribe') {
      let sub;
      try { sub = await request.json(); } catch { return jsonResponse({ error: 'invalid json' }, 400); }
      if (!_validSub(sub)) return jsonResponse({ error: 'invalid subscription' }, 400);
      const arr = await readPushSubs(env);
      // Dedup by endpoint
      const filtered = arr.filter(s => s.endpoint !== sub.endpoint);
      filtered.push({
        endpoint:   sub.endpoint,
        keys:       { p256dh: sub.keys.p256dh, auth: sub.keys.auth },
        created_at: new Date().toISOString(),
        ua:         request.headers.get('User-Agent') || '',
      });
      await writePushSubs(env, filtered);
      return jsonResponse({ ok: true, total: filtered.length });
    }

    // ── /push/unsubscribe (PUBLIC — Browser meldet sich ab) ──
    if (request.method === 'POST' && path === '/push/unsubscribe') {
      let body;
      try { body = await request.json(); } catch { return jsonResponse({ error: 'invalid json' }, 400); }
      const endpoint = (body && body.endpoint) || '';
      if (!endpoint) return jsonResponse({ error: 'endpoint required' }, 400);
      const arr = await readPushSubs(env);
      const next = arr.filter(s => s.endpoint !== endpoint);
      await writePushSubs(env, next);
      return jsonResponse({ ok: true, removed: arr.length - next.length });
    }

    // ── /push/list (AUTH — Python liest Subscriptions zum Senden) ──
    if (request.method === 'GET' && path === '/push/list') {
      if (!requireAuth(request, env)) return new Response('Unauthorized', { status: 401, headers: cors() });
      const arr = await readPushSubs(env);
      return jsonResponse({ subs: arr });
    }

    // ── /push/prune (AUTH — Python entfernt expired Subs nach 410-Status) ──
    if (request.method === 'POST' && path === '/push/prune') {
      if (!requireAuth(request, env)) return new Response('Unauthorized', { status: 401, headers: cors() });
      let body;
      try { body = await request.json(); } catch { return jsonResponse({ error: 'invalid json' }, 400); }
      const endpoints = (body && Array.isArray(body.endpoints)) ? body.endpoints : [];
      if (!endpoints.length) return jsonResponse({ ok: true, removed: 0 });
      const arr = await readPushSubs(env);
      const next = arr.filter(s => !endpoints.includes(s.endpoint));
      await writePushSubs(env, next);
      return jsonResponse({ ok: true, removed: arr.length - next.length });
    }

    return new Response('Not Found', { status: 404, headers: cors() });
  },
};

function cors() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
  };
}
