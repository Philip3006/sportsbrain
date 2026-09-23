import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const worker = await import(resolve(__dir, '../../cloudflare/worker.js'));
const contract = await import(resolve(__dir, '../../cloudflare/cl_publication_contract.js'));

const NOW = Date.now();
const CAPTURED_AT = new Date(NOW - 30_000).toISOString();
const DIGESTS = {
  source_sha: 'a'.repeat(64),
  research_sha: 'b'.repeat(64),
  model_artifact_hash: 'c'.repeat(64),
  evidence_digest: 'd'.repeat(64),
};

function record(market, league = 'UCL') {
  return {
    sport: 'football',
    league,
    fixture_key: 'ucl:offline:fixture-001',
    match: 'Home FC vs Away FC',
    home: 'Home FC',
    away: 'Away FC',
    kickoff: new Date(NOW + 3_600_000).toISOString(),
    prediction_id: 'ucl-prediction-001',
    model_identity: 'cl-model-v1',
    prediction_timestamp: CAPTURED_AT,
    source: 'offline-fixture',
    provider: 'offline-fixture',
    activation_state: 'SHADOW',
    publication_status: 'UNPUBLISHED',
    publication_enabled: false,
    no_bet: true,
    signal_status: 'SHADOW',
    result_status: 'PENDING',
    settlement_status: 'PENDING',
    stale_state: 'FRESH',
    market,
    provenance: {
      ...DIGESTS,
      source: 'offline-fixture',
      provider: 'offline-fixture',
      snapshot_id: 'ucl-offline-snapshot-001',
      snapshot_kind: 'signal_time',
      captured_at: CAPTURED_AT,
      source_age_seconds: 30,
    },
  };
}

function payload() {
  return {
    updated: CAPTURED_AT,
    football: ['home', 'draw', 'away'].map((market) => record(market)),
    champions_league_release: {
      schema_version: 'champions-league-publication-v1',
      competition: 'UEFA Champions League',
      league_code: 'UCL',
      generation_id: 'ucl-offline-generation-001',
      activation_state: 'SHADOW',
      publication_status: 'UNPUBLISHED',
      publication_enabled: false,
      provider_authority: 'offline-fixture',
      result_authority: 'offline-result-fixture',
      ...DIGESTS,
      prediction_count: 1,
      fixture_count: 1,
      generated_at: CAPTURED_AT,
      stale_after_seconds: 900,
      no_bet: true,
    },
    health: {
      football_releases: [{
        schema_version: 'football-release-health-v1',
        league: 'UCL',
        publication_status: 'UNPUBLISHED',
        stale_artifact: false,
        missing_result_count: 1,
        settlement_status: 'PENDING',
        source_age_seconds: 30,
        source: 'offline-fixture',
        provider: 'offline-fixture',
        source_sha: DIGESTS.source_sha,
        activation_state: 'SHADOW',
        no_bet: true,
        publication_enabled: false,
        observed_at: CAPTURED_AT,
      }],
    },
  };
}

describe('Champions League public publication contract', () => {
  test('Worker public serializer accepts the complete offline CL envelope', () => {
    const result = worker.serializePublicProduct(payload());
    assert.equal(result.champions_league_release.league_code, 'UCL');
    assert.equal(result.football.length, 3);
    assert.deepEqual(
      contract.validateChampionsLeaguePublicProduct(result, { requireHealth: true }),
      {
        schema_version: 'champions-league-publication-v1',
        league_code: 'UCL',
        prediction_count: 1,
        fixture_count: 1,
        publication_enabled: false,
        publication_status: 'UNPUBLISHED',
      },
    );
  });

  test('missing release or provenance fails closed', () => {
    const withoutRelease = payload();
    delete withoutRelease.champions_league_release;
    assert.throws(() => worker.serializePublicProduct(withoutRelease), /CL release envelope/);

    const withoutEvidence = payload();
    delete withoutEvidence.football[0].provenance.evidence_digest;
    assert.throws(() => worker.serializePublicProduct(withoutEvidence), /evidence_digest/);
  });

  test('CL records cannot become actionable in the public boundary', () => {
    const actionable = payload();
    actionable.football[0].signal_status = 'ACTIVE';
    assert.throws(() => worker.serializePublicProduct(actionable), /actionable/);

    const bettingEnabled = payload();
    bettingEnabled.football[0].no_bet = false;
    assert.throws(() => worker.serializePublicProduct(bettingEnabled), /no-bet/);
  });

  test('aliases are accepted for records but canonicalized in release metadata', () => {
    const aliased = payload();
    aliased.football = aliased.football.map((item) => ({ ...item, league: 'champions_league' }));
    const result = worker.serializePublicProduct(aliased);
    assert.equal(result.champions_league_release.league_code, 'UCL');
    assert.equal(result.football[0].league, 'UCL');
    assert.equal(result.champions_league_release.activation_state, 'SHADOW');
    assert.equal(result.champions_league_release.publication_enabled, false);
    assert.equal(result.champions_league_release.no_bet, true);
  });

  test('published stale records are rejected by the freshness gate', () => {
    const stale = payload();
    stale.champions_league_release.activation_state = 'CONTROLLED';
    stale.champions_league_release.publication_status = 'PUBLISHED';
    stale.champions_league_release.publication_enabled = true;
    stale.champions_league_release.publication_authorization_id = 'offline-test-authorization';
    stale.football = stale.football.map((item) => ({
      ...item,
      activation_state: 'CONTROLLED',
      publication_status: 'PUBLISHED',
      publication_enabled: true,
      signal_status: 'CONTROLLED',
      stale_state: 'STALE',
    }));
    stale.health.football_releases[0].publication_status = 'PUBLISHED';
    stale.health.football_releases[0].activation_state = 'CONTROLLED';
    stale.health.football_releases[0].publication_enabled = true;
    assert.throws(
      () => contract.validateChampionsLeaguePublicProduct(stale, {
        nowMs: NOW,
        maxAgeSeconds: 900,
        requireHealth: true,
      }),
      /stale/,
    );
  });
});
