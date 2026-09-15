# Top-5 real shadow execution

This is a controlled, no-bet observation path for the five approved leagues:
BL1, EPL, LL, SA, and L1. It is not a production registration, scheduler,
publisher, model approval, or betting path.

## Execution contract

`scripts/top5_controlled_shadow.py` requires an explicit timing experiment,
exact implementation SHA, quota remaining value, safety reserve, and the
`--ack-no-bet` acknowledgement. It performs one The Odds API bulk request per
league using `h2h` and `eu`. It does not call `/sports`, use event-level
fallback, retry, use stale cache, or fan out beyond the five requests.

Only M5 (`M5_market_preclose`) is available in the frozen Research SHA
`6eaabbec7d0182103d815c72fae4976e261b40aa`. M1, M2, M3, M4, M6, and M7 are
not fabricated or substituted.

The caller-selected timing is named `shadow-experiment:*` and is recorded as
`production_approved: false`. The signal-time contract rejects naive
timestamps, closing snapshots, stale snapshots, and fixtures outside the
selected event-relative window. Every accepted observation carries the
league, stable provider fixture identity, request identity, source, snapshot
time and age, timing identity, Research SHA, implementation SHA, M5 identity,
feature schema, prediction/signal identity, no-bet state, publication state,
result state, and closing-capture state.

## Quota boundary

The preflight budget is exactly five bulk request cost units plus the caller's
positive safety reserve. An unknown or insufficient quota fails before any
provider call. A 401, 403, or 429 stops the cycle immediately. Other provider
failures are recorded as bounded evidence without fallback. Provider response
diagnostics retain only allowlisted quota headers and never response bodies,
URLs containing credentials, cookies, or tokens.

## Retention and safety

The default archive writer stores redacted shadow evidence under the external
operator-owned runtime state root at `football/top5/shadow_runs/`. It is not a
public artifact and is not written into the active checkout. No result
attachment or closing capture is performed in this cycle; both remain pending
benchmark-only stages. No ledger, publisher, Cloudflare, PWA, launchd, or
production model path is imported.

Each invocation is a `CONTROLLED SHADOW RUN`. It must not be described as a
natural canary or natural scheduled run. No persistent scheduler is included
by this change.

## Independent evidence contract

The merged Builder 2 validator consumes `top5-shadow-evidence-v1`. Builder 1
remains the producer: `RealShadowCycleResult.as_evidence_payload()` emits the
contract's safety assertions, per-fixture observations, M5 predictions,
provider and signal-time evidence, quota-cost evidence, health evidence, and
bounded failure evidence. The producer does not import or invoke the Builder 2
validator. Closing benchmarks and result attachments are intentionally empty
until their separately authorized stages exist. The payload is deterministic
for a fixed cycle result and contains no provider secret, response body, or
credential-bearing URL.

The current external provider gate is blocked by exhausted monthly quota. A
zero-cost authenticated `/events` check returned HTTP 200 with 500 requests
used, 0 remaining, and 0 last-request cost. No odds request was made after
that confirmation, and no archive was written for the failed/quota-blocked
execution. No billing, subscription, or quota change was authorized.
