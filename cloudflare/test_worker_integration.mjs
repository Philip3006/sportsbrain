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

const KNOWN_REPO_URL = 'Philip3006/sportsbrain/dispatches';

// ── Cron routing tests ────────────────────────────────────────────────────────

console.log('\n=== STAB-SCHED-AUTH-001: Worker Integration Tests (actual worker.js) ===');

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
