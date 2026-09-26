import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const appSource = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');
const LEAGUES = ['EPL', 'BL1', 'LL', 'SA', 'L1'];
const NOW = Date.parse('2026-09-20T12:00:00Z');

function loadGuard() {
  const storage = new Map();
  const context = {
    console,
    Date,
    Error,
    JSON,
    Math,
    Number,
    Object,
    Promise,
    Set,
    String,
    URLSearchParams,
    decodeURIComponent,
    encodeURIComponent,
    isFinite,
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
    },
    document: {
      addEventListener() {},
      getElementById: () => null,
      querySelectorAll: () => [],
    },
    history: { replaceState() {} },
    location: { pathname: '/', search: '' },
    fetch: () => Promise.reject(new Error('fetch disabled in guard test')),
    setInterval: () => 0,
    clearInterval() {},
  };
  vm.createContext(context);
  vm.runInContext(appSource, context, { filename: 'docs/js/app.js' });
  return context._top5PublicReleaseGuard;
}

const RELEASE = {
  schema_version: 'top5-public-release-v1',
  generation_id: 'generation:test',
  activation_state: 'CONTROLLED',
  activation_id: 'activation:test',
  publication_status: 'PUBLISHED',
  publication_enabled: true,
  publication_authorization_id: 'publication-auth:test',
  provider_authority: 'the_odds_api',
  controlled_shadow_run_id: 'shadow:test',
  qualification_session_id: 'qualification:test',
  league_codes: LEAGUES,
  generated_at: '2026-09-20T12:00:00Z',
  published_at: '2026-09-20T12:00:00Z',
  fallback_max_age_seconds: 7200,
  no_bet: true,
};

function recordFor(league, index, fixture = `${league}:fixture:test`) {
  return {
    league,
    fixture_key: fixture,
    market: ['home', 'draw', 'away'][index],
    model_identity: 'model:test',
    prediction_timestamp: RELEASE.published_at,
    model_prob: 50,
    fair_prob: 40,
    activation_state: 'CONTROLLED',
    signal_status: 'CONTROLLED',
    publication_status: 'PUBLISHED',
    publication_enabled: true,
    no_bet: true,
    activation_id: RELEASE.activation_id,
    provider: RELEASE.provider_authority,
    run_id: RELEASE.controlled_shadow_run_id,
    session_id: RELEASE.qualification_session_id,
    signal_timestamp: RELEASE.published_at,
    evidence_digest: `evidence:${league}:${index}`,
    provenance: {
      activation_id: RELEASE.activation_id,
      evidence_digest: `evidence:${league}:${index}`,
    },
  };
}

const RECORDS = LEAGUES.flatMap((league) =>
  [0, 1, 2].map((index) => recordFor(league, index))
);

function lifecycleFor(record, { stage = 'INITIAL', version = 1, currentAt = RELEASE.published_at } = {}) {
  const initialAt = stage === 'INITIAL'
    ? currentAt
    : new Date(Date.parse(currentAt) - 5 * 60000).toISOString();
  const initialProbability = 0.5;
  const currentProbability = stage === 'REFINED' ? 0.52 : 0.5;
  const lifecycle = {
    schema_version: 'top5-lifecycle-public-v1',
    lifecycle_id: `life:${record.fixture_key}:${record.market}`,
    initial_record_id: `initial:${record.fixture_key}:${record.market}`,
    lifecycle_version: version,
    lifecycle_stage: stage,
    initial_generated_at: initialAt,
    current_generated_at: currentAt,
    initial_probability: initialProbability,
    current_probability: currentProbability,
    fixture_identity: record.fixture_key,
    model_identity: record.model_identity,
    provenance_binding: { evidence_digest: record.evidence_digest },
  };
  if (stage === 'REFINED') {
    lifecycle.probability_delta = currentProbability - initialProbability;
    lifecycle.refinement_classification = 'STRENGTHENED';
  }
  if (stage === 'WITHDRAWN') lifecycle.refinement_classification = 'WITHDRAWN';
  return lifecycle;
}

function payload(overrides = {}) {
  return {
    updated: '2026-09-20T12:00:00Z',
    top5_release: RELEASE,
    football: RECORDS,
    ...overrides,
  };
}

describe('browser Top-5 public guard', () => {
  test('accepts one complete governed five-league generation', () => {
    const guard = loadGuard();
    const result = guard(payload(), 'worker', NOW);
    assert.equal(result.football.length, 15);
  });

  test('Worker source rejects partial or incomplete generations', () => {
    const guard = loadGuard();
    for (const invalid of [
      { top5_release: { ...RELEASE, league_codes: ['EPL'] } },
      { top5_release: { ...RELEASE, league_codes: ['EPL', 'BL1', 'LL', 'SA'] } },
      { top5_release: { ...RELEASE, league_codes: ['EPL', 'BL1', 'LL', 'SA', 'SA'] } },
      { top5_release: { ...RELEASE, league_codes: [...LEAGUES, 'UCL'] } },
      { football: RECORDS.slice(0, -1) },
      { football: RECORDS.map((record, index) => index === 0
        ? { ...record, fixture_key: 'EPL:fixture:other' }
        : record) },
    ]) {
      assert.throws(() => guard({ ...payload(), ...invalid }, 'worker', NOW));
    }
  });

  test('static source drops invalid Top-5 material fail-closed', () => {
    const guard = loadGuard();
    const result = guard({
      ...payload(),
      top5_release: { ...RELEASE, league_codes: ['EPL'] },
    }, 'static', NOW);
    assert.equal(result.top5_release, undefined);
    assert.equal(result.football.length, 0);
  });

  test('rejects future and candidate-provider releases', () => {
    const guard = loadGuard();
    assert.throws(() => guard({
      ...payload(),
      top5_release: { ...RELEASE, provider_authority: 'therundown_experimental' },
    }, 'worker', NOW));
    assert.throws(() => guard({
      ...payload(),
      top5_release: { ...RELEASE, published_at: '2026-09-20T13:00:00Z' },
    }, 'worker', NOW));
  });

  test('rejects malformed or stale signal provenance even when the container is fresh', () => {
    const guard = loadGuard();
    assert.throws(() => guard({
      ...payload(),
      football: RECORDS.map((record, index) => index === 0
        ? { ...record, signal_timestamp: 'not-a-timestamp' }
        : record),
    }, 'worker', NOW));
    assert.throws(() => guard({
      ...payload(),
      football: RECORDS.map((record, index) => index === 0
        ? { ...record, stale_state: 'STALE' }
        : record),
    }, 'worker', NOW));
  });

  test('normalizes accepted Top-5 aliases before rendering', () => {
    const guard = loadGuard();
    const aliases = { EPL: 'premier_league', BL1: 'bundesliga', LL: 'la_liga', SA: 'serie_a', L1: 'ligue_1' };
    const result = guard({
      ...payload(),
      top5_release: { ...RELEASE, league_codes: Object.values(aliases) },
      football: RECORDS.map((record) => ({ ...record, league: aliases[record.league] })),
    }, 'worker', NOW);
    assert.deepEqual(new Set(result.football.map((record) => record.league)), new Set(LEAGUES));
  });

  test('accepts each supported lifecycle stage without changing release authority', () => {
    for (const stage of ['INITIAL', 'REFINED', 'WITHDRAWN']) {
      const guard = loadGuard();
      const records = RECORDS.map((record, index) => index === 0
        ? {
          ...record,
          ...(stage === 'REFINED' ? { model_prob: 52 } : {}),
          lifecycle: lifecycleFor(record, { stage, version: stage === 'INITIAL' ? 1 : 2 }),
        }
        : record);
      const result = guard(payload({ football: records }), 'worker', NOW);
      assert.equal(result.top5_release.provider_authority, 'the_odds_api');
      assert.equal(result.football[0].lifecycle.lifecycle_stage, stage);
    }
  });

  test('rejects malformed lifecycle, authority leakage, active withdrawal, and missing release authorization', () => {
    const guard = loadGuard();
    const valid = lifecycleFor(RECORDS[0]);
    for (const lifecycle of [
      { ...valid, lifecycle_version: 0 },
      { ...valid, current_generated_at: 'not-a-time' },
      { ...valid, provider_authority: 'candidate-provider' },
      { ...valid, provenance_binding: { provider_authority: 'therundown_experimental' } },
      { ...valid, lifecycle_id: '' },
    ]) {
      const records = RECORDS.map((record, index) => index === 0 ? { ...record, lifecycle } : record);
      assert.throws(() => guard(payload({ football: records }), 'worker', NOW), /lifecycle/);
    }
    const badDelta = lifecycleFor(RECORDS[0], { stage: 'REFINED', version: 2 });
    badDelta.probability_delta = 0.9;
    assert.throws(() => guard(payload({
      football: RECORDS.map((record, index) => index === 0 ? { ...record, lifecycle: badDelta } : record),
    }), 'worker', NOW), /probability_delta/);

    const withdrawn = lifecycleFor(RECORDS[0], { stage: 'WITHDRAWN', version: 2 });
    assert.throws(() => guard(payload({
      football: RECORDS.map((record, index) => index === 0
        ? { ...record, signal_status: 'ACTIVE', lifecycle: withdrawn } : record),
    }), 'worker', NOW), /active recommendation/);

    const authorizedShape = RECORDS.map((record, index) => index === 0
      ? { ...record, lifecycle: valid } : record);
    assert.throws(() => guard({ football: authorizedShape }, 'worker', NOW), /authorized and published/);
  });

  test('collapses a consistent INITIAL→REFINED chain to one public signal', () => {
    const guard = loadGuard();
    const initialAt = new Date(NOW - 60000).toISOString();
    const initial = {
      ...RECORDS[0],
      prediction_timestamp: initialAt,
      signal_timestamp: initialAt,
      lifecycle: lifecycleFor(RECORDS[0], { currentAt: initialAt }),
    };
    const refined = {
      ...RECORDS[0],
      prediction_timestamp: new Date(NOW).toISOString(),
      signal_timestamp: new Date(NOW).toISOString(),
      model_prob: 52,
      lifecycle: {
        ...lifecycleFor(RECORDS[0], { stage: 'REFINED', version: 2, currentAt: new Date(NOW).toISOString() }),
        initial_generated_at: initialAt,
      },
    };
    const records = [...RECORDS];
    records[0] = initial;
    records.push(refined);
    const result = guard(payload({ football: records }), 'worker', NOW);
    assert.equal(result.football.length, 15);
    assert.equal(result.football.filter((record) => record.lifecycle?.lifecycle_id === refined.lifecycle.lifecycle_id).length, 1);
    assert.equal(result.football.find((record) => record.lifecycle?.lifecycle_id === refined.lifecycle.lifecycle_id).lifecycle.lifecycle_version, 2);
  });

  test('remembers version monotonicity and rejects an older replacement', () => {
    const guard = loadGuard();
    const firstRecords = [...RECORDS];
    firstRecords[0] = { ...RECORDS[0], lifecycle: lifecycleFor(RECORDS[0]) };
    guard(payload({ football: firstRecords }), 'worker', NOW);

    const nextAt = new Date(NOW + 60000).toISOString();
    const nextRelease = {
      ...RELEASE,
      generation_id: 'generation:next',
      generated_at: nextAt,
      published_at: nextAt,
    };
    const newerRecords = [...RECORDS];
    newerRecords[0] = {
      ...RECORDS[0],
      model_prob: 52,
      prediction_timestamp: nextAt,
      signal_timestamp: nextAt,
      lifecycle: lifecycleFor(RECORDS[0], { stage: 'REFINED', version: 2, currentAt: nextAt }),
    };
    guard(payload({ football: newerRecords, top5_release: nextRelease, updated: nextAt }), 'worker', NOW + 60000);

    const regressedAt = new Date(NOW + 120000).toISOString();
    const regressedRecords = [...RECORDS];
    regressedRecords[0] = { ...RECORDS[0], lifecycle: lifecycleFor(RECORDS[0]) };
    assert.throws(() => guard(payload({
      football: regressedRecords,
      top5_release: { ...RELEASE, generation_id: 'generation:regressed', published_at: regressedAt, generated_at: regressedAt },
      updated: regressedAt,
    }), 'worker', NOW + 120000), /version regressed/);
  });

  test('static lifecycle failures still drop Top-5 and lifecycle fields are projected', () => {
    const guard = loadGuard();
    const invalid = { ...lifecycleFor(RECORDS[0]), initial_record_id: '' };
    const result = guard(payload({
      football: RECORDS.map((record, index) => index === 0 ? { ...record, lifecycle: invalid } : record),
    }), 'static', NOW);
    assert.equal(result.top5_release, undefined);
    assert.equal(result.football.length, 0);
  });
});
