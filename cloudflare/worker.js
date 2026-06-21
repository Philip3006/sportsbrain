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
//
// D2 — Token-Rotation (Master + Per-User-Tokens, KV "user_tokens"):
//   POST  /rotate_token   → generate new user-token, old one 24h grace (Bearer)
//   GET   /token_status   → has_active + grace state (Bearer)
//
// Auth-Modell:
//   • env.API_TOKEN ist der MASTER-Token: bleibt immer gültig, wird via
//     `wrangler secret put API_TOKEN` gesetzt. Python-Cron-Jobs nutzen ihn.
//   • Per-User-Tokens werden vom Master oder von einem laufenden User-Token
//     rotiert; alter Token bleibt 24h gültig (Grace-Period), damit ein
//     PWA-Bug nicht sofort den Zugang killt.

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
// Player names may contain Unicode letters (João, Álvarez, Aktürkoğlu, …) and
// occasional curly apostrophes (’) — allow \p{L}/\p{M}/\p{N} via the /u flag.
const SCORER_RE = /^scorer_[\p{L}\p{M}\p{N} '’_\-\.]{1,60}$/u;

function isValidMarket(m) {
  if (typeof m !== 'string' || !m) return false;
  return ALLOWED_MARKETS.has(m) || OU_RE.test(m) || AH_RE.test(m) || SCORER_RE.test(m);
}

function jsonResponse(obj, status = 200, corsHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders },
  });
}

function _extractBearer(request) {
  const auth = request.headers.get('Authorization') || '';
  return auth.startsWith('Bearer ') ? auth.slice(7) : '';
}

// ── D2 — User-Token-KV ────────────────────────────────────────
// Storage: KV key `user_tokens` → { [user]: { active, previous: { token, expires_at } | null, rotated_at } }
async function readUserTokens(env) {
  const raw = await env.SIGNALS.get('user_tokens');
  if (!raw) return {};
  try { const obj = JSON.parse(raw); return (obj && typeof obj === 'object') ? obj : {}; }
  catch { return {}; }
}
async function writeUserTokens(env, obj) {
  await env.SIGNALS.put('user_tokens', JSON.stringify(obj));
}

function _sanitizeUser(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 32);
}

function _randomToken() {
  const buf = new Uint8Array(32);
  crypto.getRandomValues(buf);
  return Array.from(buf).map(b => b.toString(16).padStart(2,'0')).join('');
}

// Returns { ok, user, viaMaster }. `user` is the matched user-slot (null for master).
async function authResolve(request, env) {
  const token = _extractBearer(request);
  if (!token) return { ok: false, user: null, viaMaster: false };
  if (env.API_TOKEN && token === env.API_TOKEN) {
    return { ok: true, user: null, viaMaster: true };
  }
  const ut = await readUserTokens(env);
  const now = Date.now();
  for (const [user, slot] of Object.entries(ut)) {
    if (!slot) continue;
    if (slot.active && slot.active === token) {
      return { ok: true, user, viaMaster: false };
    }
    if (slot.previous && slot.previous.token === token) {
      const exp = Date.parse(slot.previous.expires_at || '');
      if (Number.isFinite(exp) && exp > now) {
        return { ok: true, user, viaMaster: false };
      }
    }
  }
  return { ok: false, user: null, viaMaster: false };
}

// Backward-compatible shim for existing endpoint guards.
async function requireAuth(request, env) {
  const r = await authResolve(request, env);
  return r.ok;
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
    const origin = request.headers.get('Origin') || '';
    const ch = cors(origin, env);
    const jr = (obj, status = 200) => jsonResponse(obj, status, ch);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: ch });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // ── /signals.json (public read of latest signals snapshot) ──
    if (request.method === 'GET' && (path === '/signals.json' || path === '/')) {
      const data = await env.SIGNALS.get('signals_json');
      if (!data) {
        return jr({ error: 'no data yet' }, 404);
      }
      return new Response(data, {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-cache, max-age=0',
          ...ch,
        },
      });
    }

    // ── POST /signals (write signals snapshot) ──
    if (request.method === 'POST' && path === '/signals') {
      if (!(await requireAuth(request, env))) return new Response('Unauthorized', { status: 401 });
      const body = await request.text();
      try { JSON.parse(body); } catch { return new Response('Invalid JSON', { status: 400 }); }
      await env.SIGNALS.put('signals_json', body);
      return new Response('OK', { headers: ch });
    }

    // ── /pending_bets ──
    if (path === '/pending_bets' || path.startsWith('/pending_bets/')) {
      if (!(await requireAuth(request, env))) {
        return new Response('Unauthorized', { status: 401, headers: ch });
      }

      if (request.method === 'GET' && path === '/pending_bets') {
        const arr = await readPending(env);
        return jr({ bets: arr });
      }

      if (request.method === 'POST' && path === '/pending_bets') {
        let body;
        try { body = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }

        const match = (body.match || '').toString().trim();
        const market = (body.market || '').toString().trim();
        const odds = Number(body.odds);
        const stake = Number(body.stake_eur);

        if (!match || !match.includes(' vs ')) return jr({ error: 'match must be "Home vs Away"' }, 400);
        if (!isValidMarket(market)) return jr({ error: 'unknown market: ' + market }, 400);
        if (!Number.isFinite(odds) || odds < 1.01 || odds > 100) return jr({ error: 'odds out of range (1.01–100)' }, 400);
        if (!Number.isFinite(stake) || stake < 0.5 || stake > 25) return jr({ error: 'stake_eur out of range (0.5–25)' }, 400);

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
        if (recent) return jr({ ok: true, id: recent.id, duplicate: true });

        arr.push(entry);
        await writePending(env, arr);
        return jr({ ok: true, id });
      }

      if (request.method === 'DELETE' && path.startsWith('/pending_bets/')) {
        const id = path.slice('/pending_bets/'.length);
        const arr = await readPending(env);
        const next = arr.filter(b => b.id !== id);
        await writePending(env, next);
        return jr({ ok: true, removed: arr.length - next.length });
      }
    }

    // ── /push/subscribe (PUBLIC — Browser registriert sich) ──
    if (request.method === 'POST' && path === '/push/subscribe') {
      let sub;
      try { sub = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }
      if (!_validSub(sub)) return jr({ error: 'invalid subscription' }, 400);
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
      return jr({ ok: true, total: filtered.length });
    }

    // ── /push/unsubscribe (PUBLIC — Browser meldet sich ab) ──
    if (request.method === 'POST' && path === '/push/unsubscribe') {
      let body;
      try { body = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }
      const endpoint = (body && body.endpoint) || '';
      if (!endpoint) return jr({ error: 'endpoint required' }, 400);
      const arr = await readPushSubs(env);
      const next = arr.filter(s => s.endpoint !== endpoint);
      await writePushSubs(env, next);
      return jr({ ok: true, removed: arr.length - next.length });
    }

    // ── /push/list (AUTH — Python liest Subscriptions zum Senden) ──
    if (request.method === 'GET' && path === '/push/list') {
      if (!(await requireAuth(request, env))) return new Response('Unauthorized', { status: 401, headers: ch });
      const arr = await readPushSubs(env);
      return jr({ subs: arr });
    }

    // ── /push/prune (AUTH — Python entfernt expired Subs nach 410-Status) ──
    if (request.method === 'POST' && path === '/push/prune') {
      if (!(await requireAuth(request, env))) return new Response('Unauthorized', { status: 401, headers: ch });
      let body;
      try { body = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }
      const endpoints = (body && Array.isArray(body.endpoints)) ? body.endpoints : [];
      if (!endpoints.length) return jr({ ok: true, removed: 0 });
      const arr = await readPushSubs(env);
      const next = arr.filter(s => !endpoints.includes(s.endpoint));
      await writePushSubs(env, next);
      return jr({ ok: true, removed: arr.length - next.length });
    }

    // ── D2 — POST /rotate_token (AUTH, Master oder gültiger User-Token) ──
    if (request.method === 'POST' && path === '/rotate_token') {
      const auth = await authResolve(request, env);
      if (!auth.ok) return new Response('Unauthorized', { status: 401, headers: ch });
      let body = {};
      try { body = await request.json(); } catch {}
      let user = _sanitizeUser(body.user || auth.user || 'philip');
      if (!user) user = 'philip';
      const ut = await readUserTokens(env);
      const slot = ut[user] || { active: null, previous: null };
      const newToken = _randomToken();
      const oldActive = slot.active;
      const previous = oldActive ? {
        token: oldActive,
        expires_at: new Date(Date.now() + 24*3600*1000).toISOString(),
      } : null;
      ut[user] = {
        active: newToken,
        previous,
        rotated_at: new Date().toISOString(),
      };
      await writeUserTokens(env, ut);
      return jr({
        ok: true,
        user,
        token: newToken,
        previous_expires_at: previous ? previous.expires_at : null,
      });
    }

    // ── D2 — GET /token_status?user=philip (AUTH) ──
    if (request.method === 'GET' && path === '/token_status') {
      const auth = await authResolve(request, env);
      if (!auth.ok) return new Response('Unauthorized', { status: 401, headers: ch });
      const user = _sanitizeUser(url.searchParams.get('user') || auth.user || 'philip') || 'philip';
      const ut = await readUserTokens(env);
      const slot = ut[user] || null;
      const graceExp = slot && slot.previous ? Date.parse(slot.previous.expires_at || '') : NaN;
      return jr({
        user,
        has_active: !!(slot && slot.active),
        rotated_at: (slot && slot.rotated_at) || null,
        previous_expires_at: (slot && slot.previous && slot.previous.expires_at) || null,
        grace_active: Number.isFinite(graceExp) && graceExp > Date.now(),
      });
    }

    return new Response('Not Found', { status: 404, headers: ch });
  },
};

// ── CORS Allowlist ────────────────────────────────────────────
// Hardcoded defaults: GitHub Pages (Prod-PWA) + localhost (Dev).
// Optional via env.ALLOWED_ORIGINS (comma-separated) für Custom-Domain-Slot,
// sodass z.B. sportsbrain.app ohne Worker-Code-Redeploy ergänzt werden kann.
const _ALLOWED_ORIGINS_EXACT = new Set([
  'https://philip3006.github.io',
]);
const _LOCALHOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

function isAllowedOrigin(origin, env) {
  if (!origin) return false;
  if (_ALLOWED_ORIGINS_EXACT.has(origin)) return true;
  if (_LOCALHOST_RE.test(origin)) return true;
  const extra = (env && typeof env.ALLOWED_ORIGINS === 'string') ? env.ALLOWED_ORIGINS : '';
  if (extra) {
    for (const o of extra.split(',').map(s => s.trim()).filter(Boolean)) {
      if (o === origin) return true;
    }
  }
  return false;
}

function cors(origin, env) {
  const h = {
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    'Vary': 'Origin',
  };
  if (isAllowedOrigin(origin, env)) {
    h['Access-Control-Allow-Origin'] = origin;
  }
  return h;
}
