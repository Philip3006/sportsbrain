// SportsBrain Signals Worker
//
// Signals (existing):
//   GET  /signals.json    → reads from KV (public)
//   POST /signals         → writes to KV (Bearer token)
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
  'goals_2_4', 'goals_2_4_no',
  'h1_goals_2_4', 'h1_goals_2_4_no',
  'h2_goals_2_4', 'h2_goals_2_4_no',
]);
const OU_RE = /^o\/u\d+(?:\.\d+)?_(over|under)$/;
const AH_RE = /^ah[+-]\d+(?:\.\d+)?_(home|away|a|b)$/;
const SCORER_RE = /^scorer_[a-zA-Z0-9 '_\-\.]{1,60}$/;

function isValidMarket(m) {
  if (typeof m !== 'string' || !m) return false;
  return ALLOWED_MARKETS.has(m) || OU_RE.test(m) || AH_RE.test(m) || SCORER_RE.test(m);
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

        const rawSource = (body.source || 'value').toString().toLowerCase();
        const source = (rawSource === 'manual') ? 'manual' : 'value';

        const id = crypto.randomUUID();
        const entry = {
          id,
          match,
          market,
          odds,
          stake_eur: stake,
          ev_pct: Number.isFinite(Number(body.ev_pct)) ? Number(body.ev_pct) : null,
          model_prob: Number.isFinite(Number(body.model_prob)) ? Number(body.model_prob) : null,
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
