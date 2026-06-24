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

// D6 — Invite-Tokens (Storage: KV "invites" → { [invite_token]: {created_at, note, used_by, used_at} })
async function readInvites(env) {
  const raw = await env.SIGNALS.get('invites');
  if (!raw) return {};
  try { const obj = JSON.parse(raw); return (obj && typeof obj === 'object') ? obj : {}; }
  catch { return {}; }
}
async function writeInvites(env, obj) {
  await env.SIGNALS.put('invites', JSON.stringify(obj));
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

// D4 — Default user mirrors the legacy single-user KV-key for backward-compat.
const DEFAULT_USER = 'philip';
function _pendingKey(user) {
  return (user && user !== DEFAULT_USER) ? `pending_bets_${user}` : 'pending_bets';
}
function _signalsKey(user) {
  return (user && user !== DEFAULT_USER) ? `signals_json_${user}` : 'signals_json';
}

async function readPending(env, user = DEFAULT_USER) {
  const raw = await env.SIGNALS.get(_pendingKey(user));
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

async function writePending(env, arr, user = DEFAULT_USER) {
  await env.SIGNALS.put(_pendingKey(user), JSON.stringify(arr));
}

function _cancelKey(user) {
  return (user && user !== DEFAULT_USER) ? `cancel_requests_${user}` : 'cancel_requests';
}
async function readCancelRequests(env, user = DEFAULT_USER) {
  const raw = await env.SIGNALS.get(_cancelKey(user));
  if (!raw) return [];
  try { const a = JSON.parse(raw); return Array.isArray(a) ? a : []; } catch { return []; }
}
async function writeCancelRequests(env, arr, user = DEFAULT_USER) {
  await env.SIGNALS.put(_cancelKey(user), JSON.stringify(arr));
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

    // ── /signals.json (public read; user-aware via auth or ?user=) ──
    // D4: if a valid user-token is presented OR ?user= is given (master-only),
    // serve signals_json_{user}; otherwise the legacy default-user snapshot.
    if (request.method === 'GET' && (path === '/signals.json' || path === '/')) {
      let user = DEFAULT_USER;
      const auth = await authResolve(request, env);
      if (auth.ok && auth.user) user = auth.user;
      const qUser = _sanitizeUser(url.searchParams.get('user') || '');
      if (qUser && (auth.viaMaster || qUser === auth.user)) user = qUser;
      let data = await env.SIGNALS.get(_signalsKey(user));
      // Fallback: if per-user snapshot doesn't exist yet, serve default.
      if (!data && user !== DEFAULT_USER) {
        data = await env.SIGNALS.get(_signalsKey(DEFAULT_USER));
      }
      if (!data) return jr({ error: 'no data yet' }, 404);
      return new Response(data, {
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-cache, max-age=0',
          ...ch,
        },
      });
    }

    // ── POST /signals (write signals snapshot; user-aware) ──
    if (request.method === 'POST' && path === '/signals') {
      const auth = await authResolve(request, env);
      if (!auth.ok) return new Response('Unauthorized', { status: 401 });
      // Master can write any user via ?user=; user-tokens write their own slot.
      let user = auth.user || DEFAULT_USER;
      const qUser = _sanitizeUser(url.searchParams.get('user') || '');
      if (qUser && auth.viaMaster) user = qUser;
      const body = await request.text();
      try { JSON.parse(body); } catch { return new Response('Invalid JSON', { status: 400 }); }
      await env.SIGNALS.put(_signalsKey(user), body);
      return new Response('OK', { headers: ch });
    }

    // ── /pending_bets + /cancel_bet + /cancel_requests (per-user via auth) ──
    if (path === '/pending_bets' || path.startsWith('/pending_bets/') ||
        path === '/cancel_bet' || path === '/cancel_requests') {
      const auth = await authResolve(request, env);
      if (!auth.ok) {
        return new Response('Unauthorized', { status: 401, headers: ch });
      }
      let pUser = auth.user || DEFAULT_USER;
      const qUser = _sanitizeUser(url.searchParams.get('user') || '');
      if (qUser && auth.viaMaster) pUser = qUser;

      if (request.method === 'GET' && path === '/pending_bets') {
        const arr = await readPending(env, pUser);
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

        const arr = await readPending(env, pUser);
        // Soft duplicate guard: same match+market+odds within 60s
        const recent = arr.find(b =>
          b.match === entry.match && b.market === entry.market &&
          Math.abs(b.odds - entry.odds) < 0.001 &&
          (Date.now() - new Date(b.placed_at).getTime()) < 60_000
        );
        if (recent) return jr({ ok: true, id: recent.id, duplicate: true });

        arr.push(entry);
        await writePending(env, arr, pUser);
        return jr({ ok: true, id });
      }

      if (request.method === 'DELETE' && path.startsWith('/pending_bets/')) {
        const id = path.slice('/pending_bets/'.length);
        const arr = await readPending(env, pUser);
        const next = arr.filter(b => b.id !== id);
        await writePending(env, next, pUser);
        return jr({ ok: true, removed: arr.length - next.length });
      }

      // H3: Cancel a placed bet (remove from pending KV + queue for ledger cancel)
      if (request.method === 'POST' && path === '/cancel_bet') {
        let body;
        try { body = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }
        const home = (body.home || '').toString().trim();
        const away = (body.away || '').toString().trim();
        const market = (body.market || '').toString().trim();
        if (!home || !away || !market) return jr({ error: 'home, away, market required' }, 400);

        // Remove from pending_bets if still there
        const pending = await readPending(env, pUser);
        const matchStr = `${home} vs ${away}`;
        const pendingFiltered = pending.filter(b => !(b.match === matchStr && b.market === market));
        if (pendingFiltered.length < pending.length) {
          await writePending(env, pendingFiltered, pUser);
        }

        // Queue cancel request for Python ledger (consume_pending_bets picks this up)
        const cancelReqs = await readCancelRequests(env, pUser);
        const alreadyQueued = cancelReqs.some(r => r.home === home && r.away === away && r.market === market);
        if (!alreadyQueued) {
          cancelReqs.push({ home, away, market, requested_at: new Date().toISOString() });
          await writeCancelRequests(env, cancelReqs, pUser);
        }
        return jr({ ok: true });
      }

      // H3: Cancel-Request-Queue read + clear (for Python consume_pending_bets)
      if (path === '/cancel_requests') {
        if (request.method === 'GET') {
          return jr({ requests: await readCancelRequests(env, pUser) });
        }
        if (request.method === 'DELETE') {
          await writeCancelRequests(env, [], pUser);
          return jr({ ok: true });
        }
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

    // ── D6 — POST /invite (Master-Token: erzeugt unbound Invite-Token) ──
    //   Body: optional {note}. Liefert {invite_token, invite_url_hint}.
    if (request.method === 'POST' && path === '/invite') {
      const auth = await authResolve(request, env);
      if (!auth.ok || !auth.viaMaster) return new Response('Master only', { status: 401, headers: ch });
      let body = {};
      try { body = await request.json(); } catch {}
      const invites = await readInvites(env);
      const invite = _randomToken();
      invites[invite] = {
        created_at: new Date().toISOString(),
        note: typeof body.note === 'string' ? body.note.slice(0, 200) : '',
        used_by: null,
      };
      await writeInvites(env, invites);
      return jr({ ok: true, invite_token: invite });
    }

    // ── D6 — POST /register (no-auth, Body: {invite, user}) ──
    //   Konsumiert einen Invite-Token, sanitisiert Username, prüft Eindeutigkeit,
    //   legt User-Slot mit frischem Per-User-Token an. Username einmalig.
    if (request.method === 'POST' && path === '/register') {
      let body = {};
      try { body = await request.json(); } catch { return jr({ error: 'invalid json' }, 400); }
      const invite = String(body.invite || '').trim();
      const user = _sanitizeUser(body.user || '');
      if (!invite) return jr({ error: 'invite required' }, 400);
      if (!user || user.length < 3) return jr({ error: 'username must be ≥3 chars (a-z, 0-9, _, -)' }, 400);
      if (user === DEFAULT_USER) return jr({ error: 'username reserved' }, 400);
      const invites = await readInvites(env);
      const inv = invites[invite];
      if (!inv) return jr({ error: 'unknown invite token' }, 400);
      if (inv.used_by) return jr({ error: 'invite already used' }, 400);
      const ut = await readUserTokens(env);
      if (ut[user]) return jr({ error: 'username taken' }, 400);
      const token = _randomToken();
      ut[user] = { active: token, previous: null, rotated_at: new Date().toISOString() };
      inv.used_by = user;
      inv.used_at = new Date().toISOString();
      await writeUserTokens(env, ut);
      await writeInvites(env, invites);
      return jr({ ok: true, user, token });
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
