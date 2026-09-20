import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const worker = await import(resolve(__dir, '../../cloudflare/worker.js'));

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
  evidence_digests: { EPL: 'evidence:test' },
  controlled_shadow_run_id: 'shadow:test',
  qualification_session_id: 'qualification:test',
  league_codes: ['EPL'],
  generated_at: '2026-09-20T12:00:00Z',
  published_at: '2026-09-20T12:00:00Z',
  fallback_max_age_seconds: 7200,
  no_bet: true,
  owner: 'must-not-cross-public-boundary',
};

describe('Worker Top-5 public release boundary', () => {
  test('preserves the governed release envelope and strips unknown fields', () => {
    const result = worker.serializePublicProduct({
      updated: '2026-09-20T12:00:00Z',
      top5_release: RELEASE,
      bankroll_state: { free: 999 },
    });
    assert.equal(result.top5_release.generation_id, RELEASE.generation_id);
    assert.equal(result.top5_release.owner, undefined);
    assert.equal(result.bankroll_state, undefined);
  });

  test('rejects a staged or disabled release fail-closed', () => {
    assert.throws(() => worker.serializePublicProduct({
      top5_release: { ...RELEASE, publication_enabled: false },
    }), /published controlled no-bet release/);
    assert.throws(() => worker.serializePublicProduct({
      football: [{ league: 'EPL', signal_status: 'SHADOW' }],
    }), /controlled release envelope/);
  });
});
