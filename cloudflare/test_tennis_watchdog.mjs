/** Offline tests for the independent Tennis Scan recovery watchdog. */

import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';

const fetchCalls = [];
let fetchHandler = async () => new Response(JSON.stringify({ workflow_runs: [], total_count: 0 }), { status: 200 });
globalThis.fetch = async (url, options = {}) => {
  fetchCalls.push({ url: String(url), options });
  return fetchHandler(String(url), options);
};

const { default: worker } = await import('./worker.js');

function makeEnv({ values = {}, token = 'watchdog-token' } = {}) {
  const store = new Map(Object.entries(values));
  return {
    API_TOKEN: 'operator-token',
    GH_TOKEN: token,
    GH_REPO: 'Philip3006/sportsbrain',
    SIGNALS: {
      get: async (key) => store.get(key) || null,
      put: async (key, value) => { store.set(key, value); },
      list: async () => ({ keys: [] }),
      delete: async (key) => { store.delete(key); },
    },
    _store: store,
  };
}

function reset(handler) {
  fetchCalls.length = 0;
  fetchHandler = handler;
}

function githubReceipt(status, slot) {
  const payload = { schema_version: 'tennis-scan-slot-receipt-v1', expected_slot: slot, status };
  return new Response(JSON.stringify({ content: Buffer.from(JSON.stringify(payload)).toString('base64') }), { status: 200 });
}

const NOW = new Date('2026-10-03T12:20:00Z').getTime();
const SLOT = 'tennis-scan:2026-10-03T12:00Z';

function scheduleRuns(count, createdAt) {
  return Array.from({ length: count }, (_, index) => ({
    id: 1000 + index,
    created_at: createdAt,
    status: 'completed',
    conclusion: 'success',
  }));
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({ workflow_runs: [], total_count: 0 }), { status: 200 });
  }
  if (url.includes('/actions/workflows/tennis_scan.yml/dispatches')) return new Response(null, { status: 204 });
  return new Response('', { status: 500 });
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  const dispatch = fetchCalls.find((call) => call.url.includes('/dispatches'));
  assert.ok(dispatch, 'missing slot dispatches recovery workflow');
  const body = JSON.parse(dispatch.options.body);
  assert.equal(body.ref, 'main');
  assert.deepEqual(body.inputs, { expected_slot: SLOT, recovery: 'true' });
  const state = JSON.parse(env._store.get('tennis_scan_watchdog_v1'));
  assert.equal(state.slots[SLOT].dispatch_count, 1);
  assert.equal(state.last_action, 'recovery_dispatched');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({
      total_count: 500,
      workflow_runs: scheduleRuns(100, '2026-09-01T00:00:00Z'),
    }), { status: 200 });
  }
  if (url.includes('/actions/workflows/tennis_scan.yml/dispatches')) return new Response(null, { status: 204 });
  throw new Error('unexpected GitHub call');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 1, 'older full page permits absence conclusion');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({
      total_count: 500,
      workflow_runs: scheduleRuns(100, '2026-10-03T13:00:00Z'),
    }), { status: 200 });
  }
  throw new Error('truncated history must not dispatch');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 0, 'newer oldest page entry is ambiguous');
  const state = JSON.parse(env._store.get('tennis_scan_watchdog_v1'));
  assert.equal(state.last_action, 'fail_closed_no_dispatch');
  assert.equal(state.failure_class, 'github_state_unavailable_or_ambiguous');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({
      total_count: 50,
      workflow_runs: scheduleRuns(50, '2026-09-01T00:00:00Z'),
    }), { status: 200 });
  }
  if (url.includes('/actions/workflows/tennis_scan.yml/dispatches')) return new Response(null, { status: 204 });
  throw new Error('unexpected GitHub call');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 1, 'short complete page permits absence conclusion');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({ total_count: 1, workflow_runs: [{}] }), { status: 200 });
  }
  throw new Error('malformed run data must not dispatch');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 0, 'malformed run data fails closed');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return githubReceipt('COMPLETED', SLOT);
  throw new Error('unexpected GitHub call');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 0, 'completed receipt must no-op');
  assert.equal(JSON.parse(env._store.get('tennis_scan_watchdog_v1')).last_action, 'receipt_completed');
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
  if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
    return new Response(JSON.stringify({
      total_count: 1,
      workflow_runs: [{ created_at: '2026-10-03T12:05:00Z', status: 'in_progress' }],
    }), { status: 200 });
  }
  throw new Error('unexpected dispatch');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 0, 'in-flight native run must no-op');
  const state = JSON.parse(env._store.get('tennis_scan_watchdog_v1'));
  assert.equal(state.last_action, 'unbound_temporal_in_flight_fail_closed');
  assert.equal(state.native_run_observed, false);
  assert.equal(state.native_run_canonical_slot_bound, false);
}

reset(async (url) => {
  if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 503 });
  throw new Error('unexpected GitHub call');
});
{
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.filter((call) => call.url.includes('/dispatches')).length, 0, 'GitHub uncertainty must fail closed');
  const state = JSON.parse(env._store.get('tennis_scan_watchdog_v1'));
  assert.equal(state.watchdog_healthy, false);
  assert.equal(state.last_action, 'fail_closed_no_dispatch');
}

reset(async () => { throw new Error('dispatch must not be retried'); });
{
  const env = makeEnv({ values: {
    tennis_scan_watchdog_v1: JSON.stringify({
      schema_version: 'tennis-scan-watchdog-v1',
      slots: { [SLOT]: { state: 'recovery_dispatched', dispatch_count: 1 } },
    }),
  } });
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(fetchCalls.length, 1, 'one receipt check only after dispatch marker');
}

for (const failure of ['throw', 'http']) {
  let dispatchAttempts = 0;
  reset(async (url) => {
    if (url.includes('/contents/results/tennis_scan_slots/')) return new Response('', { status: 404 });
    if (url.includes('/actions/workflows/tennis_scan.yml/runs')) {
      return new Response(JSON.stringify({ workflow_runs: [], total_count: 0 }), { status: 200 });
    }
    if (url.includes('/actions/workflows/tennis_scan.yml/dispatches')) {
      dispatchAttempts += 1;
      if (failure === 'throw') throw new TypeError('simulated dispatch timeout');
      return new Response(JSON.stringify({ message: 'simulated failure' }), { status: 502 });
    }
    throw new Error('unexpected GitHub call');
  });
  const env = makeEnv();
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  await worker.scheduled({ cron: '*/5 * * * *', scheduledTime: NOW }, env);
  assert.equal(dispatchAttempts, 1, `${failure} after durable marker must not retry`);
  const state = JSON.parse(env._store.get('tennis_scan_watchdog_v1'));
  assert.equal(state.slots[SLOT].dispatch_count, 1);
  assert.equal(state.slots[SLOT].state, 'dispatch_outcome_unknown');
  assert.equal(state.last_action, 'dispatch_outcome_unknown_no_retry');
  assert.equal(state.dispatch_outcome_unknown, true);
}

{
  const env = makeEnv({ values: {
    tennis_scan_watchdog_v1: JSON.stringify({ schema_version: 'tennis-scan-watchdog-v1', slots: {} }),
  } });
  const response = await worker.fetch(new Request('https://worker.test/scheduler/tennis', {
    headers: { Authorization: 'Bearer operator-token' },
  }), env);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.schema_version, 'tennis-scan-watchdog-v1');
  const denied = await worker.fetch(new Request('https://worker.test/scheduler/tennis'), env);
  assert.equal(denied.status, 401, 'watchdog status is not public');
}

console.log('Tennis watchdog tests passed');
