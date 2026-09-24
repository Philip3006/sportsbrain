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
});
