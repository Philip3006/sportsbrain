/**
 * STAB-SCHED-AUTH-001: Unit tests for the Cloudflare scheduled() handler helpers.
 *
 * Tests cron dispatch helper logic: event_type routing, payload construction,
 * idempotency key structure, fail-closed dispatch semantics (HTTP error handling).
 * Run with: node cloudflare/test_scheduled_dispatch.js
 *
 * Worker integration tests (actual scheduled() routing) are in:
 *   cloudflare/test_worker_integration.mjs
 *
 * Uses a configurable fetch-stub — no real network calls, no Cloudflare Worker runtime.
 */

'use strict';

// ── Minimal test harness ──────────────────────────────────────────────────────

let _passed = 0;
let _failed = 0;
const _failures = [];

function assert(condition, msg) {
  if (condition) {
    _passed++;
  } else {
    _failed++;
    _failures.push(msg);
    console.error(`  FAIL: ${msg}`);
  }
}

function assertEq(actual, expected, msg) {
  if (actual === expected) {
    _passed++;
  } else {
    _failed++;
    const detail = `${msg} — expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`;
    _failures.push(detail);
    console.error(`  FAIL: ${detail}`);
  }
}

function test(name, fn) {
  console.log(`\n  ${name}`);
  try {
    fn();
  } catch (e) {
    _failed++;
    const msg = `${name} threw: ${e.message}`;
    _failures.push(msg);
    console.error(`  FAIL: ${msg}`);
  }
}

async function testAsync(name, fn) {
  console.log(`\n  ${name}`);
  try {
    await fn();
  } catch (e) {
    _failed++;
    const msg = `${name} threw: ${e.message}`;
    _failures.push(msg);
    console.error(`  FAIL: ${msg}`);
  }
}

// ── Configurable fetch stub ───────────────────────────────────────────────────

const _fetchCalls = [];
let _fetchResponses = [{ ok: true, status: 204 }]; // default: success

// Override the global fetch for a single test via setFetchResponses().
// Each element in the array is returned for successive fetch calls (cycling on last).
function setFetchResponses(responses) {
  _fetchResponses = Array.isArray(responses) ? responses : [responses];
}

function resetFetch() {
  _fetchCalls.length = 0;
  _fetchResponses = [{ ok: true, status: 204 }];
}

global.fetch = async (url, opts) => {
  _fetchCalls.push({ url, opts });
  const idx = Math.min(_fetchCalls.length - 1, _fetchResponses.length - 1);
  return _fetchResponses[idx];
};

// ── Minimal env stub ──────────────────────────────────────────────────────────

function makeEnv(token = 'test_gh_token', repo = 'Philip3006/sportsbrain') {
  return {
    GH_TOKEN: token,
    GH_REPO: repo,
    SIGNALS: {
      get: async () => null,
      put: async () => {},
    },
  };
}

// ── Helpers mirroring production worker.js (kept in sync) ────────────────────
// These mirror the production helpers so unit tests can exercise payload logic.
// If worker.js changes, these must be updated to match.
// Routing tests (which cron string → which job) live in test_worker_integration.mjs
// and use the ACTUAL worker.js — so routing regressions are caught there.

const _GH_REPO_DEFAULT = 'Philip3006/sportsbrain';
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

// GH_TOKEN missing is a hard configuration failure — throw, never silently skip.
async function _cronBl2LivePush(env, scheduledTime) {
  const token = env.GH_TOKEN;
  if (!token) {
    console.error('[cron] GH_TOKEN missing — cannot dispatch sportsbrain_bl2_live_push');
    throw new Error('GH_TOKEN not configured: sportsbrain_bl2_live_push dispatch impossible');
  }
  const scheduledAt = new Date(scheduledTime).toISOString();
  const minuteKey = scheduledAt.slice(0, 16);
  await _ghRepositoryDispatchWithPayload(token, 'sportsbrain_bl2_live_push', {
    scheduled_at: scheduledAt,
    scheduler: 'cloudflare_cron',
    idempotency_key: `bl2_live_push/${minuteKey}`,
  }, env.GH_REPO || _GH_REPO_DEFAULT);
}

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

// ── Tests ─────────────────────────────────────────────────────────────────────

console.log('\n=== STAB-SCHED-AUTH-001: Cloudflare Scheduled Dispatch Tests ===');

// ── BL2 live push ─────────────────────────────────────────────────────────────

await testAsync('BL2: dispatches repository_dispatch with correct event_type', async () => {
  resetFetch();
  const env = makeEnv();
  const scheduledMs = new Date('2026-08-30T18:36:00Z').getTime();
  await _cronBl2LivePush(env, scheduledMs);

  assert(_fetchCalls.length === 1, 'exactly one fetch call');
  const call = _fetchCalls[0];
  assert(call.url.includes('Philip3006/sportsbrain/dispatches'), 'URL targets correct repo');
  assert(call.url.includes('api.github.com'), 'URL is GitHub API');

  const body = JSON.parse(call.opts.body);
  assertEq(body.event_type, 'sportsbrain_bl2_live_push', 'event_type is BL2 push');
  assertEq(body.client_payload.scheduler, 'cloudflare_cron', 'scheduler is cloudflare_cron');
  assertEq(body.client_payload.scheduled_at, '2026-08-30T18:36:00.000Z', 'scheduled_at matches');
});

await testAsync('BL2: idempotency_key has minute-granularity prefix', async () => {
  resetFetch();
  const env = makeEnv();
  const ms = new Date('2026-08-30T18:36:45Z').getTime(); // seconds matter here
  await _cronBl2LivePush(env, ms);
  const body = JSON.parse(_fetchCalls[0].opts.body);
  // Key should be truncated at minute boundary: 2026-08-30T18:36
  assertEq(body.client_payload.idempotency_key, 'bl2_live_push/2026-08-30T18:36', 'minute-key correct');
});

await testAsync('BL2: same minute produces same idempotency_key (duplicate-safe)', async () => {
  const env = makeEnv();
  const ms1 = new Date('2026-08-30T18:36:01Z').getTime();
  const ms2 = new Date('2026-08-30T18:36:59Z').getTime();

  resetFetch();
  await _cronBl2LivePush(env, ms1);
  const key1 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  resetFetch();
  await _cronBl2LivePush(env, ms2);
  const key2 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  assertEq(key1, key2, 'same minute → same idempotency_key');
});

await testAsync('BL2: different minutes produce different idempotency_keys', async () => {
  const env = makeEnv();
  resetFetch();
  await _cronBl2LivePush(env, new Date('2026-08-30T18:36:00Z').getTime());
  const key1 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  resetFetch();
  await _cronBl2LivePush(env, new Date('2026-08-30T18:38:00Z').getTime());
  const key2 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  assert(key1 !== key2, 'different minutes → different idempotency_keys');
});

await testAsync('BL2: missing GH_TOKEN fails closed (throws, not silent skip)', async () => {
  resetFetch();
  const env = makeEnv('');  // empty token
  let threw = false;
  let errMsg = '';
  try { await _cronBl2LivePush(env, Date.now()); } catch (e) { threw = true; errMsg = e.message; }
  assert(threw, 'missing GH_TOKEN throws');
  assertEq(_fetchCalls.length, 0, 'no fetch attempted when token missing');
  assert(errMsg.length > 0, 'error message is not empty');
});

await testAsync('BL2: uses custom GH_REPO if set', async () => {
  resetFetch();
  const env = makeEnv('tok', 'CustomOrg/custom-repo');
  await _cronBl2LivePush(env, Date.now());
  assert(_fetchCalls[0].url.includes('CustomOrg/custom-repo'), 'custom repo used');
});

await testAsync('BL2: Authorization header contains token', async () => {
  resetFetch();
  const env = makeEnv('secret_token_xyz');
  await _cronBl2LivePush(env, Date.now());
  const auth = _fetchCalls[0].opts.headers.Authorization;
  assertEq(auth, 'token secret_token_xyz', 'Bearer token in Authorization header');
});

// ── Tennis closing odds ───────────────────────────────────────────────────────

await testAsync('ClosingOdds: dispatches correct event_type', async () => {
  resetFetch();
  const env = makeEnv();
  await _cronTennisClosingOdds(env, new Date('2026-08-30T12:30:00Z').getTime());
  const body = JSON.parse(_fetchCalls[0].opts.body);
  assertEq(body.event_type, 'sportsbrain_tennis_closing_odds', 'event_type correct');
  assertEq(body.client_payload.scheduler, 'cloudflare_cron', 'scheduler correct');
});

await testAsync('ClosingOdds: idempotency_key prefixed with tennis_closing_odds/', async () => {
  resetFetch();
  const env = makeEnv();
  await _cronTennisClosingOdds(env, new Date('2026-08-30T12:30:00Z').getTime());
  const key = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;
  assert(key.startsWith('tennis_closing_odds/'), 'key has correct prefix');
});

await testAsync('ClosingOdds: missing GH_TOKEN fails closed (throws)', async () => {
  resetFetch();
  let threw = false;
  try { await _cronTennisClosingOdds(makeEnv(''), Date.now()); } catch { threw = true; }
  assert(threw, 'missing GH_TOKEN throws for tennis_closing_odds');
  assertEq(_fetchCalls.length, 0, 'no fetch attempted when token missing');
});

// ── Tennis settle ─────────────────────────────────────────────────────────────

await testAsync('TennisSettle: dispatches correct event_type', async () => {
  resetFetch();
  const env = makeEnv();
  await _cronTennisSettle(env, new Date('2026-08-30T08:15:00Z').getTime());
  const body = JSON.parse(_fetchCalls[0].opts.body);
  assertEq(body.event_type, 'sportsbrain_tennis_settle', 'event_type correct');
  assertEq(body.client_payload.scheduler, 'cloudflare_cron', 'scheduler correct');
});

await testAsync('TennisSettle: idempotency_key prefixed with tennis_settle/', async () => {
  resetFetch();
  const env = makeEnv();
  await _cronTennisSettle(env, new Date('2026-08-30T08:15:00Z').getTime());
  const key = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;
  assert(key.startsWith('tennis_settle/'), 'key has correct prefix');
});

await testAsync('TennisSettle: missing GH_TOKEN fails closed (throws)', async () => {
  resetFetch();
  let threw = false;
  try { await _cronTennisSettle(makeEnv(''), Date.now()); } catch { threw = true; }
  assert(threw, 'missing GH_TOKEN throws for tennis_settle');
  assertEq(_fetchCalls.length, 0, 'no fetch attempted when token missing');
});

// ── Idempotency: duplicate dispatch within same minute ────────────────────────

await testAsync('Idempotency: BL2 duplicate within same minute → same key (at-least-once safe)', async () => {
  const env = makeEnv();
  const baseMs = new Date('2026-08-30T20:04:00Z').getTime();

  const keys = [];
  for (let i = 0; i < 3; i++) {
    resetFetch();
    await _cronBl2LivePush(env, baseMs + i * 1000); // same minute, different seconds
    keys.push(JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key);
  }
  assert(keys.every(k => k === keys[0]), `all 3 keys identical: ${keys[0]}`);
});

await testAsync('Idempotency: settle duplicate within same minute → same key', async () => {
  const env = makeEnv();
  const baseMs = new Date('2026-08-30T08:15:00Z').getTime();

  resetFetch();
  await _cronTennisSettle(env, baseMs);
  const k1 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  resetFetch();
  await _cronTennisSettle(env, baseMs + 30000); // 30s later, same minute
  const k2 = JSON.parse(_fetchCalls[0].opts.body).client_payload.idempotency_key;

  assertEq(k1, k2, 'same minute key for settle duplicate');
});

// ── Payload structure compliance ──────────────────────────────────────────────

await testAsync('Payload: all three handlers include scheduler + scheduled_at + idempotency_key', async () => {
  const env = makeEnv();
  const now = new Date('2026-08-30T16:30:00Z').getTime();

  for (const [fn, label] of [
    [_cronBl2LivePush, 'BL2'],
    [_cronTennisClosingOdds, 'ClosingOdds'],
    [_cronTennisSettle, 'TennisSettle'],
  ]) {
    resetFetch();
    await fn(env, now);
    const payload = JSON.parse(_fetchCalls[0].opts.body).client_payload;
    assert(typeof payload.scheduled_at === 'string', `${label}: scheduled_at is string`);
    assert(typeof payload.scheduler === 'string', `${label}: scheduler is string`);
    assert(typeof payload.idempotency_key === 'string', `${label}: idempotency_key is string`);
    assert(payload.scheduler === 'cloudflare_cron', `${label}: scheduler value correct`);
  }
});

await testAsync('Payload: scheduled_at is valid ISO-8601', async () => {
  const env = makeEnv();
  const testMs = new Date('2026-08-30T18:36:22.500Z').getTime();
  resetFetch();
  await _cronBl2LivePush(env, testMs);
  const sat = JSON.parse(_fetchCalls[0].opts.body).client_payload.scheduled_at;
  const parsed = new Date(sat);
  assert(!isNaN(parsed.getTime()), 'scheduled_at parses as valid Date');
  // Should round-trip within 1ms
  assert(Math.abs(parsed.getTime() - testMs) <= 1, 'scheduled_at preserves millisecond precision');
});

// ── Fail-closed dispatch semantics (Blocker 4) ────────────────────────────────

await testAsync('Dispatch: 204 No Content succeeds without throw', async () => {
  resetFetch();
  setFetchResponses([{ ok: true, status: 204 }]);
  const env = makeEnv();
  // Should not throw
  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }
  assert(!threw, '204 response does not throw');
  assertEq(_fetchCalls.length, 1, 'exactly one fetch for 204');
});

await testAsync('Dispatch: 200 OK succeeds without throw', async () => {
  resetFetch();
  setFetchResponses([{ ok: true, status: 200 }]);
  const env = makeEnv();
  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }
  assert(!threw, '200 response does not throw');
});

await testAsync('Dispatch: 401 fails immediately (permanent error, no retry)', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 401 }]);
  const env = makeEnv();
  let threw = false;
  let thrownMsg = '';
  try { await _cronBl2LivePush(env, Date.now()); } catch (e) { threw = true; thrownMsg = e.message; }
  assert(threw, '401 throws');
  assertEq(_fetchCalls.length, 1, '401: no retry (exactly 1 fetch)');
  assert(thrownMsg.includes('401'), '401 error message contains status code');
});

await testAsync('Dispatch: 403 fails immediately (permanent error, no retry)', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 403 }]);
  const env = makeEnv();
  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }
  assert(threw, '403 throws');
  assertEq(_fetchCalls.length, 1, '403: no retry (exactly 1 fetch)');
});

await testAsync('Dispatch: 404 fails immediately (permanent error, no retry)', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 404 }]);
  const env = makeEnv();
  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }
  assert(threw, '404 throws');
  assertEq(_fetchCalls.length, 1, '404: no retry (exactly 1 fetch)');
});

await testAsync('Dispatch: 429 retries then fails truthfully', async () => {
  resetFetch();
  // All responses are 429 — should retry _DISPATCH_RETRY_DELAYS_MS.length times then fail.
  setFetchResponses([{ ok: false, status: 429 }, { ok: false, status: 429 }, { ok: false, status: 429 }]);
  const env = makeEnv();
  let threw = false;
  let thrownMsg = '';
  try { await _cronBl2LivePush(env, Date.now()); } catch (e) { threw = true; thrownMsg = e.message; }
  assert(threw, '429 eventually throws after retries');
  // 3 attempts: initial + 2 retries (matches _DISPATCH_RETRY_DELAYS_MS.length)
  assertEq(_fetchCalls.length, 3, '429: 3 total attempts (1 initial + 2 retries)');
  assert(thrownMsg.includes('429'), '429 error message contains status code');
});

await testAsync('Dispatch: 429 succeeds if retry returns 204', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 429 }, { ok: true, status: 204 }]);
  const env = makeEnv();
  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }
  assert(!threw, '429 then 204 → no throw');
  assertEq(_fetchCalls.length, 2, '429→204: 2 fetch calls');
});

await testAsync('Dispatch: 500 retries then fails truthfully', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 500 }, { ok: false, status: 500 }, { ok: false, status: 500 }]);
  const env = makeEnv();
  let threw = false;
  let thrownMsg = '';
  try { await _cronBl2LivePush(env, Date.now()); } catch (e) { threw = true; thrownMsg = e.message; }
  assert(threw, '500 eventually throws');
  assertEq(_fetchCalls.length, 3, '500: 3 total attempts');
  assert(thrownMsg.includes('500'), '500 error message contains status code');
});

await testAsync('Dispatch: token is never logged in error output', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 401 }]);
  const token = 'super_secret_token_abc123';
  const env = makeEnv(token);

  // Capture console.error
  const logged = [];
  const orig = console.error;
  console.error = (...args) => logged.push(args.join(' '));

  let threw = false;
  try { await _cronBl2LivePush(env, Date.now()); } catch { threw = true; }

  console.error = orig;

  assert(threw, 'dispatch failed as expected');
  const loggedText = logged.join('\n');
  assert(!loggedText.includes(token), `token not present in logged output: ${loggedText.slice(0, 200)}`);
});

// ── Summary ───────────────────────────────────────────────────────────────────

console.log(`\n${'─'.repeat(60)}`);
console.log(`Results: ${_passed} passed, ${_failed} failed`);
if (_failures.length > 0) {
  console.log('\nFailures:');
  _failures.forEach(f => console.log(`  ✗ ${f}`));
  process.exit(1);
} else {
  console.log('\nAll tests passed.');
  process.exit(0);
}
