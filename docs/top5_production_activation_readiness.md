# Top-5 production activation readiness

## Current status

Production-side readiness is implemented as offline, deterministic contracts
and simulations from merged PR #56 (`8de9472644057656c50d900380a843909ff5a46b`).
The frozen Research reference remains `6eaabbec7d0182103d815c72fae4976e261b40aa`.
The five leagues remain disabled, no-bet, unpublished, and absent from the
active league registry. Builder 1's shadow integration and the frozen Research
model are outside this branch.

## What is ready

- provider-validation scenarios with independent fixture, odds, and result
  authorities and injected failure outcomes;
- request/cost V3 planning across matchday, 24-hour, 72-hour, weekly, and
  five-league horizons;
- event-relative signal-time candidate comparisons with no automatic winner;
- shadow-performance metrics and explicit operational gates;
- a cumulative ten-stage rollout state machine;
- a future controlled activation envelope and fail-closed preflight;
- rollback-to-disabled contracts for every required failure class;
- disabled Top-5 publisher, PWA, and health/observability contracts;
- controlled activation and production verification runbooks.

## Future activation chain audit

The complete future chain is:

`fixture discovery` → `signal-time eligibility` → `provider request` →
`snapshot validation` → `feature/model inference` → `shadow artifact` →
`shadow evaluation` → `CEO approval` → `controlled activation` → `publication`
→ `settlement` → `monitoring` → `rollback`.

The current readiness branch supplies only the injected/offline boundaries,
evidence, and runbooks. Fixture/result authority, provider authority, model
binding, signal-time numeric values, shadow observation duration, publication,
settlement, scheduler, and rollback execution remain unresolved or disabled.

## What remains disabled

No model is bound. No provider client is imported or called. No live authority
is selected. No scheduler, launchd job, Cloudflare Worker, publisher, ledger,
or runtime-state writer is registered. Closing odds are benchmark-only and are
structurally excluded from signal-time inference.

## Provider-validation framework

`top5_provider_validation.py` covers BL1, EPL, LL, SA, and L1 with separate
fixture, odds, and result authority fields. Authorities default to unset.
The offline simulator reports bulk odds requests, per-event fallback requests,
result requests, retries, latency, coverage, stale rejection, result delay,
HTTP 403/429, timeout, empty, malformed, unsupported-market, partial,
missing-fixture, wrong-league, and fail-closed outcomes.

The normal bulk route is `/sports/{sport}/odds`. Event fallback is modeled as
`/sports/{sport}/events/{event_id}/odds`. No route is invoked by this branch.

## Quota/cost framework

`QuotaAssumptions` and `QuotaPlannerV3` report independently:

1. fixtures;
2. logical evaluations;
3. bulk odds HTTP requests;
4. event fallback HTTP requests;
5. result requests;
6. revalidation requests;
7. closing-capture requests;
8. raw HTTP requests; and
9. injected provider cost units.

Cadence, retry count, fallback rates, signal-time passes, bulk reuse, batch
size, markets, regions, and cost weights are caller-supplied. These values
are planning assumptions, not vendor billing claims or a quota purchase.

## Signal-time candidates

`top5_signal_time_matrix.py` compares named event-relative candidates by
coverage proxy, request count, fallback frequency, stale-rejection risk,
latency budget, cost units, and operational complexity. It returns no selected
candidate. Numeric production timing remains a CEO decision.

## Shadow-performance measurement and gates

`top5_shadow_performance.py` measures fixture/inference coverage, valid
prediction rate, signal-time success, freshness, provider/inference latency,
retries, fallbacks, stale rejection, duplicate suppression, candidate
availability, probability quality, calibration, market comparison, and
closing/CLV-style benchmark diagnostics. Closing snapshots never enter a
prediction input.

Gates cover minimum coverage, stale/provider/duplicate/invalid/inference error
limits, provenance, no-bet, closing exclusion, and cross-league isolation.
There is deliberately no profitability threshold.

## Rollout state machine

The required cumulative sequence is:

`research_approved` → `adapter_ready` → `offline_compatible` →
`shadow_inference` → `signal_time_validated` → `provider_validated` →
`shadow_performance_validated` → `ceo_approved` → `controlled_activation` →
`production_verified`.

Missing predecessors fail closed. Controlled activation requires the evidence
through CEO approval. Production verification additionally requires an
explicit controlled-activation state.

## Controlled activation and rollback

`ControlledActivationRequest` requires explicit CEO authorization, league and
candidate scope, source/Research/model hashes, provider authority, signal-time
contract, deterministic configuration snapshot, and rollback pointer.
`ControlledActivationHarness` can prepare and validate a future plan but its
execution method is disabled. `RollbackController` returns the safe disabled,
no-bet, unpublished, non-scheduled, ledger-untouched state for every trigger.

## Publisher, PWA, and health

Publisher payloads require narrow `docs/data/top5/shadow/` ownership, complete
provenance, no-bet, and shadow mode; staging is in memory and publishing is
disabled. The PWA contract includes league, fixture, kickoff, probabilities,
model identity, confidence metadata, snapshot age, signal timestamp, status,
provenance, and health state while retaining disabled/no-bet semantics.

Operational health reports provider, fixture, odds, signal-time, inference,
publisher, result-source, fallback, quota/cost, stale, retry, duplicate,
last-cycle, and activation-state fields with `registered=false`.

## Unresolved CEO decisions

- production candidate/model and model artifact;
- signal-time numeric values, cadence, retries, and markets/regions;
- fixture, odds, and result authority;
- quota/cost budget and provider plan;
- minimum shadow observation period and pass criteria;
- activation league order and scope;
- publication policy and any transition away from no-bet.

These decisions are intentionally not resolved by this workstream.
