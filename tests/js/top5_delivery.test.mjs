import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const worker = await import(resolve(__dir, '../../cloudflare/worker.js'));

const TOP5_LEAGUES = ['EPL', 'BL1', 'LL', 'SA', 'L1'];
const RELEASE = {
  schema_version: 'top5-public-release-v1',
  release_type: 'CONTROLLED_TOP5',
  generation_id: 'top5-generation-v1:test',
  activation_state: 'CONTROLLED',
  activation_id: 'activation:test',
  publication_status: 'PUBLISHED',
  publication_enabled: true,
  publication_authorization_id: 'publication-auth:test',
  provider_authority: 'the_odds_api',
  result_authority: 'result-source',
  candidate_id: 'candidate:test',
  model_identity: 'model:test',
  evidence_digest: 'evidence:test',
  evidence_digests: Object.fromEntries(TOP5_LEAGUES.map((league) => [league, 'evidence:test'])),
  controlled_shadow_run_id: 'shadow:test',
  qualification_session_id: 'qualification:test',
  league_codes: TOP5_LEAGUES,
  generated_at: '2026-09-20T12:00:00Z',
  published_at: '2026-09-20T12:00:00Z',
  fallback_max_age_seconds: 7200,
  no_bet: true,
  owner: 'must-not-cross-public-boundary',
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
    evidence_digest: `evidence:${league}:${index}`,
    provenance: {
      activation_id: RELEASE.activation_id,
      evidence_digest: `evidence:${league}:${index}`,
    },
  };
}

const TOP5_RECORDS = TOP5_LEAGUES.flatMap((league) =>
  [0, 1, 2].map((index) => recordFor(league, index))
);

function payload(overrides = {}) {
  return {
    updated: '2026-09-20T12:00:00Z',
    top5_release: RELEASE,
    football: TOP5_RECORDS,
    ...overrides,
  };
}

describe('Worker Top-5 public release boundary', () => {
  test('preserves the governed release envelope and strips unknown fields', () => {
    const result = worker.serializePublicProduct({
      ...payload(),
      bankroll_state: { free: 999 },
    });
    assert.equal(result.top5_release.generation_id, RELEASE.generation_id);
    assert.equal(result.top5_release.owner, undefined);
    assert.equal(result.bankroll_state, undefined);
    assert.deepEqual(result.top5_release.league_codes, [...TOP5_LEAGUES].sort());
    assert.equal(result.football.length, 15);
  });

  test('rejects a staged or disabled release fail-closed', () => {
    assert.throws(() => worker.serializePublicProduct({
      top5_release: { ...RELEASE, publication_enabled: false },
    }), /published controlled no-bet release/);
    assert.throws(() => worker.serializePublicProduct({
      football: [{ league: 'EPL', signal_status: 'SHADOW' }],
    }), /controlled release envelope/);
  });

  test('rejects a release without the freshness contract', () => {
    const incomplete = { ...RELEASE };
    delete incomplete.generated_at;
    assert.throws(() => worker.serializePublicProduct(payload({
      top5_release: incomplete,
    })), /incomplete top5_release/);
  });

  test('rejects partial, duplicate, and unknown release league sets', () => {
    for (const league_codes of [
      ['EPL'],
      ['EPL', 'BL1', 'LL', 'SA'],
      ['EPL', 'BL1', 'LL', 'SA', 'SA'],
      ['EPL', 'BL1', 'LL', 'SA', 'L1', 'UCL'],
    ]) {
      assert.throws(() => worker.serializePublicProduct(payload({
        top5_release: { ...RELEASE, league_codes },
      })), /five Top-5 leagues|league_codes/);
    }
  });

  test('rejects incomplete record coverage and mixed fixtures', () => {
    assert.throws(() => worker.serializePublicProduct(payload({
      football: TOP5_RECORDS.slice(0, -1),
    })), /15|three records/);
    assert.throws(() => worker.serializePublicProduct(payload({
      football: TOP5_RECORDS.map((record, index) => index === 0
        ? { ...record, fixture_key: 'EPL:fixture:other' }
        : record),
    })), /one fixture/);
  });

  test('rejects candidate-provider authority and canonicalizes accepted aliases', () => {
    assert.throws(() => worker.serializePublicProduct(payload({
      top5_release: { ...RELEASE, provider_authority: 'therundown_experimental' },
    })), /provider authority/);
    const aliased = payload({
      top5_release: { ...RELEASE, league_codes: ['epl', 'bundesliga', 'la_liga', 'serie_a', 'ligue_1'] },
      football: TOP5_RECORDS.map((record) => ({
        ...record,
        league: ({ EPL: 'premier_league', BL1: 'bundesliga', LL: 'la_liga', SA: 'serie_a', L1: 'ligue_1' })[record.league],
      })),
    });
    const result = worker.serializePublicProduct(aliased);
    assert.deepEqual(result.top5_release.league_codes, ['BL1', 'EPL', 'L1', 'LL', 'SA']);
    assert.deepEqual(new Set(result.football.map((record) => record.league)), new Set(TOP5_LEAGUES));
  });
});
