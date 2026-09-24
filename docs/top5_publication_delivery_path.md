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
| source/runtime identity | accepted release manifest | `source_release_sha`, `runtime_data_sha`, `source_runtime_consistent` | same release fields | Machine-readable binding; not rendered as user-facing text |
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

The Worker deliberately serves the complete governed bundle rather than
creating separately cached per-league documents. The PWA's keyboard-accessible
Top-5 filter selects `EPL`, `BL1`, `LL`, `SA`, or `L1` locally after the
complete bundle passes validation. This preserves one generation/digest across
all public readers and prevents a partial or empty league response from being
mistaken for a complete release.

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

## Public-read acceptance and precheck

`src/football/top5_public_acceptance.py` is the single B3 read-only contract
for a generated public bundle. It verifies the exact five canonical leagues,
15-record coverage, one fixture per league, controlled/no-bet bindings,
`the_odds_api` authority, record-level freshness, future timestamps,
provenance digests, and test/candidate isolation. It returns machine-readable
codes such as `PUBLIC_SCHEMA_INVALID`, `PUBLIC_ARTIFACT_STALE`,
`PUBLIC_PROVENANCE_INVALID`, `PUBLIC_LEAGUE_INCOMPLETE`,
`PUBLIC_CANDIDATE_AUTHORITY_LEAK`, and `PUBLIC_TEST_DATA_REJECTED`.

Offline contract validation is intentionally distinct from production
eligibility. A `TEST/OFFLINE` fixture can return
`TOP5_PUBLIC_DELIVERY_READY` with `production_eligible: false`; it can never
satisfy the real-evidence or Builder 1 acceptance boundary.

```text
python3 scripts/top5_public_acceptance.py accept \
  --bundle /absolute/input/top5-public-bundle.json \
  --now 2026-09-20T12:00:00Z

python3 scripts/top5_public_acceptance.py precheck \
  --bundle /absolute/input/top5-public-bundle.json \
  --accepted-evidence /absolute/input/builder1-acceptance.json \
  --delivery-manifest /absolute/input/top5-delivery-manifest.json \
  --now 2026-09-20T12:00:00Z
```

The Builder 1 interface consumed by `precheck` is deliberately narrow:
`schema_version=top5-final-acceptance-v1`, `status=ACCEPTED`,
`publication_ready=true`, `provider_authority=the_odds_api`, and bound
`generation_id`, `activation_id`, `source_release_sha`, `runtime_data_sha`,
and `evidence_digest`. B3 does not recompute or replace Builder 1's evidence
engine. Builder 2 supplies the runtime artifact and lifecycle state consumed
by the upstream accepted manifest; disabled/degraded/incomplete runtime state
therefore blocks the precheck rather than being published as a fallback.

The delivery-manifest interface consumed by `precheck` combines the existing
`Top5DeliveryPlan.manifest()` with the read-only dry-run result. It must carry
the exact `public_product_digest`, `static_payload_digest`,
`worker_payload_digest`, `generation_id`, and `activation_id`, plus
`dry_run_status` of `TOP5_DELIVERY_DRY_RUN` (or an already-idempotent result)
and `rollback_ready=true`. This proves that both public targets are prepared
from the same bytes and that the prior safe version remains recoverable; it
does not publish or deploy either target.

The source/runtime binding is carried as `source_release_sha` and
`runtime_data_sha`; bot-generated data commits must not be substituted for the
source release identity. The first production precheck requires both fields
and `source_runtime_consistent=true` when the accepted manifest is available.

After authorized publication, the separate read-only verifier
`scripts/top5_public_production_verification.py` can compare anonymous Worker
and static GET captures. It requires HTTP success, exact generation and
source/runtime identity agreement, the same public bundle digest, and the
PWA status supplied by the operator. Its success token is
`TOP5_PUBLIC_PRODUCTION_VERIFIED`; it never repairs or mutates a target.

The existing `scripts/top5_production_verification.py` remains the read-only
post-activation runtime verifier. After the first authorized publication, its
captured evidence and this public-read precheck must both pass before the
release is considered verified. No command in this section performs a live
publication, Worker deployment, scheduler change, provider request, ledger
mutation, or betting action.
