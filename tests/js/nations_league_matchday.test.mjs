import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { orchestrateNationsLeagueMatchday } from '../../cloudflare/worker.js';

const SOURCE = JSON.parse(
  readFileSync(resolve('docs/data/signals.json'), 'utf8'),
).nations_league;

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    ).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return createHash('sha256').update(canonicalJson(value)).digest('hex');
}

function makeBundle({ nowMs = Date.now(), minutes = 90, phase = 'initial', fixtureId = 'nl:matchday-test' } = {}) {
  const fixture = structuredClone(SOURCE.fixtures[0]);
  const capturedAt = new Date(nowMs - 60_000).toISOString();
  const kickoff = new Date(nowMs + minutes * 60_000).toISOString();
  const recordId = 'a'.repeat(64);
  fixture.fixture_id = fixtureId;
  fixture.phase = phase;
  fixture.kickoff_utc = kickoff;
  fixture.prediction_cutoff = capturedAt;
  fixture.updated_at = capturedAt;
  fixture.source_prediction_record_id = recordId;
  fixture.edge_analysis.fixture_id = fixtureId;
  fixture.edge_analysis.phase = phase;
  fixture.edge_analysis.prediction_record_id = recordId;
  fixture.edge_analysis.evaluated_at = capturedAt;

  const bundle = {
    ...structuredClone(SOURCE),
    fixture_count: 1,
    fixtures: [fixture],
    audit_history: [{ fixture_id: fixtureId, source_prediction_record_ids: [recordId] }],
    updated_at: capturedAt,
  };
  delete bundle.public_digest;
  bundle.public_digest = digest(bundle);
  return bundle;
}

function makeKV(snapshot) {
  const store = new Map([['signals_json', JSON.stringify(snapshot)]]);
  return {
    async get(key) { return store.has(key) ? store.get(key) : null; },
    async put(key, value) { store.set(key, value); },
    async delete(key) { store.delete(key); },
    _store: store,
  };
}

function makeEnv(bundle, overrides = {}) {
  return {
    SIGNALS: makeKV({ nations_league: bundle }),
    GH_TOKEN: 'test-token-not-a-credential',
    GH_REPO: 'Philip3006/sportsbrain',
    ...overrides,
  };
}

function fakeDispatchers(calls, { failWorkflow = false } = {}) {
  return {
    workflowDispatch: async (...args) => {
      calls.push({ kind: 'workflow', args });
      if (failWorkflow) return new Response(null, { status: 503 });
      return new Response(null, { status: 204 });
    },
    repositoryDispatch: async (...args) => {
      calls.push({ kind: 'repository', args });
    },
  };
}

describe('Nations League matchday Worker orchestration', () => {
  test('dispatches the existing LIVE workflow once for an INITIAL fixture and deduplicates delivery', async () => {
    const nowMs = Date.now();
    const env = makeEnv(makeBundle({ nowMs, minutes: 120, phase: 'initial' }));
    const calls = [];
    const options = fakeDispatchers(calls);

    const first = await orchestrateNationsLeagueMatchday(env, nowMs, options);
    const second = await orchestrateNationsLeagueMatchday(env, nowMs, options);

    assert.equal(first.dispatched.length, 1);
    assert.equal(second.skipped.length, 1);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].kind, 'workflow');
    assert.equal(calls[0].args[1], 'nations_league_live_cycle.yml');
  });

  test('dispatches quote workflow at both bounded refinement windows with safety payload', async () => {
    const nowMs = Date.now();
    const calls = [];
    const options = fakeDispatchers(calls);
    const t60 = makeEnv(makeBundle({ nowMs, minutes: 60, phase: 'refinement', fixtureId: 'nl:t60' }));
    const t30 = makeEnv(makeBundle({ nowMs, minutes: 30, phase: 'refinement', fixtureId: 'nl:t30' }));

    await orchestrateNationsLeagueMatchday(t60, nowMs, options);
    await orchestrateNationsLeagueMatchday(t30, nowMs, options);

    assert.equal(calls.length, 2);
    assert.deepEqual(calls.map((call) => call.kind), ['repository', 'repository']);
    assert.equal(calls[0].args[1], 'sportsbrain_nations_league_bet_quote_launch');
    assert.equal(calls[0].args[2].window, 'T_MINUS_60');
    assert.equal(calls[1].args[2].window, 'T_MINUS_30');
    assert.equal(calls[0].args[2].no_bet, true);
    assert.equal(calls[0].args[2].publication, false);
    assert.equal(calls[0].args[2].production_activation, false);
    assert.equal(calls[0].args[2].betting, false);
  });

  test('accepts exact due-window boundaries and ignores a wrong phase', async () => {
    const boundaries = [
      ['initial', 120], ['initial', 60],
      ['refinement', 62], ['refinement', 58],
      ['refinement', 32], ['refinement', 28],
    ];
    for (const [phase, minutes] of boundaries) {
      const nowMs = Date.now();
      const calls = [];
      await orchestrateNationsLeagueMatchday(
        makeEnv(makeBundle({ nowMs, minutes, phase, fixtureId: `${phase}:${minutes}` })),
        nowMs,
        fakeDispatchers(calls),
      );
      assert.equal(calls.length, 1, `${phase} at ${minutes}m should dispatch`);
    }

    const nowMs = Date.now();
    const calls = [];
    await orchestrateNationsLeagueMatchday(
      makeEnv(makeBundle({ nowMs, minutes: 90, phase: 'refinement', fixtureId: 'nl:wrong-phase' })),
      nowMs,
      fakeDispatchers(calls),
    );
    assert.equal(calls.length, 0);
  });

  test('deletes a failed claim so the next cron delivery can retry', async () => {
    const nowMs = Date.now();
    const env = makeEnv(makeBundle({ nowMs, minutes: 90, phase: 'initial' }));
    const failedCalls = [];
    await assert.rejects(
      orchestrateNationsLeagueMatchday(env, nowMs, fakeDispatchers(failedCalls, { failWorkflow: true })),
      /workflow dispatch failed/,
    );
    assert.equal(env.SIGNALS._store.size, 1, 'only the trusted signals snapshot remains');

    const successfulCalls = [];
    const result = await orchestrateNationsLeagueMatchday(env, nowMs, fakeDispatchers(successfulCalls));
    assert.equal(result.dispatched.length, 1);
    assert.equal(successfulCalls.length, 1);
  });

  test('fails closed on missing state, missing token, tampered digest, and past kickoff', async () => {
    const nowMs = Date.now();
    await assert.rejects(
      orchestrateNationsLeagueMatchday({ SIGNALS: makeKV({}) }, nowMs, fakeDispatchers([])),
      /state is missing/,
    );

    await assert.rejects(
      orchestrateNationsLeagueMatchday(
        makeEnv(makeBundle({ nowMs, minutes: 90, phase: 'initial' }), { GH_TOKEN: undefined }),
        nowMs,
        fakeDispatchers([]),
      ),
      /GH_TOKEN not configured/,
    );

    const tampered = makeBundle({ nowMs, minutes: 90, phase: 'initial' });
    tampered.fixtures[0].fixture_id = 'nl:tampered';
    await assert.rejects(
      orchestrateNationsLeagueMatchday(makeEnv(tampered), nowMs, fakeDispatchers([])),
      /digest mismatch|malformed LIVE edge analysis/,
    );

    await assert.rejects(
      orchestrateNationsLeagueMatchday(
        makeEnv(makeBundle({ nowMs, minutes: -1, phase: 'initial' })),
        nowMs,
        fakeDispatchers([]),
      ),
      /digest|kickoff|invalid Nations League shadow/,
    );
  });
});

test('quote workflow accepts the Worker repository-dispatch event with keyed concurrency', () => {
  const workflow = readFileSync(resolve('.github/workflows/nations_league_bet_quote_launch.yml'), 'utf8');
  assert.match(workflow, /repository_dispatch:/);
  assert.match(workflow, /sportsbrain_nations_league_bet_quote_launch/);
  assert.match(workflow, /client_payload\.idempotency_key/);
  assert.match(workflow, /cancel-in-progress: true/);
});
