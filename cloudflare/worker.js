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
  const isMasterToken =
    (env.API_TOKEN && token === env.API_TOKEN) ||
    (env.API_TOKEN_NEXT && token === env.API_TOKEN_NEXT);
  if (isMasterToken) {
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
//
// P0C-002 owner-routing note:
//   The DEFAULT_USER constant is used ONLY for:
//     (a) legacy physical KV-key mapping for the philip owner (signals_json,
//         pending_bets, cancel_intent:philip:*),
//     (b) internal cron helpers that need a fixed baseline key.
//   It is EXPLICITLY NOT a fallback for authorization. The private /me endpoint
//   (P0C-002) never falls back to DEFAULT_USER when the authenticated user has
//   no owner: it either returns exact-owner state for the authenticated user,
//   or fails 404. See serveMe() below.
const DEFAULT_USER = 'philip';

// OPS-LOCALHEALTH-PUBLISH-001: health has two explicit execution authorities.
// This Worker-side policy is the enforcement point; clients cannot grant
// themselves authority by placing a job name in an otherwise valid payload.
const _LOCAL_HEALTH_JOBS = new Set([
  'aggregate_health', 'auto_retrain', 'closing_odds', 'daily_scan',
  'live_score_push', 'odds_refresh', 'prematch_scan', 'settle',
]);
const _CLOUD_HEALTH_JOBS = new Set([
  'bundesliga2_closing_odds', 'bundesliga2_live_push', 'bundesliga2_retrain',
  'bundesliga2_scan', 'bundesliga2_settle', 'consume_pending_bets',
  'tennis_closing_odds', 'tennis_retrain', 'tennis_scan', 'tennis_settle',
  'signals_data_fresh', 'live_scores_fresh',
]);
const _HEALTH_MERGE_ATTEMPTS = 3;

function _healthKey(user) {
  return (user && user !== DEFAULT_USER) ? `health_v1_${user}` : 'health_v1';
}

function _healthAuthorityAllows(job, authority) {
  return authority === 'local'
    ? _LOCAL_HEALTH_JOBS.has(job)
    : authority === 'cloud' && _CLOUD_HEALTH_JOBS.has(job);
}

function _healthTimestamp(value) {
  if (typeof value !== 'string' || value.length === 0) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function _healthOverall(jobs) {
  const statuses = new Set(jobs.map((job) => job.status));
  if (statuses.has('error')) return 'down';
  if (statuses.has('stale') || statuses.has('degraded')) return 'degraded';
  return 'ok';
}

function _parseHealth(raw) {
  if (!raw) return null;
  try {
    const health = JSON.parse(raw);
    return health && typeof health === 'object' && Array.isArray(health.jobs) ? health : null;
  } catch {
    return null;
  }
}

async function _readCanonicalHealthState(env, user) {
  const dedicatedRaw = await env.SIGNALS.get(_healthKey(user));
  const dedicated = _parseHealth(dedicatedRaw);
  if (dedicated) return { health: dedicated, bootstrapAllowed: false };

  // One-way legacy fallback: first authority-aware update seeds a separate
  // health record from the existing signals payload without rewriting it.
  const rawSignals = await env.SIGNALS.get(_signalsKey(user));
  try {
    const signals = JSON.parse(rawSignals || '{}');
    const legacy = signals && typeof signals === 'object' && Array.isArray(signals.health?.jobs)
      ? signals.health
      : null;
    return { health: legacy, bootstrapAllowed: !dedicatedRaw && !legacy };
  } catch {
    return { health: null, bootstrapAllowed: !dedicatedRaw };
  }
}

async function _readCanonicalHealth(env, user) {
  return (await _readCanonicalHealthState(env, user)).health;
}

function _bootstrapCloudHealth(incoming, authority) {
  if (authority !== 'cloud') return { error: 'canonical_health_baseline_missing' };
  if (!incoming || typeof incoming !== 'object' || Array.isArray(incoming)) {
    return { error: 'invalid_health_bootstrap' };
  }
  if (_healthTimestamp(incoming.generated_at) === null) {
    return { error: 'invalid_generated_at' };
  }
  if (!Array.isArray(incoming.jobs) || incoming.jobs.length === 0) {
    return { error: 'invalid_health_bootstrap' };
  }

  const jobs = new Map();
  for (const job of incoming.jobs) {
    if (!job || typeof job.job !== 'string' || !_CLOUD_HEALTH_JOBS.has(job.job)) {
      return { error: 'unclassified_or_foreign_health_job' };
    }
    if (jobs.has(job.job)) return { error: 'duplicate_health_job' };
    if (!['ok', 'error', 'stale', 'degraded'].includes(job.status)) {
      return { error: 'invalid_health_bootstrap' };
    }
    if (job.last_run_at !== null && _healthTimestamp(job.last_run_at) === null) {
      return { error: 'invalid_job_timestamp' };
    }
    jobs.set(job.job, job);
  }
  if (jobs.size !== _CLOUD_HEALTH_JOBS.size || [..._CLOUD_HEALTH_JOBS].some((job) => !jobs.has(job))) {
    return { error: 'incomplete_cloud_health_bootstrap' };
  }

  const canonicalJobs = [...jobs.values()].sort((a, b) => a.job.localeCompare(b.job));
  return {
    health: {
      ...incoming,
      jobs: canonicalJobs,
      overall: _healthOverall(canonicalJobs),
    },
  };
}

function _mergeHealthPartial(existing, incoming, authority, bootstrapAllowed = false) {
  if (!existing || !Array.isArray(existing.jobs) || !incoming || !Array.isArray(incoming.jobs)) {
    return bootstrapAllowed
      ? _bootstrapCloudHealth(incoming, authority)
      : { error: 'canonical_health_baseline_missing' };
  }
  if (_healthTimestamp(incoming.generated_at) === null) {
    return { error: 'invalid_generated_at' };
  }

  const next = new Map();
  for (const job of existing.jobs) {
    if (job && typeof job.job === 'string') next.set(job.job, job);
  }
  for (const job of incoming.jobs) {
    if (!job || typeof job.job !== 'string' || !_healthAuthorityAllows(job.job, authority)) {
      return { error: 'unclassified_or_foreign_health_job' };
    }
    const previous = next.get(job.job);
    const previousAt = previous ? _healthTimestamp(previous.last_run_at) : null;
    if (job.last_run_at === null) {
      // "No snapshot yet" is unknown evidence, not a fresh timestamp. It may
      // establish a missing job as error, but never replace observed evidence.
      if (!previous) next.set(job.job, job);
      continue;
    }
    const incomingAt = _healthTimestamp(job.last_run_at);
    if (incomingAt === null) return { error: 'invalid_job_timestamp' };
    if (previousAt !== null && incomingAt < previousAt) {
      return { error: 'stale_health_update' };
    }
    // Equal timestamps are a no-op. They must not change canonical status.
    if (previousAt === null || incomingAt > previousAt) next.set(job.job, job);
  }

  const existingGeneratedAt = _healthTimestamp(existing.generated_at);
  const incomingGeneratedAt = _healthTimestamp(incoming.generated_at);
  const jobs = [...next.values()].sort((a, b) => a.job.localeCompare(b.job));
  return {
    health: {
      ...existing,
      jobs,
      overall: _healthOverall(jobs),
      generated_at: existingGeneratedAt !== null && existingGeneratedAt > incomingGeneratedAt
        ? existing.generated_at
        : incoming.generated_at,
    },
  };
}

function _healthUpdatePersisted(current, incoming) {
  if (!current || !Array.isArray(current.jobs)) return false;
  const persisted = new Map(current.jobs.map((job) => [job.job, job]));
  return incoming.jobs.every((job) => {
    const currentJob = persisted.get(job.job);
    const incomingAt = _healthTimestamp(job.last_run_at);
    const currentAt = currentJob ? _healthTimestamp(currentJob.last_run_at) : null;
    if (job.last_run_at === null) return currentJob !== undefined;
    return incomingAt !== null && currentAt !== null && currentAt >= incomingAt;
  });
}

async function _mergeHealthWithRetry(env, user, incoming, authority) {
  // KV has no compare-and-swap. Health is isolated from financial state, and
  // this read-after-write loop detects most concurrent lost updates. A Durable
  // Object is required if contention needs a strict serializable guarantee.
  for (let attempt = 0; attempt < _HEALTH_MERGE_ATTEMPTS; attempt++) {
    const state = await _readCanonicalHealthState(env, user);
    const merged = _mergeHealthPartial(state.health, incoming, authority, state.bootstrapAllowed);
    if (merged.error) return merged;
    await env.SIGNALS.put(_healthKey(user), JSON.stringify(merged.health));
    if (_healthUpdatePersisted(await _readCanonicalHealth(env, user), incoming)) {
      return merged;
    }
  }
  return { error: 'health_merge_race_retry_exhausted' };
}

function _pendingKey(user) {
  return (user && user !== DEFAULT_USER) ? `pending_bets_${user}` : 'pending_bets';
}
// P0C-002: exact-owner physical-key routing. Not a fallback. The philip owner
// still lives under the legacy `signals_json` key for backward compatibility;
// every other owner uses `signals_json_<owner>`. Owner is determined by the
// authenticated user identity, never by a missing-user default.
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

// Forbidden private keys — must never appear anywhere in the public payload.
// Mirrors FORBIDDEN_PRIVATE_KEYS in src/notifications/public_serializer.py.
const _FORBIDDEN_PRIVATE_KEYS = new Set([
  'bankroll', 'bankroll_state', 'open_bets', 'pending_bets', 'settled_bets',
  'bet_history', 'ledger', 'ledger_rows', 'stake_history', 'pnl_history',
  'user', 'user_id', 'default_user', 'owner',
  'auth_token', 'token', 'master_token', 'api_token',
  'total_staked', 'total_pnl',
]);

// Recursive private-key assertion — fail-closed guard.
function _assertNoPrivateKeys(obj, path = 'root') {
  if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
    for (const k of Object.keys(obj)) {
      if (_FORBIDDEN_PRIVATE_KEYS.has(k)) {
        throw new Error(
          `P0C-001 PRIVACY VIOLATION — forbidden key '${k}' found at ${path}.${k}`
        );
      }
      _assertNoPrivateKeys(obj[k], `${path}.${k}`);
    }
  } else if (Array.isArray(obj)) {
    for (let i = 0; i < obj.length; i++) {
      _assertNoPrivateKeys(obj[i], `${path}[${i}]`);
    }
  }
}

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
// P0C-002 — Private state allowlist for GET /me.
// Only these top-level keys are returned in the authenticated /me payload.
// Public product fields (schedule, all_odds, model_tips, football, tennis, …)
// are intentionally EXCLUDED — the PWA fetches those from public GET /signals.json
// via the dual-channel architecture.
const _PRIVATE_STATE_KEYS = new Set([
  'bankroll_state',
  'open_bets',
  'settled_bets',
  'history',
  'portfolio',
  'wm_stats',
]);

/**
 * P0C-002: Return authenticated private payload for owner.
 * Mirrors src/notifications/private_serializer.py.
 *
 * @param {object|null} snapshot - Parsed KV snapshot for the owner (or null).
 * @param {string} owner - Authenticated owner identity (never falls back).
 * @returns {object} allowlisted private payload with schema_version + owner.
 */
export function serializePrivateState(snapshot, owner) {
  const result = { schema_version: '1', owner };
  if (!snapshot || typeof snapshot !== 'object') return result;
  for (const key of _PRIVATE_STATE_KEYS) {
    if (key in snapshot) result[key] = snapshot[key];
  }
  return result;
}

export function serializePublicProduct(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return {};
  const pub = {};
  for (const key of _PUBLIC_TOP_LEVEL_KEYS) {
    if (key in snapshot) pub[key] = snapshot[key];
  }
  if ('meta' in snapshot) pub.meta = _publicMeta(snapshot.meta);
  if ('tennis_stats' in snapshot) pub.tennis_stats = _publicTennisStats(snapshot.tennis_stats);
  // Fail-closed: throws if any forbidden key survived inside an approved container.
  _assertNoPrivateKeys(pub);
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
// P0D-003: _cronHealerCheck WORKFLOW_MAP — non-financial, non-model, active workflows only.
// Financial workflows (settle, tennis_settle, bundesliga2_settle, closing_odds) and model
// workflows (auto_retrain) are EXCLUDED — these require human authorization (Tier 3).
// Stale mappings to .disabled workflow files are removed.
// _cronConsumeCheck() is NOT a healer action and is NOT modified here — it is the
// designed P0-A pending-bet dispatcher and must remain fully operational.
const _WORKFLOW_MAP = {
  tennis_scan: 'tennis_scan.yml',
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

// STAB-SCHED-AUTH-001: repository_dispatch with idempotency payload.
// client_payload.scheduled_at: ISO-8601 UTC timestamp of the Cloudflare cron trigger.
// client_payload.scheduler: identifies the scheduler source for observability.
// client_payload.idempotency_key: <event_type>/<scheduled_at_minute> — prevents duplicate
//   financial writes when Cloudflare delivers the same cron slot twice (at-least-once).
//
// Fail-closed dispatch semantics:
//   - 2xx (including 204 No Content): success — no throw.
//   - Permanent errors (401, 403, 404): fail immediately, no retry.
//   - Transient errors (429, 5xx): retry up to 2 times with bounded backoff.
//   - Token and request body are never logged.
const _DISPATCH_PERMANENT_ERRORS = new Set([401, 403, 404]);
const _DISPATCH_RETRY_DELAYS_MS = [500, 1500];

function _sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function _ghRepositoryDispatchWithPayload(token, eventType, payload, repo = _GH_REPO_DEFAULT) {
  const url = `https://api.github.com/repos/${repo}/dispatches`;
  const opts = {
    method: 'POST',
    headers: {
      Authorization: `token ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/vnd.github+json',
      'User-Agent': 'sportsbrain-worker',
    },
    body: JSON.stringify({ event_type: eventType, client_payload: payload }),
  };

  let lastStatus = 0;
  for (let attempt = 0; attempt <= _DISPATCH_RETRY_DELAYS_MS.length; attempt++) {
    const resp = await fetch(url, opts);
    if (resp.ok) return;
    lastStatus = resp.status;
    if (_DISPATCH_PERMANENT_ERRORS.has(lastStatus)) break;
    if (attempt < _DISPATCH_RETRY_DELAYS_MS.length) {
      await _sleep(_DISPATCH_RETRY_DELAYS_MS[attempt]);
    }
  }
  // Log status only — never token or body
  console.error(`[cron] dispatch failed: event=${eventType} status=${lastStatus}`);
  throw new Error(`GH dispatch ${eventType} failed: HTTP ${lastStatus}`);
}

// STAB-SCHED-AUTH-001: BL2 live-push scheduler authority.
// Fires every 2 min during BL2 match windows (Fri/Sat/Sun).
// Dispatches repository_dispatch so the GitHub runner executes the Python push script.
// Idempotency: cancel-in-progress:true in the workflow discards queued duplicates.
// GH_TOKEN missing is a hard configuration failure — the cron was scheduled but cannot dispatch.
async function _cronBl2LivePush(env, scheduledTime) {
  const token = env.GH_TOKEN;
  if (!token) {
    console.error('[cron] GH_TOKEN missing — cannot dispatch sportsbrain_bl2_live_push');
    throw new Error('GH_TOKEN not configured: sportsbrain_bl2_live_push dispatch impossible');
  }
  const scheduledAt = new Date(scheduledTime).toISOString();
  // Minute-granularity key prevents double-write if CF delivers same slot twice.
  const minuteKey = scheduledAt.slice(0, 16); // e.g. "2026-08-30T18:36"
  await _ghRepositoryDispatchWithPayload(token, 'sportsbrain_bl2_live_push', {
    scheduled_at: scheduledAt,
    scheduler: 'cloudflare_cron',
    idempotency_key: `bl2_live_push/${minuteKey}`,
  }, env.GH_REPO || _GH_REPO_DEFAULT);
}

// STAB-SCHED-AUTH-001: Tennis closing-odds scheduler authority (30-min).
// Dispatches alongside the existing healer/consume heartbeat at */30.
// GH_TOKEN missing is a hard configuration failure.
async function _cronTennisClosingOdds(env, scheduledTime) {
  const token = env.GH_TOKEN;
  if (!token) {
    console.error('[cron] GH_TOKEN missing — cannot dispatch sportsbrain_tennis_closing_odds');
    throw new Error('GH_TOKEN not configured: sportsbrain_tennis_closing_odds dispatch impossible');
  }
  const scheduledAt = new Date(scheduledTime).toISOString();
  const minuteKey = scheduledAt.slice(0, 16);
  await _ghRepositoryDispatchWithPayload(token, 'sportsbrain_tennis_closing_odds', {
    scheduled_at: scheduledAt,
    scheduler: 'cloudflare_cron',
    idempotency_key: `tennis_closing_odds/${minuteKey}`,
  }, env.GH_REPO || _GH_REPO_DEFAULT);
}

// STAB-SCHED-AUTH-001: Tennis settle scheduler authority (9 slots/day at :15 past even hours).
// GH_TOKEN missing is a hard configuration failure.
async function _cronTennisSettle(env, scheduledTime) {
  const token = env.GH_TOKEN;
  if (!token) {
    console.error('[cron] GH_TOKEN missing — cannot dispatch sportsbrain_tennis_settle');
    throw new Error('GH_TOKEN not configured: sportsbrain_tennis_settle dispatch impossible');
  }
  const scheduledAt = new Date(scheduledTime).toISOString();
  const minuteKey = scheduledAt.slice(0, 16);
  await _ghRepositoryDispatchWithPayload(token, 'sportsbrain_tennis_settle', {
    scheduled_at: scheduledAt,
    scheduler: 'cloudflare_cron',
    idempotency_key: `tennis_settle/${minuteKey}`,
  }, env.GH_REPO || _GH_REPO_DEFAULT);
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
  const health = await _readCanonicalHealth(env, DEFAULT_USER);
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
      const canonicalHealth = await _readCanonicalHealth(env, DEFAULT_USER);
      if (canonicalHealth) parsed.health = canonicalHealth;
      let publicPayload;
      try {
        publicPayload = serializePublicProduct(parsed);
      } catch {
        // Fail-closed: nested private key found in an approved container.
        return jr({ error: 'privacy_boundary_violation' }, 500);
      }
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
        const authority = url.searchParams.get('health_authority');
        if (authority !== 'local' && authority !== 'cloud') {
          return new Response('merge_health=1 requires health_authority=local|cloud', { status: 400 });
        }
        const result = await _mergeHealthWithRetry(env, user, incoming.health, authority);
        if (result.error) return new Response(result.error, { status: 409 });
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

    // ── P0C-002 — GET /me (authenticated private state, per-user) ──
    // Auth model:
    //   • Requires a Bearer per-user token (from KV `user_tokens`).
    //   • Master token WITHOUT a uniquely authenticated per-user identity → 403.
    //     Rationale: master token can act as any user server-side, but /me
    //     must resolve to exactly ONE owner. Master with no explicit user
    //     mapping is ambiguous — fail closed.
    //   • Owner routing: exact match on auth.user. Philip token → `signals_json`
    //     (legacy physical key). Alice token → `signals_json_alice`. This is
    //     exact-owner routing, NOT a DEFAULT_USER fallback.
    //   • Missing snapshot → 404 (never fall back to Philip state for another user).
    //   • Owner-in-snapshot mismatch → 403 (fail closed on identity divergence).
    // Response is allowlist-serialized private state (no public product fields).
    // Cache-Control: no-store to prevent CDN/browser caching of private data.
    if (request.method === 'GET' && path === '/me') {
      const auth = await authResolve(request, env);
      if (!auth.ok) {
        return new Response('Unauthorized', { status: 401, headers: ch });
      }
      // Master token without a resolved per-user identity → ambiguous → 403.
      // (Master cannot request /me on behalf of some implicit user.)
      if (auth.viaMaster || !auth.user) {
        return new Response('Forbidden — /me requires per-user identity', { status: 403, headers: ch });
      }
      const owner = auth.user;
      const raw = await env.SIGNALS.get(_signalsKey(owner));
      if (!raw) {
        // No snapshot for this owner → explicit unavailable. Never fall back.
        return new Response(
          JSON.stringify({ error: 'private_state_unavailable', owner }),
          {
            status: 404,
            headers: {
              'Content-Type': 'application/json; charset=utf-8',
              'Cache-Control': 'no-store',
              'Pragma': 'no-cache',
              ...ch,
            },
          },
        );
      }
      let parsed;
      try { parsed = JSON.parse(raw); } catch {
        return new Response(
          JSON.stringify({ error: 'malformed_private_state' }),
          { status: 500, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Pragma': 'no-cache', ...ch } },
        );
      }
      // Owner assertion: if the stored snapshot has an owner-identifying field,
      // it must match the authenticated user. Guards against cross-user KV bugs.
      const storedOwner = parsed && parsed.meta
        ? (parsed.meta.user || parsed.meta.default_user || null)
        : null;
      if (storedOwner && String(storedOwner).toLowerCase() !== String(owner).toLowerCase()) {
        return new Response(
          JSON.stringify({ error: 'owner_mismatch' }),
          { status: 403, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Pragma': 'no-cache', ...ch } },
        );
      }
      const payload = serializePrivateState(parsed, owner);
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
          'Pragma': 'no-cache',
          ...ch,
        },
      });
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
    // P0C-002 auth model:
    //   • Non-master token may rotate ONLY its own slot. Any body.user that
    //     differs from auth.user → 403. No implicit 'philip' fallback.
    //   • Master token MAY explicitly target another user via body.user.
    //   • Missing body.user for non-master → target = auth.user (self-rotate).
    if (request.method === 'POST' && path === '/rotate_token') {
      const auth = await authResolve(request, env);
      if (!auth.ok) return new Response('Unauthorized', { status: 401, headers: ch });
      let body = {};
      try { body = await request.json(); } catch {}
      const requested = _sanitizeUser(body.user || '');
      let user;
      if (auth.viaMaster) {
        // Master must specify a target user explicitly. No implicit default.
        if (!requested) {
          return jr({ error: 'master token must specify body.user explicitly' }, 400);
        }
        user = requested;
      } else {
        // Per-user token: may only rotate own slot.
        if (requested && requested !== auth.user) {
          return jr({ error: 'cannot rotate token for another user' }, 403);
        }
        user = auth.user;
      }
      if (!user) return jr({ error: 'user identity required' }, 400);
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
    // P0C-002 auth model:
    //   • Non-master token may inspect ONLY its own slot. ?user= differing
    //     from auth.user → 403.
    //   • Master token may inspect any explicit user.
    //   • No implicit 'philip' fallback.
    if (request.method === 'GET' && path === '/token_status') {
      const auth = await authResolve(request, env);
      if (!auth.ok) return new Response('Unauthorized', { status: 401, headers: ch });
      const requested = _sanitizeUser(url.searchParams.get('user') || '');
      let user;
      if (auth.viaMaster) {
        if (!requested) {
          return jr({ error: 'master token must specify ?user= explicitly' }, 400);
        }
        user = requested;
      } else {
        if (requested && requested !== auth.user) {
          return jr({ error: 'cannot inspect token status for another user' }, 403);
        }
        user = auth.user;
      }
      if (!user) return jr({ error: 'user identity required' }, 400);
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

  // F5-C/D + STAB-SCHED-AUTH-001: Cron triggers (wrangler.toml [triggers].crons)
  // Requires GH_TOKEN Worker secret: wrangler secret put GH_TOKEN
  //
  // Cron routing:
  //   */5          → consume check (F5-C)
  //   */30         → healer + consume heartbeat (F5-D) + tennis_closing_odds dispatch
  //   */2 11-22 SAT,SUN → BL2 live-push dispatch (STAB-SCHED-AUTH-001)
  //   */2 18-22 FRI     → BL2 live-push dispatch (STAB-SCHED-AUTH-001)
  //   15 6-22/2 daily   → tennis_settle dispatch  (STAB-SCHED-AUTH-001)
  async scheduled(event, env) {
    const cron = event.cron;
    const scheduledTime = event.scheduledTime; // ms epoch from Cloudflare
    if (cron === '*/5 * * * *') {
      await _cronConsumeCheck(env);
    } else if (cron === '*/30 * * * *') {
      await _cronHealerCheck(env);
      // Blocker-4: unconditional risk-state heartbeat every 30 min.
      const token = env.GH_TOKEN;
      if (token) {
        await _ghRepositoryDispatch(token, 'consume_pending_bets', env.GH_REPO || _GH_REPO_DEFAULT);
      }
      // STAB-SCHED-AUTH-001: tennis closing-odds scheduler authority.
      await _cronTennisClosingOdds(env, scheduledTime);
    } else if (cron === '*/2 11-22 * * SAT,SUN') {
      // BL2 live-push: Saturday and Sunday match windows.
      await _cronBl2LivePush(env, scheduledTime);
    } else if (cron === '*/2 18-22 * * FRI') {
      // BL2 live-push: Friday match window.
      await _cronBl2LivePush(env, scheduledTime);
    } else if (cron === '15 6-22/2 * * *') {
      // Tennis settlement: 9 slots/day.
      await _cronTennisSettle(env, scheduledTime);
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
