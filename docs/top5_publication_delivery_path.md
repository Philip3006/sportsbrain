# Top-5 public delivery path

This document describes the narrow delivery bridge on top of the existing
SportsBrain public product. It does not grant provider authority, activate a
model, issue a qualification receipt, or publish an artifact.

## Canonical path

`ControlledTop5PublicationBatch` validates exactly one record for each of
`EPL`, `BL1`, `LL`, `SA`, and `L1`. It builds one `top5_release.generation_id`
and one public product, then an injected publication store swaps that product
as one pointer. `serialize_public_product()` emits the existing `football`
array. The Worker applies its matching public allowlist at `GET /signals.json`.
The phone PWA tries the Worker first and the static
`docs/data/signals.json` snapshot second; the service worker has no fetch/cache
handler for either signal source.

The existing single-league controlled publisher remains supported. The batch
bridge is the required path when a five-league generation is delivered.

## Canonical delivery adapter

`Top5CanonicalDeliveryAdapter.build_plan()` is the governed seam between the
already authorized `PublishedTop5BatchArtifact` and the existing public
product. It removes only the prior `EPL`, `BL1`, `LL`, `SA`, and `L1` football
records, preserves the other football leagues and unrelated public keys, then
adds the one validated batch release. A conflicting existing `top5_release`,
mixed bindings, missing league, stale artifact, malformed output, or
non-no-bet/staged release fails closed.

The adapter calls `serialize_public_product()` once. Its immutable
`serialized_payload` and `public_product_digest` are reused as both the
static `docs/data/signals.json` staging input and the Worker `/signals`
staging input. The adapter itself has no filesystem or network writer.

The guarded executor is the only execution seam. `prepare` is always
read-only and emits a signed-input-free plan/manifest. `execute` is also a
dry-run unless the explicit `--execute` flag is supplied. Real execution
requires the detached `Top5DeliveryAttestation`, an operator-owned
`ControlledPublicationCapability` token, and the existing one-time capability
state containing the matching `ControlledPublicationAttestation`; the file
consumer validates and consumes that capability immediately before the first
delivery mutation. Secrets are never printed or written to the plan.

The bounded state sequence is
`PREPARED → STATIC_STAGED → WORKER_WRITTEN → STATIC_COMMITTED →
ACCEPTANCE_REQUIRED`. The executor rechecks the canonical snapshot and both
current delivery-target digests before consuming capability state. It stages
through `scripts/publish_runtime_artifacts.sh publish-staged`, writes the
Worker `/signals` body as the exact same bytes, and stops for the existing
read-only acceptance command. Static-stage or Worker failures clean up the
stage; a static-commit failure attempts Worker restoration and reports
`ROLLBACK_REQUIRED` even if restoration succeeds. There is no distributed
atomicity claim and no unbounded retry. Re-running the same generation,
activation, and digest is idempotent; a conflicting or newer generation is
rejected.

Example commands (all paths are absolute in operational use):

```text
python3 scripts/top5_public_delivery.py prepare \
  --artifact /absolute/input/published-top5-artifact.json \
  --current-snapshot /absolute/input/current-signals.json \
  --plan-output /absolute/output/top5-delivery-plan.json

python3 scripts/top5_public_delivery.py execute \
  --artifact /absolute/input/published-top5-artifact.json \
  --current-snapshot /absolute/input/current-signals.json \
  --plan /absolute/output/top5-delivery-plan.json \
  --attestation /absolute/input/top5-delivery-attestation.json \
  --capability-token /absolute/operator/top5-capability.json \
  --now 2026-01-01T00:00:00+00:00

python3 scripts/top5_public_delivery.py execute \
  --artifact /absolute/input/published-top5-artifact.json \
  --current-snapshot /absolute/input/current-signals.json \
  --plan /absolute/output/top5-delivery-plan.json \
  --attestation /absolute/input/top5-delivery-attestation.json \
  --capability-token /absolute/operator/top5-capability.json \
  --execute \
  --active-checkout /absolute/active/sportsbrain \
  --stage-directory /absolute/stage/top5-generation \
  --runtime-log /absolute/logs/top5-delivery.log \
  --worker-url https://example.invalid/signals.json
```

The final command is intentionally the only path that can invoke the
isolated runtime publisher or Worker transport. It remains subject to the
separate CEO publication authorization and is not run by tests or CI.

## Field compatibility matrix

| Contract field | Publisher input | Public football record | `top5_release` / health | PWA use |
|---|---|---|---|---|
| league | `league_code`, record `league` | `league` | `league_codes` | Football grouping/filter |
| sport | implicit football | `sport=football` | — | Football UI |
| fixture/match | `fixture` / `fixture_key` | `fixture_key`, `match`, `home`, `away` | generation binds record IDs | Card identity |
| kickoff | fixture `kickoff` | `kickoff` | — | Schedule/card display |
| signal identity | `prediction_id` or stable derived ID | `signal_id`, `prediction_id` | generation record seed | PWA/bet identity; controlled is not actionable |
| model identity | `model_identity` | `model_identity`, `model_version` | `model_identity` | Compatibility metadata |
| probabilities | `probabilities` | `model_prob`, `fair_prob` | — | Card display |
| market | outcome probabilities/odds | `market` | — | Home/draw/away rows |
| current odds | record `odds` | `odds`, `current_odds` | — | Canonical current-odds contract |
| source timestamp | `signal_timestamp` / `captured_at` | `signal_timestamp`, `odds_ts` | `generated_at`, `published_at` | Freshness display/guard |
| freshness | source age/health | `source_age_seconds`, `stale_state` | fallback age limit | Stale guard |
| activation state | controlled activation binding | `activation_state=CONTROLLED` | `activation_state` | Must not become ACTIVE |
| publication state | separate publication authorization | `publication_status`, `publication_enabled` | `PUBLISHED` plus auth ID | PWA acceptance gate |
| provider provenance | `provider_authority` | `provider`, `source`, `provenance.provider` | `provider_authority` | Exact binding; no provider widening |
| evidence digest | per-league `evidence_digest` | `evidence_digest`, provenance copy | aggregate and `evidence_digests` | Audit/reconciliation |
| no_bet | required true | `no_bet=true`, `no_bet_flag` | `no_bet=true` | Prevents actionability |
| Controlled Shadow run | `controlled_shadow_run_id` | explicit field and `run_id` | same binding | Audit/reconciliation |
| qualification session | `qualification_session_id` | explicit field and `session_id` | same binding | Audit/reconciliation |

The three real compatibility losses fixed here were the missing evidence and
activation identifiers on public records, the missing `current_odds` /
`current_ev_pct` aliases, and the absence of a generation/fallback fence.

## Fallback and cache safety

`top5_release` is reconstructed through an explicit allowlist in Python and
the Worker. A payload containing Top-5 records is accepted by the PWA only
when it is controlled, published, no-bet, authorization-bound, fresh, and
record-bound to the same activation/provider/run/session. A static fallback
with invalid, stale, or regressed Top-5 metadata has its Top-5 records removed
while unrelated Tennis, Bundesliga 2, WM, and other public records remain
usable. The last accepted generation is kept in local storage to reject an
older static snapshot after a newer Worker generation was observed.

The browser uses `cache: 'no-store'` and a timestamp query on both public
requests. `docs/sw.js` intentionally has no `fetch` handler and therefore does
not cache the signal payload. A failed publication/verification must restore
the prior safe single snapshot (or an unpublished snapshot) rather than write
a partial league set.

## Read-only post-publication acceptance

Capture the two HTTP 200 response bodies and the separately issued publication
attestation, then run:

```text
python3 scripts/top5_publication_delivery_acceptance.py \
  --worker-payload /absolute/capture/worker-signals.json \
  --static-payload /absolute/capture/static-signals.json \
  --publication-attestation /absolute/capture/publication-attestation.json \
  --delivery-manifest /absolute/capture/top5-delivery-manifest.json \
  --expected-provider the_odds_api \
  --worker-status 200 \
  --pwa-status 200
```

The command is read-only and returns `TOP5_DELIVERY_VERIFIED` only when both
responses carry the same generation and activation, their canonical public
product digest matches the immutable delivery manifest, all five leagues are
visible, records are no-bet controlled records, provenance is intact, and the
separate publication attestation matches. Any mismatch returns
`TOP5_DELIVERY_BLOCKED`; it never repairs, publishes, activates, or rolls back
state.

The offline fixture at
`tests/fixtures/top5/publication_delivery_offline.json` is explicitly
`TEST/OFFLINE` / `TEST_FIXTURE`. It exercises publisher staging through the
serializer and frontend-compatible shape, and is structurally unable to
become `REAL_OBSERVED`, controlled, or published.
