/**
 * STAB-SCHED-AUTH-001: Integration tests for the ACTUAL Worker scheduled() handler.
 *
 * Unlike test_scheduled_dispatch.js (which tests helper copies in isolation),
 * this file imports worker.js directly. Routing changes in scheduled() WILL
 * cause test failures here — that is the intent.
 *
 * Run with: node cloudflare/test_worker_integration.mjs
 *
 * Requires: Node.js 18+ (ES module import, globalThis.fetch).
 */

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

// ── Configurable fetch stub (must be installed BEFORE worker.js import) ──────

const _fetchCalls = [];
let _fetchResponses = [{ ok: true, status: 204 }];

function setFetchResponses(responses) {
  _fetchResponses = Array.isArray(responses) ? responses : [responses];
}

function resetFetch() {
  _fetchCalls.length = 0;
  _fetchResponses = [{ ok: true, status: 204 }];
}

// Install global fetch stub before worker.js loads
globalThis.fetch = async (url, opts) => {
  _fetchCalls.push({ url, opts });
  const idx = Math.min(_fetchCalls.length - 1, _fetchResponses.length - 1);
  return _fetchResponses[idx];
};

// ── Import ACTUAL worker.js ───────────────────────────────────────────────────
// This is the production file. If the scheduled() handler routing changes,
// these tests will fail. That is the intent.

import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Dynamic import so the fetch stub is installed first
const workerModule = await import(join(__dirname, 'worker.js'));
const worker = workerModule.default;

// Verify the worker has the expected scheduled() handler shape
if (!worker || typeof worker.scheduled !== 'function') {
  console.error('FATAL: worker.js default export does not have scheduled() function');
  process.exit(1);
}

// ── Minimal env stub ──────────────────────────────────────────────────────────

function makeEnv(token = 'integration_test_token', repo = 'Philip3006/sportsbrain') {
  return {
    GH_TOKEN: token,
    GH_REPO: repo,
    SIGNALS: {
      get: async () => null,
      put: async () => {},
      list: async () => ({ keys: [] }),
      delete: async () => {},
    },
  };
}

function makeEvent(cron, scheduledTime = new Date('2026-09-05T20:00:00Z').getTime()) {
  return { cron, scheduledTime };
}

function healthJob(job, status, lastRunAt) {
  return { job, status, last_run_at: lastRunAt, exit_code: status === 'error' ? 1 : 0 };
}

function healthPayload(jobs, generatedAt = '2026-08-31T10:00:00Z', overall = 'ok') {
  return { generated_at: generatedAt, overall, jobs };
}

function makeHealthEnv(initialHealth) {
  const signals = {
    football: [],
    bankroll_state: { published_at: '2026-08-31T09:00:00Z' },
  };
  if (initialHealth) signals.health = initialHealth;
  const values = new Map([
    ['signals_json', JSON.stringify(signals)],
  ]);
  return {
    API_TOKEN: 'health_test_token',
    GH_TOKEN: 'integration_test_token',
    GH_REPO: 'Philip3006/sportsbrain',
    SIGNALS: {
      get: async (key) => values.get(key) || null,
      put: async (key, value) => { values.set(key, value); },
      list: async () => ({ keys: [] }),
      delete: async (key) => { values.delete(key); },
    },
    _values: values,
  };
}

function makeCanonicalHealthEnv(initialHealth) {
  const env = makeHealthEnv(initialHealth);
  env._values.set('health_v1', JSON.stringify(initialHealth));
  return env;
}

async function postHealth(env, authority, health) {
  return worker.fetch(new Request(
    `https://worker.test/signals?merge_health=1&health_authority=${authority}`,
    {
      method: 'POST',
      headers: {
        Authorization: 'Bearer health_test_token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ health }),
    },
  ), env);
}

const CLOUD_BOOTSTRAP_JOBS = [
  'bundesliga2_closing_odds', 'bundesliga2_live_push', 'bundesliga2_retrain',
  'bundesliga2_scan', 'bundesliga2_settle', 'consume_pending_bets',
  'tennis_closing_odds', 'tennis_retrain', 'tennis_scan', 'tennis_settle',
  'signals_data_fresh', 'live_scores_fresh',
];

function cloudBootstrapPayload({
  generatedAt = '2026-09-02T10:00:00Z',
  overall = 'ok',
  jobStatus = 'ok',
  jobTimestamp = '2026-09-02T09:59:00Z',
} = {}) {
  return healthPayload(
    CLOUD_BOOTSTRAP_JOBS.map((job) => healthJob(job, jobStatus, jobTimestamp)),
    generatedAt,
    overall,
  );
}

function makeAuthEnv() {
  const values = new Map([
    ['user_tokens', JSON.stringify({
      philip: {
        active: 'philip_active_token',
        previous: {
          token: 'philip_previous_token',
          expires_at: '2030-01-01T00:00:00Z',
        },
        rotated_at: '2026-08-31T00:00:00Z',
      },
    })],
  ]);
  return {
    API_TOKEN: 'old_master_token',
    API_TOKEN_NEXT: 'next_master_token',
    GH_TOKEN: 'integration_test_token',
    GH_REPO: 'Philip3006/sportsbrain',
    SIGNALS: {
      get: async (key) => values.get(key) || null,
      put: async (key, value) => { values.set(key, value); },
      list: async () => ({ keys: [] }),
      delete: async (key) => { values.delete(key); },
    },
  };
}

function authenticatedRequest(path, token) {
  return new Request(`https://worker.test${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

const KNOWN_REPO_URL = 'Philip3006/sportsbrain/dispatches';

// ── Cron routing tests ────────────────────────────────────────────────────────

console.log('\n=== STAB-SCHED-AUTH-001: Worker Integration Tests (actual worker.js) ===');

// ── Master-token transition authentication ──────────────────────────────────

await testAsync('Auth: old and next master tokens retain master-only semantics', async () => {
  const env = makeAuthEnv();
  const oldStatus = await worker.fetch(
    authenticatedRequest('/token_status?user=philip', 'old_master_token'), env,
  );
  const nextStatus = await worker.fetch(
    authenticatedRequest('/token_status?user=philip', 'next_master_token'), env,
  );
  const invalid = await worker.fetch(
    authenticatedRequest('/token_status?user=philip', 'invalid_token'), env,
  );
  assertEq(oldStatus.status, 200, 'old API_TOKEN is accepted');
  assertEq(nextStatus.status, 200, 'API_TOKEN_NEXT is accepted');
  assertEq(invalid.status, 401, 'invalid token is rejected');

  const oldMe = await worker.fetch(authenticatedRequest('/me', 'old_master_token'), env);
  const nextMe = await worker.fetch(authenticatedRequest('/me', 'next_master_token'), env);
  assertEq(oldMe.status, 403, 'old master remains ambiguous for /me');
  assertEq(nextMe.status, 403, 'next master remains ambiguous for /me');
});

await testAsync('Auth: active and grace per-user tokens retain their owner behavior', async () => {
  const env = makeAuthEnv();
  const active = await worker.fetch(
    authenticatedRequest('/token_status?user=philip', 'philip_active_token'), env,
  );
  const previous = await worker.fetch(
    authenticatedRequest('/token_status?user=philip', 'philip_previous_token'), env,
  );
  assertEq(active.status, 200, 'active per-user token remains accepted');
  assertEq(previous.status, 200, 'unexpired per-user grace token remains accepted');
  assertEq((await active.json()).user, 'philip', 'active token retains its owner');
  assertEq((await previous.json()).user, 'philip', 'grace token retains its owner');
});

await testAsync('Auth: next master retains health merge authorization', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeHealthEnv(initial);
  env.API_TOKEN_NEXT = 'next_master_token';
  const response = await worker.fetch(new Request(
    'https://worker.test/signals?merge_health=1&health_authority=local',
    {
      method: 'POST',
      headers: {
        Authorization: 'Bearer next_master_token',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ health: initial }),
    },
  ), env);
  assertEq(response.status, 200, 'next master retains existing health merge authorization');
});

// ── Named weekday BL2 crons ───────────────────────────────────────────────────

await testAsync('Routing: SAT,SUN cron dispatches BL2 live push', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('*/2 11-22 * * SAT,SUN'), env);
  assert(_fetchCalls.length >= 1, 'at least one fetch call');
  const bl2Calls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assert(bl2Calls.length === 1, 'exactly one BL2 live push dispatch for SAT,SUN cron');
});

await testAsync('Routing: FRI cron dispatches BL2 live push', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('*/2 18-22 * * FRI'), env);
  assert(_fetchCalls.length >= 1, 'at least one fetch call');
  const bl2Calls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assert(bl2Calls.length === 1, 'exactly one BL2 live push dispatch for FRI cron');
});

await testAsync('Routing: old numeric SAT/SUN cron (6,0) does NOT dispatch (named weekdays only)', async () => {
  resetFetch();
  const env = makeEnv();
  // The numeric form was the old bugged cron — it must not route anywhere
  await worker.scheduled(makeEvent('*/2 11-22 * * 6,0'), env);
  const bl2Calls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assertEq(bl2Calls.length, 0, 'numeric 6,0 weekday cron does NOT route to BL2 (named weekdays required)');
});

await testAsync('Routing: old numeric FRI cron (5) does NOT dispatch (named weekdays only)', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('*/2 18-22 * * 5'), env);
  const bl2Calls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assertEq(bl2Calls.length, 0, 'numeric 5 weekday cron does NOT route to BL2 (named weekdays required)');
});

await testAsync('Routing: Thursday does not dispatch BL2 (no Thursday cron)', async () => {
  resetFetch();
  const env = makeEnv();
  // Simulate a hypothetical Thursday cron that should NOT exist
  await worker.scheduled(makeEvent('*/2 18-22 * * THU'), env);
  const bl2Calls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assertEq(bl2Calls.length, 0, 'THU cron does not dispatch BL2');
});

// ── Tennis closing odds cron ──────────────────────────────────────────────────

await testAsync('Routing: */30 cron dispatches tennis_closing_odds', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('*/30 * * * *'), env);
  const closingCalls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_tennis_closing_odds'
  );
  assert(closingCalls.length === 1, 'exactly one tennis_closing_odds dispatch on */30 cron');
});

// ── Tennis settle cron ────────────────────────────────────────────────────────

await testAsync('Routing: tennis settle cron dispatches tennis_settle', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('15 6-22/2 * * *'), env);
  const settleCalls = _fetchCalls.filter(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_tennis_settle'
  );
  assert(settleCalls.length === 1, 'exactly one tennis_settle dispatch');
});

// ── Unknown cron does nothing harmful ────────────────────────────────────────

await testAsync('Routing: unknown cron string does not dispatch anything', async () => {
  resetFetch();
  const env = makeEnv();
  await worker.scheduled(makeEvent('*/7 * * * *'), env);
  const scheduledDispatches = _fetchCalls.filter(c => {
    try {
      const body = JSON.parse(c.opts && c.opts.body);
      return ['sportsbrain_bl2_live_push', 'sportsbrain_tennis_closing_odds', 'sportsbrain_tennis_settle']
        .includes(body.event_type);
    } catch { return false; }
  });
  assertEq(scheduledDispatches.length, 0, 'unknown cron does not dispatch any scheduled job');
});

// ── Payload fields from actual scheduled() handler ────────────────────────────

await testAsync('Payload: FRI BL2 dispatch has correct client_payload fields', async () => {
  resetFetch();
  const env = makeEnv();
  const ts = new Date('2026-09-05T20:14:00Z').getTime();
  await worker.scheduled(makeEvent('*/2 18-22 * * FRI', ts), env);

  const bl2Call = _fetchCalls.find(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_bl2_live_push'
  );
  assert(bl2Call !== undefined, 'BL2 dispatch call found');

  const payload = JSON.parse(bl2Call.opts.body).client_payload;
  assertEq(payload.scheduler, 'cloudflare_cron', 'scheduler is cloudflare_cron');
  assertEq(payload.scheduled_at, '2026-09-05T20:14:00.000Z', 'scheduled_at matches event.scheduledTime');
  assertEq(payload.idempotency_key, 'bl2_live_push/2026-09-05T20:14', 'idempotency_key has minute granularity');
});

await testAsync('Payload: tennis settle dispatch has correct client_payload fields', async () => {
  resetFetch();
  const env = makeEnv();
  const ts = new Date('2026-09-05T08:15:00Z').getTime();
  await worker.scheduled(makeEvent('15 6-22/2 * * *', ts), env);

  const settleCall = _fetchCalls.find(c =>
    c.opts && c.opts.body && JSON.parse(c.opts.body).event_type === 'sportsbrain_tennis_settle'
  );
  assert(settleCall !== undefined, 'tennis_settle dispatch call found');

  const payload = JSON.parse(settleCall.opts.body).client_payload;
  assertEq(payload.scheduler, 'cloudflare_cron', 'scheduler field correct');
  assert(payload.idempotency_key.startsWith('tennis_settle/'), 'idempotency_key prefix correct');
});

// ── GH dispatch HTTP failure handling from actual Worker ──────────────────────

await testAsync('Worker: 2xx dispatch is accepted (no throw)', async () => {
  resetFetch();
  setFetchResponses([{ ok: true, status: 204 }]);
  const env = makeEnv();
  let threw = false;
  try { await worker.scheduled(makeEvent('*/2 18-22 * * FRI'), env); } catch { threw = true; }
  assert(!threw, '2xx response does not cause scheduled() to throw');
});

await testAsync('Worker: 401 from GH causes scheduled() to fail', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 401 }]);
  const env = makeEnv();
  let threw = false;
  try { await worker.scheduled(makeEvent('*/2 18-22 * * FRI'), env); } catch { threw = true; }
  assert(threw, '401 response causes scheduled() to throw (fail-closed)');
});

await testAsync('Worker: 403 from GH causes scheduled() to fail', async () => {
  resetFetch();
  setFetchResponses([{ ok: false, status: 403 }]);
  const env = makeEnv();
  let threw = false;
  try { await worker.scheduled(makeEvent('*/2 11-22 * * SAT,SUN'), env); } catch { threw = true; }
  assert(threw, '403 response causes scheduled() to throw (fail-closed)');
});

await testAsync('Worker: 500 retries then fails (tennis settle)', async () => {
  resetFetch();
  setFetchResponses([
    { ok: false, status: 500 },
    { ok: false, status: 500 },
    { ok: false, status: 500 },
  ]);
  const env = makeEnv();
  let threw = false;
  try { await worker.scheduled(makeEvent('15 6-22/2 * * *'), env); } catch { threw = true; }
  assert(threw, '500 retry exhaustion causes scheduled() to throw');
  // settle only dispatches tennis_settle — filter those calls
  const settleFetches = _fetchCalls.filter(c => {
    try { return JSON.parse(c.opts.body).event_type === 'sportsbrain_tennis_settle'; } catch { return false; }
  });
  assertEq(settleFetches.length, 3, '500: 3 total settle dispatch attempts');
});

// ── Missing GH_TOKEN fails closed (actual Worker) ────────────────────────────

await testAsync('Worker: missing GH_TOKEN causes BL2 scheduled() to throw', async () => {
  resetFetch();
  const env = makeEnv('');  // no token
  let threw = false;
  try { await worker.scheduled(makeEvent('*/2 18-22 * * FRI'), env); } catch { threw = true; }
  assert(threw, 'BL2 scheduled() throws when GH_TOKEN missing (fail-closed)');
  assertEq(_fetchCalls.length, 0, 'no fetch attempted when token missing');
});

await testAsync('Worker: missing GH_TOKEN causes tennis_closing_odds scheduled() to throw', async () => {
  resetFetch();
  const env = makeEnv('');
  let threw = false;
  try { await worker.scheduled(makeEvent('*/30 * * * *'), env); } catch { threw = true; }
  // Note: */30 also calls _cronHealerCheck/_cronConsumeCheck which silently return on missing token.
  // _cronTennisClosingOdds throws — so scheduled() propagates.
  assert(threw, 'tennis_closing_odds scheduled() throws when GH_TOKEN missing');
});

await testAsync('Worker: missing GH_TOKEN causes tennis_settle scheduled() to throw', async () => {
  resetFetch();
  const env = makeEnv('');
  let threw = false;
  try { await worker.scheduled(makeEvent('15 6-22/2 * * *'), env); } catch { threw = true; }
  assert(threw, 'tennis_settle scheduled() throws when GH_TOKEN missing');
  assertEq(_fetchCalls.length, 0, 'no fetch attempted when token missing');
});

// ── OPS-LOCALHEALTH-PUBLISH-001: actual Worker health merge boundary ───────

await testAsync('Health bootstrap: complete Cloud evidence initializes dedicated health without financial writes', async () => {
  const env = makeHealthEnv();
  const beforeSignals = env._values.get('signals_json');
  const response = await postHealth(env, 'cloud', cloudBootstrapPayload({
    overall: 'ok',
    jobStatus: 'error',
  }));
  assertEq(response.status, 200, 'complete Cloud bootstrap accepted');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.length, CLOUD_BOOTSTRAP_JOBS.length, 'dedicated health_v1 contains complete Cloud evidence');
  assertEq(stored.overall, 'down', 'bootstrap recomputes overall instead of trusting caller');
  assertEq(env._values.get('signals_json'), beforeSignals, 'bootstrap leaves financial signals record untouched');
});

await testAsync('Health bootstrap: Local and invalid Cloud payloads fail closed', async () => {
  const local = await postHealth(makeHealthEnv(), 'local', healthPayload([
    healthJob('daily_scan', 'ok', '2026-09-02T09:59:00Z'),
  ]));
  assertEq(local.status, 409, 'Local authority cannot bootstrap missing canonical health');
  assertEq(await local.text(), 'canonical_health_baseline_missing', 'Local bootstrap failure is explicit');

  const unknownPayload = cloudBootstrapPayload();
  unknownPayload.jobs.push(healthJob('unknown_job', 'ok', '2026-09-02T09:59:00Z'));
  const unknown = await postHealth(makeHealthEnv(), 'cloud', unknownPayload);
  assertEq(unknown.status, 409, 'unknown Cloud bootstrap job rejected');

  const localPayload = cloudBootstrapPayload();
  localPayload.jobs.push(healthJob('daily_scan', 'ok', '2026-09-02T09:59:00Z'));
  const foreignLocal = await postHealth(makeHealthEnv(), 'cloud', localPayload);
  assertEq(foreignLocal.status, 409, 'Local-owned job rejected from Cloud bootstrap');

  const invalidGenerated = await postHealth(makeHealthEnv(), 'cloud', cloudBootstrapPayload({
    generatedAt: 'not-a-timestamp',
  }));
  assertEq(invalidGenerated.status, 409, 'invalid bootstrap generated_at rejected');

  const invalidTimestamp = await postHealth(makeHealthEnv(), 'cloud', cloudBootstrapPayload({
    jobTimestamp: 'not-a-timestamp',
  }));
  assertEq(invalidTimestamp.status, 409, 'invalid bootstrap job timestamp rejected');
});

await testAsync('Health bootstrap: duplicate and incomplete Cloud coverage fail closed', async () => {
  const duplicatePayload = cloudBootstrapPayload();
  duplicatePayload.jobs.push({ ...duplicatePayload.jobs[0] });
  const duplicate = await postHealth(makeHealthEnv(), 'cloud', duplicatePayload);
  assertEq(duplicate.status, 409, 'duplicate Cloud bootstrap job rejected');
  assertEq(await duplicate.text(), 'duplicate_health_job', 'duplicate failure is explicit');

  const incompletePayload = cloudBootstrapPayload();
  incompletePayload.jobs.pop();
  const incomplete = await postHealth(makeHealthEnv(), 'cloud', incompletePayload);
  assertEq(incomplete.status, 409, 'incomplete Cloud bootstrap coverage rejected');
  assertEq(await incomplete.text(), 'incomplete_cloud_health_bootstrap', 'incomplete failure is explicit');
});

await testAsync('Health: fresh local-authoritative job updates without touching financial signals', async () => {
  const initial = healthPayload([
    healthJob('daily_scan', 'ok', '2026-08-31T09:00:00Z'),
    healthJob('tennis_scan', 'ok', '2026-08-31T09:30:00Z'),
  ]);
  const env = makeCanonicalHealthEnv(initial);
  const beforeSignals = env._values.get('signals_json');
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'degraded', '2026-08-31T10:00:00Z'),
  ], '2026-08-31T10:00:00Z', 'ok'));
  assertEq(response.status, 200, 'fresh local update accepted');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.find(j => j.job === 'daily_scan').status, 'degraded', 'local job updated');
  assertEq(stored.jobs.find(j => j.job === 'tennis_scan').status, 'ok', 'cloud job preserved');
  assertEq(env._values.get('signals_json'), beforeSignals, 'financial signals record unchanged');
});

await testAsync('Health: stale local-authoritative update is rejected', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeCanonicalHealthEnv(initial);
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'error', '2026-08-31T09:59:00Z'),
  ], '2026-08-31T10:01:00Z'));
  assertEq(response.status, 409, 'stale local update rejected');
  assertEq(await response.text(), 'stale_health_update', 'stale reason is explicit');
});

await testAsync('Health: equal timestamps remain no-op after canonical initialization', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeCanonicalHealthEnv(initial);
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'error', '2026-08-31T10:00:00Z'),
  ], '2026-08-31T11:00:00Z'));
  assertEq(response.status, 200, 'equal timestamp accepted as no-op');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.find(j => j.job === 'daily_scan').status, 'ok', 'equal timestamp cannot overwrite canonical status');
});

await testAsync('Health: local payload cannot clobber cloud-authoritative job', async () => {
  const initial = healthPayload([healthJob('tennis_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeCanonicalHealthEnv(initial);
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('tennis_scan', 'error', '2026-08-31T11:00:00Z'),
  ], '2026-08-31T11:00:00Z'));
  assertEq(response.status, 409, 'foreign cloud job rejected from local authority');
  assertEq(JSON.parse(env._values.get('signals_json')).health.jobs[0].status, 'ok', 'cloud truth unchanged');
});

await testAsync('Health: Cloud update preserves local-authoritative evidence after initialization', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeCanonicalHealthEnv(initial);
  const response = await postHealth(env, 'cloud', cloudBootstrapPayload({
    generatedAt: '2026-08-31T11:00:00Z',
    jobTimestamp: '2026-08-31T10:30:00Z',
  }));
  assertEq(response.status, 200, 'valid Cloud update accepted after initialization');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.find(j => j.job === 'daily_scan').status, 'ok', 'Cloud update retains local evidence');
});

await testAsync('Health: unknown local job and malformed timestamps fail closed', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeHealthEnv(initial);
  const unknown = await postHealth(env, 'local', healthPayload([
    healthJob('unknown_job', 'ok', '2026-08-31T11:00:00Z'),
  ], '2026-08-31T11:00:00Z'));
  assertEq(unknown.status, 409, 'unknown local job rejected');
  const malformed = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'ok', 'not-a-timestamp'),
  ], '2026-08-31T11:00:00Z'));
  assertEq(malformed.status, 409, 'malformed timestamp rejected');
});

await testAsync('Health: no-snapshot evidence remains non-fresh without blocking cloud truth', async () => {
  const initial = healthPayload([healthJob('daily_scan', 'ok', '2026-08-31T10:00:00Z')]);
  const env = makeHealthEnv(initial);
  const response = await postHealth(env, 'cloud', healthPayload([
    { job: 'tennis_scan', status: 'error', last_run_at: null, exit_code: null },
  ], '2026-08-31T11:00:00Z'));
  assertEq(response.status, 200, 'truthful no-snapshot cloud evidence accepted');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.find(j => j.job === 'tennis_scan').last_run_at, null, 'unknown timestamp preserved');
  assertEq(stored.overall, 'down', 'unknown execution evidence is not treated as healthy');
});

await testAsync('Health: partial update preserves unrelated jobs and recomputes overall', async () => {
  const initial = healthPayload([
    healthJob('daily_scan', 'ok', '2026-08-31T09:00:00Z'),
    healthJob('tennis_scan', 'ok', '2026-08-31T09:00:00Z'),
  ]);
  const env = makeHealthEnv(initial);
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'error', '2026-08-31T10:00:00Z'),
  ], '2026-08-31T10:00:00Z', 'ok'));
  assertEq(response.status, 200, 'partial local update accepted');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.length, 2, 'unrelated job retained');
  assertEq(stored.overall, 'down', 'overall recomputed instead of trusting caller');
});

await testAsync('Health: detected KV write race retries and preserves both authority updates', async () => {
  const initial = healthPayload([
    healthJob('daily_scan', 'ok', '2026-08-31T09:00:00Z'),
    healthJob('tennis_scan', 'ok', '2026-08-31T09:00:00Z'),
  ]);
  const env = makeHealthEnv(initial);
  let intercepted = false;
  const originalPut = env.SIGNALS.put;
  env.SIGNALS.put = async (key, value) => {
    if (key === 'health_v1' && !intercepted) {
      intercepted = true;
      await originalPut(key, JSON.stringify(healthPayload([
        healthJob('daily_scan', 'ok', '2026-08-31T09:00:00Z'),
        healthJob('tennis_scan', 'degraded', '2026-08-31T10:30:00Z'),
      ], '2026-08-31T10:30:00Z')));
      return;
    }
    await originalPut(key, value);
  };
  const response = await postHealth(env, 'local', healthPayload([
    healthJob('daily_scan', 'degraded', '2026-08-31T10:00:00Z'),
  ], '2026-08-31T10:00:00Z'));
  assertEq(response.status, 200, 'detected race retried successfully');
  const stored = JSON.parse(env._values.get('health_v1'));
  assertEq(stored.jobs.find(j => j.job === 'daily_scan').status, 'degraded', 'local update retained');
  assertEq(stored.jobs.find(j => j.job === 'tennis_scan').status, 'degraded', 'concurrent cloud update retained');
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
