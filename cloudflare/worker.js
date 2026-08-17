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

import {
  validateBetBodyBasic,
  validateSignalActionability,
  validateBankrollCap,
  validateActiveBets,
  validateAuthStateFreshness,
  validateOddsMatchCanonical,
  resolveCanonicalSignal,
  validateCanonicalIdentity,
} from './contract.js';

// P0-A (item K): supported sports for new bets
const SUPPORTED_SPORTS = new Set(['football', 'tennis', 'basketball']);

/**
 * Blocker-3: Normalize model_prob from published PWA/API percent units to
 * ledger probability-fraction units.
 *
 * Published schema (web_dashboard._signal_to_dict): model_prob = round(s.model_prob * 100, 1)
 *   e.g. 0.52 → 52.0 (percent)
 * Ledger schema (consume_pending_bets, calibration, Brier/ECE/LogLoss): 0 < model_prob < 1
 *   e.g. 0.52 (fraction)
 *
 * Conversion: fraction = pct / 100. Performed ONCE here at the Worker boundary.
 * Returns null for invalid values — never stores garbage in the ledger.
 */
function normalizeModelProbPct(pct) {
  if (pct == null) return null;
  const v = Number(pct);
  if (!Number.isFinite(v)) return null;
  // Strict (0, 100) — endpoints are not calibration-eligible:
  // model_prob=0.0 or model_prob=1.0 in the ledger breaks Brier/ECE/LogLoss.
  if (v <= 0) return null;    // 0 and negative are invalid
  if (v >= 100) return null;  // 100 and above are invalid
  return v / 100;
}

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
const OU_RE = /^o\/u(?:_(?:sets|games)_)?\d+(?:\.\d+)?_(over|under)$/;
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

// P0C-001 — Public product serialization boundary.
// Allowlist-based: only approved top-level keys are included in the public
// response. Private financial/identity state (bankroll_state, open_bets,
// settled_bets, history, portfolio, wm_stats, meta.user/default_user, …)
// is structurally excluded. Applied to every GET /signals.json response.
const _PUBLIC_TOP_LEVEL_KEYS = new Set([
  'updated', 'build_info', 'schedule', 'all_odds', 'model_tips', 'model_evals',
  'football', 'tennis', 'top_elo', 'wm_results', 'odds_history', 'health',
]);
function _publicMeta(meta) {
  if (!meta || typeof meta !== 'object') return {};
  return { stale_odds: Boolean(meta.stale_odds) };
}
function _publicTennisStats(ts) {
  if (!ts || typeof ts !== 'object') return {};
  const out = {};
  for (const k of ['updated', 'active_tournaments', 'live_gate_status']) {
    if (k in ts) out[k] = ts[k];
  }
  return out;
}
export function serializePublicProduct(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return {};
  const pub = {};
  for (const key of _PUBLIC_TOP_LEVEL_KEYS) {
    if (key in snapshot) pub[key] = snapshot[key];
  }
  if ('meta' in snapshot) pub.meta = _publicMeta(snapshot.meta);
  if ('tennis_stats' in snapshot) pub.tennis_stats = _publicTennisStats(snapshot.tennis_stats);
  return pub;
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

// P0-A: Read and parse the full signals_json KV snapshot.
// Returns the parsed object or null if unavailable/unparseable.
async function readSignalsJson(env, user = DEFAULT_USER) {
  try {
    const raw = await env.SIGNALS.get(_signalsKey(user));
    if (!raw) return null;
    const data = JSON.parse(raw);
    return (data && typeof data === 'object') ? data : null;
  } catch {
    return null;
  }
}

// P0-A: Extract authoritative risk state from parsed signals JSON.
// Returns { bankroll, openBetsCount, publishedAt } — null fields mean fail closed.
// publishedAt comes from bankroll_state.published_at (set by Python backend).
function extractAuthState(data) {
  if (!data) return { bankroll: null, openBetsCount: null, publishedAt: null };
  const bs = data.bankroll_state;
  let bankroll = null;
  if (bs && Number.isFinite(Number(bs.free)) && Number.isFinite(Number(bs.staked))) {
    bankroll = Number(bs.free) + Number(bs.staked);
  }
  const openBetsArr = data.open_bets;
  const openBetsCount = Array.isArray(openBetsArr) ? openBetsArr.length : null;
  const publishedAt = (bs && typeof bs.published_at === 'string') ? bs.published_at : null;
  return { bankroll, openBetsCount, publishedAt };
}

// P0-A: Convenience wrapper — reads KV and extracts auth state in one call.
// Retained for backward-compat with cron helpers that only need auth state.
async function readAuthoritativeState(env, user = DEFAULT_USER) {
  return extractAuthState(await readSignalsJson(env, user));
}

/**
 * Blocker-5: Core pending-bet POST orchestration, extracted for testability.
 *
 * Accepts already-parsed input (no KV reads here) so Node.js tests can call
 * this with deterministic mock state and verify ACTUAL production logic.
 * The fetch handler calls this after reading from KV.
 *
 * @param {object} body          - Parsed request JSON body
 * @param {object} opts
 * @param {object|null} opts.signalsJson  - Parsed signals_json KV value (or null)
 * @param {Array}  opts.pendingArr        - Current pending_bets array from KV
 * @param {number} [opts.nowMs]           - Override for Date.now() (tests only)
 * @param {Function} [opts.genId]         - UUID generator override (tests only)
 * @returns {{ status: number, json: object, entry?: object }}
 */
export function orchestratePendingBetPost(body, { signalsJson, pendingArr, nowMs, genId } = {}) {
  const _now = nowMs != null ? nowMs : Date.now();
  const _id = genId != null ? genId : () => 'test-id-' + Math.random().toString(36).slice(2);

  const match  = (body.match  || '').toString().trim();
  const market = (body.market || '').toString().trim();
  const odds   = Number(body.odds);

  if (!match || !match.includes(' vs '))
    return { status: 400, json: { error: 'match must be "Home vs Away"' } };
  if (!isValidMarket(market))
    return { status: 400, json: { error: 'unknown market: ' + market } };
  if (!Number.isFinite(odds) || odds < 1.01 || odds > 100)
    return { status: 400, json: { error: 'odds out of range (1.01–100)' } };

  const bodyValidation = validateBetBodyBasic(body);
  if (!bodyValidation.ok)
    return { status: bodyValidation.status, json: { error: bodyValidation.error } };

  const { source } = bodyValidation;
  const signalId = String(body.signal_id || '').trim();
  const stake    = Number(body.stake_eur);

  const { bankroll: authBankroll, openBetsCount: authOpenBets, publishedAt } =
    extractAuthState(signalsJson || null);

  const freshnessResult = validateAuthStateFreshness(publishedAt, _now);
  if (!freshnessResult.ok)
    return { status: 503, json: { error: freshnessResult.reason } };

  const capResult = validateBankrollCap(authBankroll, stake);
  if (!capResult.ok)
    return { status: authBankroll === null ? 503 : 400, json: { error: capResult.error } };

  const pending = Array.isArray(pendingArr) ? pendingArr : [];
  const activeBetsResult = validateActiveBets(authOpenBets, pending.length);
  if (!activeBetsResult.ok)
    return { status: authOpenBets === null ? 503 : 400, json: { error: activeBetsResult.error } };

  let canonicalSigForEntry = null;
  let canonicalNormalizedProb = null;
  if (source === 'value') {
    const canonicalSig = resolveCanonicalSignal(signalsJson, signalId);
    if (!canonicalSig)
      return { status: 400, json: { error: `signal_id '${signalId}' not found in published canonical signals — REJECT` } };

    const sigResult = validateSignalActionability(canonicalSig, _now);
    if (!sigResult.ok)
      return { status: 400, json: { error: `canonical signal not actionable: ${sigResult.reason}` } };

    const identityResult = validateCanonicalIdentity(canonicalSig, body);
    if (!identityResult.ok)
      return { status: 400, json: { error: `identity mismatch — REJECT: ${identityResult.reason}` } };

    const oddsResult = validateOddsMatchCanonical(odds, canonicalSig.current_odds);
    if (!oddsResult.ok)
      return { status: 400, json: { error: oddsResult.reason } };

    // Blocker-3 (V5): source=value must have valid canonical model_prob for calibration.
    // normalizeModelProbPct returns null for null/negative/>100 values.
    const normProb = normalizeModelProbPct(canonicalSig.model_prob);
    if (normProb === null) {
      return {
        status: 400,
        json: { error: `canonical model_prob missing or out of range (got ${canonicalSig.model_prob}) — source=value requires valid probability evidence (0–100 percent)` },
      };
    }
    canonicalNormalizedProb = normProb;
    canonicalSigForEntry = canonicalSig;
  }

  if (source === 'manual') {
    const manualSport = String(body.sport || '').trim();
    if (!manualSport || !SUPPORTED_SPORTS.has(manualSport))
      return { status: 400, json: { error: `manual bets require sport in [${[...SUPPORTED_SPORTS].join(', ')}], got '${manualSport}'` } };
  }

  const id = _id();
  let entry;
  if (source === 'value' && canonicalSigForEntry) {
    const cs = canonicalSigForEntry;
    entry = {
      id,
      match:        String(cs.match        || match).trim(),
      market:       String(cs.market       || market).trim(),
      odds,
      stake_eur: stake,
      ev_pct:     Number.isFinite(Number(cs.current_ev_pct)) ? Number(cs.current_ev_pct) : null,
      model_prob: canonicalNormalizedProb,  // validated above — never null for source=value
      confidence: typeof cs.confidence === 'string' ? cs.confidence : (typeof body.confidence === 'string' ? body.confidence : ''),
      kickoff:    typeof cs.kickoff === 'string' ? cs.kickoff : (typeof cs.scheduled_start_current === 'string' ? cs.scheduled_start_current : ''),
      sport:      String(cs.sport        || '').trim(),
      source,
      signal_id:   signalId,
      fixture_key: String(cs.fixture_key  || '').trim(),
      league:      String(cs.league       || '').trim(),
      odds_ts:     String(cs.odds_ts      || ''),
      selection:   String(cs.selection    || '').trim(),
      event_status: String(cs.event_status || '').trim(),
      bankroll_hint: Number.isFinite(Number(body.bankroll_hint)) ? Number(body.bankroll_hint) : null,
      origin: 'pwa',
      placed_at: new Date(_now).toISOString(),
    };
  } else {
    entry = {
      id,
      match, market, odds, stake_eur: stake,
      ev_pct:     Number.isFinite(Number(body.ev_pct))     ? Number(body.ev_pct)     : null,
      model_prob: Number.isFinite(Number(body.model_prob)) ? Number(body.model_prob) : null,
      confidence: typeof body.confidence  === 'string' ? body.confidence  : '',
      kickoff:    typeof body.kickoff     === 'string' ? body.kickoff     : '',
      sport:      typeof body.sport       === 'string' ? body.sport.trim() : '',
      source, signal_id: signalId,
      fixture_key: typeof body.fixture_key === 'string' ? body.fixture_key.trim() : '',
      league:      typeof body.league      === 'string' ? body.league.trim() : '',
      odds_ts:     typeof body.odds_ts     === 'string' ? body.odds_ts : '',
      selection:   typeof body.selection   === 'string' ? body.selection.trim() : '',
      bankroll_hint: Number.isFinite(Number(body.bankroll_hint)) ? Number(body.bankroll_hint) : null,
      origin: 'pwa',
      placed_at: new Date(_now).toISOString(),
    };
  }

  // Soft duplicate guard
  const recent = pending.find(b =>
    b.match === entry.match && b.market === entry.market &&
    Math.abs(b.odds - entry.odds) < 0.001 &&
    (_now - new Date(b.placed_at).getTime()) < 60_000
  );
  if (recent) return { status: 200, json: { ok: true, id: recent.id, duplicate: true } };

  return { status: 200, json: { ok: true, id: entry.id }, entry };
}

// FND-20260814-003 (P0A-012): one KV key per cancel intent — truly atomic per-item delete.
// Prefix scheme: "cancel_intent:{user}:" for every user — explicit namespace prevents default-user
// prefix "cancel_intent:" from matching other users' "cancel_intent:{other}:..." keys.
// DELETE /cancel_requests/{id} deletes one key without reading or rewriting any other key.
function _cancelIntentPrefix(user) {
  return `cancel_intent:${user || DEFAULT_USER}:`;
}
function _cancelIntentKey(user, id) {
  return `${_cancelIntentPrefix(user)}${id}`;
}
async function readCancelRequests(env, user = DEFAULT_USER) {
  const prefix = _cancelIntentPrefix(user);
  const listed = await env.SIGNALS.list({ prefix });
  if (!listed.keys.length) return [];
  const vals = await Promise.all(listed.keys.map(k => env.SIGNALS.get(k.name)));
  return vals.flatMap(v => {
    if (!v) return [];
    try { const p = JSON.parse(v); return (p && typeof p === 'object') ? [p] : []; } catch { return []; }
  });
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

// ── F5-C/D: Worker Cron Helpers ───────────────────────────────
// GH_REPO can be overridden via wrangler secret/var; falls back to hardcoded default.
const _GH_REPO_DEFAULT = 'Philip3006/sportsbrain';
// Resolved per-request in handlers that need it: env.GH_REPO || _GH_REPO_DEFAULT
const _WORKFLOW_MAP = {
  daily_scan:    'daily_scan.yml',
  auto_retrain:  'auto_retrain.yml',
  closing_odds:  'closing_odds.yml',
  settle:        'settle.yml',
  prematch_scan: 'prematch_scan.yml',
  tennis_scan:   'tennis_scan.yml',
  tennis_settle: 'tennis_settle.yml',
};
const _HEALER_SKIP = new Set(['consume_pending_bets', 'aggregate_health']);
const _HEALER_COOLDOWN_MS = 10 * 60 * 1000;

async function _ghWorkflowDispatch(token, workflow, repo = _GH_REPO_DEFAULT) {
  return fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `token ${token}`,
        'Content-Type': 'application/json',
        Accept: 'application/vnd.github+json',
        'User-Agent': 'sportsbrain-worker',
      },
      body: JSON.stringify({ ref: 'main' }),
    },
  );
}

async function _ghRepositoryDispatch(token, eventType, repo = _GH_REPO_DEFAULT) {
  return fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: 'POST',
    headers: {
      Authorization: `token ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/vnd.github+json',
      'User-Agent': 'sportsbrain-worker',
    },
    body: JSON.stringify({ event_type: eventType }),
  });
}

// F5-C: Trigger consume_pending_bets only when KV has pending bets.
async function _cronConsumeCheck(env) {
  const token = env.GH_TOKEN;
  if (!token) return;
  const users = [DEFAULT_USER, ...Object.keys(await readUserTokens(env))];
  let hasPending = false;
  for (const u of new Set(users)) {
    if ((await readPending(env, u)).length > 0) { hasPending = true; break; }
    if ((await readCancelRequests(env, u)).length > 0) { hasPending = true; break; }
  }
  if (!hasPending) return;
  await _ghRepositoryDispatch(token, 'consume_pending_bets', env.GH_REPO || _GH_REPO_DEFAULT);
}

// F5-D: Read health from signals KV, trigger failed workflows with 10-min cooldown.
async function _cronHealerCheck(env) {
  const token = env.GH_TOKEN;
  if (!token) return;
  const raw = await env.SIGNALS.get(_signalsKey(DEFAULT_USER));
  if (!raw) return;
  let health;
  try { health = JSON.parse(raw).health; } catch { return; }
  if (!health || health.overall === 'ok') return;

  const now = Date.now();
  const cdRaw = await env.SIGNALS.get('healer_cooldowns');
  const cooldowns = cdRaw ? JSON.parse(cdRaw) : {};
  let updated = false;

  for (const job of (health.jobs || [])) {
    if (job.status === 'ok') continue;
    if (_HEALER_SKIP.has(job.job)) continue;
    const wf = _WORKFLOW_MAP[job.job];
    if (!wf) continue;
    if (now - (cooldowns[job.job] || 0) < _HEALER_COOLDOWN_MS) continue;
    await _ghWorkflowDispatch(token, wf, env.GH_REPO || _GH_REPO_DEFAULT);
    cooldowns[job.job] = now;
    updated = true;
  }
  if (updated) await env.SIGNALS.put('healer_cooldowns', JSON.stringify(cooldowns));
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

    // ── /signals.json (public read — always returns public-product data only) ──
    // P0C-001: The public GET endpoint always serves the public-serialized
    // product payload. Private financial/identity state is excluded via
    // serializePublicProduct(). No DEFAULT_USER fallback for unauthenticated
    // public access — unauthenticated does not mean "serve Philip's private state".
    // The full private KV snapshot is preserved for Worker POST /pending-bet
    // validation (bankroll_state, open_bets), which reads directly from KV.
    if (request.method === 'GET' && (path === '/signals.json' || path === '/')) {
      const raw = await env.SIGNALS.get(_signalsKey(DEFAULT_USER));
      if (!raw) return jr({ error: 'no data yet' }, 404);
      let parsed;
      try { parsed = JSON.parse(raw); } catch { return jr({ error: 'malformed signals data' }, 500); }
      const publicPayload = serializePublicProduct(parsed);
      return new Response(JSON.stringify(publicPayload), {
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

      // P0C-001: ?merge_health=1 — merge only the health key into the existing
      // KV object. Used by aggregate_health to inject health monitoring data
      // without fetching (and thus discarding private fields from) the full KV
      // snapshot via the public GET endpoint. Preserves bankroll_state and
      // open_bets that Worker POST /pending-bet validation depends on.
      if (url.searchParams.get('merge_health') === '1') {
        const body = await request.text();
        let incoming;
        try { incoming = JSON.parse(body); } catch { return new Response('Invalid JSON', { status: 400 }); }
        if (!incoming || typeof incoming !== 'object' || !incoming.health) {
          return new Response('merge_health=1 requires {"health": {...}}', { status: 400 });
        }
        const existing = await env.SIGNALS.get(_signalsKey(user));
        let current = {};
        try { current = JSON.parse(existing || '{}'); } catch { current = {}; }
        current.health = incoming.health;
        await env.SIGNALS.put(_signalsKey(user), JSON.stringify(current));
        return new Response('OK', { headers: ch });
      }

      const body = await request.text();
      let parsed;
      try { parsed = JSON.parse(body); } catch { return new Response('Invalid JSON', { status: 400 }); }

      // Dummy-Overwrite-Guard (2026-08-01 Incident): Reject payloads with
      // suspiciously little data — real scans have schedule OR ≥3 total signals.
      // Bypass via ?force=1 (Master only) for legit low-signal scans.
      const force = url.searchParams.get('force') === '1' && auth.viaMaster;
      if (!force) {
        const nSched = Array.isArray(parsed.schedule) ? parsed.schedule.length : 0;
        const nTennis = Array.isArray(parsed.tennis) ? parsed.tennis.length : 0;
        const nFoot = Array.isArray(parsed.football) ? parsed.football.length : 0;
        if (nSched === 0 && (nTennis + nFoot) < 3) {
          console.log(`[signals-guard] REJECT thin payload user=${user} sched=${nSched} tennis=${nTennis} football=${nFoot} ua=${request.headers.get('user-agent') || '?'} cf-ip=${request.headers.get('cf-connecting-ip') || '?'}`);
          return new Response('Payload too thin — use ?force=1 to bypass', { status: 400 });
        }
      }

      // Structural POST log (visible via `wrangler tail`) — helps diagnose future incidents.
      console.log(`[signals-write] user=${user} bytes=${body.length} tennis=${(parsed.tennis||[]).length} football=${(parsed.football||[]).length} sched=${(parsed.schedule||[]).length} ua=${request.headers.get('user-agent') || '?'} cf-ip=${request.headers.get('cf-connecting-ip') || '?'} viaMaster=${auth.viaMaster}`);

      await env.SIGNALS.put(_signalsKey(user), body);
      return new Response('OK', { headers: ch });
    }

    // ── /pending_bets + /cancel_bet + /cancel_requests (per-user via auth) ──
    if (path === '/pending_bets' || path.startsWith('/pending_bets/') ||
        path === '/cancel_bet' || path === '/cancel_requests' ||
        path.startsWith('/cancel_requests/')) {
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

        // P0-A: read signals JSON ONCE — used for auth state AND canonical signal lookup
        const signalsData = await readSignalsJson(env, pUser);
        const arr = await readPending(env, pUser);

        // Blocker-5: orchestratePendingBetPost contains ALL validation + entry construction.
        // This is the SAME function tested in worker.test.mjs — no separate simulation.
        const result = orchestratePendingBetPost(body, { signalsJson: signalsData, pendingArr: arr });
        if (result.status !== 200 || !result.entry) {
          return jr(result.json, result.status);
        }
        if (result.json.duplicate) return jr(result.json);

        arr.push(result.entry);
        await writePending(env, arr, pUser);
        return jr(result.json);
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
          // FND-20260814-003 (P0A-012): write one KV key per intent — no array RMW, no concurrent loss.
          const cid = _randomToken().slice(0, 16);
          const intent = { id: cid, home, away, market, requested_at: new Date().toISOString() };
          await env.SIGNALS.put(_cancelIntentKey(pUser, cid), JSON.stringify(intent));
        }
        return jr({ ok: true });
      }

      // H3: Cancel-Request-Queue read + per-item delete (for Python consume_pending_bets)
      if (path === '/cancel_requests') {
        if (request.method === 'GET') {
          return jr({ requests: await readCancelRequests(env, pUser) });
        }
        if (request.method === 'DELETE') {
          // FND-20260814-003 (P0A-012): clear-all disabled — prevents accidental erasure of
          // concurrent intents. Use DELETE /cancel_requests/{id} for per-item ACK.
          return jr({ error: 'clear-all cancel is disabled; use DELETE /cancel_requests/{id}' }, 410);
        }
      }

      // FND-20260814-003 (P0A-012): atomic per-item ACK — deletes exactly one KV key.
      // No read-modify-write; concurrent cancel intents are unaffected.
      if (request.method === 'DELETE' && path.startsWith('/cancel_requests/')) {
        const cancelId = path.slice('/cancel_requests/'.length);
        if (!cancelId) return jr({ error: 'cancel request id required' }, 400);
        await env.SIGNALS.delete(_cancelIntentKey(pUser, cancelId));
        return jr({ ok: true });
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

  // F5-C/D: Cron triggers (configured in wrangler.toml [triggers].crons)
  // Requires GH_TOKEN Worker secret: wrangler secret put GH_TOKEN
  async scheduled(event, env) {
    if (event.cron === '*/5 * * * *') await _cronConsumeCheck(env);
    else if (event.cron === '*/30 * * * *') {
      await _cronHealerCheck(env);
      // Blocker-4: unconditional risk-state heartbeat every 30 min.
      // consume_pending_bets.py always refreshes bankroll_state.published_at even
      // when added == 0, ensuring AUTH_STATE_MAX_AGE_MS (90 min) is never exceeded
      // regardless of tennis_scan cadence (which has gaps up to 3 hours).
      const token = env.GH_TOKEN;
      if (token) {
        await _ghRepositoryDispatch(token, 'consume_pending_bets', env.GH_REPO || _GH_REPO_DEFAULT);
      }
    }
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
