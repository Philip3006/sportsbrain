# Top-5 controlled activation runbook

Status: prepared for future CEO-authorized use only. The current repository
state remains disabled, no-bet, unpublished, and unregistered. The runbook is
an evidence checklist; it does not authorize a run.

The only approved name for a manually authorized execution is **CONTROLLED
ACTIVATION RUNS**. Do not call these runs a natural canary or a natural
scheduled run.

## Prerequisites

- Confirm the exact production source SHA and the frozen Research SHA.
- The currently referenced frozen Research SHA is
  `6eaabbec7d0182103d815c72fae4976e261b40aa`; it is not modified here.
- Confirm the exact model artifact hash and candidate/model identity.
- Confirm the league-scoped provider authority for fixtures, odds, and results.
- Confirm the approved signal-time contract, markets, regions, cadence, and
  retry budget.
- Confirm the cumulative rollout evidence through CEO approval.
- Confirm the immutable configuration snapshot and rollback pointer.
- Confirm that the model adapter, provider client, scheduler, publisher, and
  ledger boundaries are the intended explicitly injected dependencies.

## CEO gate

The CEO authorization must identify the league, candidate, model artifact,
source SHA, Research SHA, provider authority, signal-time contract, rollback
pointer, and exact scope. A blank, stale, or mismatched authorization fails
closed. Authorization is not inferred from a passing shadow metric.

## Preflight

1. Validate the cumulative state machine:
   `research_approved` → `adapter_ready` → `offline_compatible` →
   `shadow_inference` → `signal_time_validated` → `provider_validated` →
   `shadow_performance_validated` → `ceo_approved`.
2. Validate source, Research, and model hashes.
3. Validate league/provider/signal-time identity.
4. Confirm closing odds are excluded from inference.
5. Confirm publication is disabled until its separate policy is approved.
6. Confirm no-bet behavior, rollback readiness, and external runtime-state
   ownership.

The prepared plan must remain an in-memory `PreparedActivation` with
`executed=false`. The current readiness harness rejects execution.

## Activation

If and only if a future implementation is separately authorized, activate one
league and one candidate/model scope from the deterministic snapshot. Record
the exact source SHA, Research SHA, model hash, provider authority,
signal-time contract, and rollback pointer before the first production cycle.

Do not expand league scope, change cadence, bind a different model, add a
provider, or enable publication in the same run.

## Verification

- Verify the selected league and fixture identity.
- Verify signal-time snapshots are fresh and signal-relative.
- Verify inference inputs contain no closing snapshot.
- Verify every prediction has complete provenance.
- Verify output remains no-bet until the separately approved policy says
  otherwise.
- Verify publisher and PWA contracts expose only the approved Top-5
  namespace.

## Health checks

Record league health, provider health, fixture and signal-time coverage, odds
freshness, inference health and latency, publisher health, result-source
health, fallback/retry/stale/duplicate rates, quota/cost usage, last
successful cycle, and activation state.

## Rollback

Any provider failure, stale market, inference error, malformed prediction,
publication error, scheduler error, health degradation, duplicate signal,
wrong league mapping, or model provenance mismatch invokes the rollback
controller. The required result is the last known safe disabled state:

- activation mode `disabled`;
- no-bet `true`;
- publication `false`;
- scheduler `false`;
- ledger mutation `false`.

Rollback is a state decision and evidence record. It does not delete files,
reset Git, or mutate the financial ledger.

## Production verification and evidence capture

Capture the preflight payload, authorization reference, source/Research/model
hashes, provider and timing configuration, health payload, prediction and
signal provenance, publisher/PWA validation, rollback result, and the final
CEO review record. Keep closing-odds and CLV-style artifacts benchmark-only.
