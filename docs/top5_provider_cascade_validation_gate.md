# Top-5 Football Provider Validation Gate

Status: independent validation baseline, validation-only, NO-BET.

Contract version: `top5-provider-cascade-validation-v1`.

This document defines the evidence boundary for Builder 2’s independent review
of Builder 4’s future Top-5 provider cascade. It consumes serialized evidence
only. It does not run a provider, choose an authority, spend quota, publish an
artifact, bind a model, alter runtime state, or authorize Controlled
Activation.

## Scope and route order

The only accepted football/Top-5 route order is sequential:

1. `the_odds_api`
2. `FAIL CLOSED`

The order is caller-supplied configuration evidence, not an approval or
ranking of providers. The validator requires exactly the canonical provider,
and requires the evidence order to match that configuration exactly. A failed
provider cannot silently become a substitute authority.

The validator covers the canonical singleton order and the fail-closed result
when The Odds API is unavailable.
The validator does not import Builder 4’s router, providers, health state,
retry code, or budget manager. Builder 4 remains the owner of execution,
provider adapters, fallback behavior, quota accounting, and runtime health.
Builder 2 independently checks the serialized result of that behavior.

Provider claims remain bounded:

- The Odds API is an implemented candidate whose current authenticated quota
  observation is exhausted (`authenticated=true`, `quota_used=500`,
  `quota_remaining=0`); a quota-consuming odds request must therefore be
  denied before network execution.
- No provider is an authority, winner, or production source by virtue of this
  contract.

## Evidence contract

Each routed request is represented by a `CascadeAttempt`. The serialized
record includes:

- fixture identity: league, exact league/key, home team, away team, kickoff,
  and canonical fixture key;
- routing: configured order, provider identity, attempt index, fallback depth,
  network-called flag, request start/end/capture timestamps, outcome, and
  failure classification;
- market: `h2h_1x2`, home/draw/away odds, bookmaker identity, and source
  identity;
- timing: caller-supplied source timestamp, capture timestamp, and measured
  request latency;
- quota and budget: before/after quota snapshots, authentication state,
  preflight decision, budget decision, quota-versus-cost classification,
  `network_request_count`, and quota cost units;
- provenance: provider record ID, adapter version, raw-record digest, request
  identity, cascade evidence identity, artifact/source SHA, frozen Research
  SHA, candidate ID, model identity, and generation time;
- safety: `no_bet=true`, publication disabled, ledger unchanged, production
  activation false, sealed data not accessed, and Research not mutated.

The caller must supply an `ExpectedCascadeFixture`. The validator uses exact
canonical identity and explicit known aliases only; it does not use fuzzy,
substring, or guessed fixture matching. A missing or non-canonical expectation
is a policy error.

## Causality and fail-closed rules

All accepted execution evidence is sequential. The validator rejects:

- fallback before a legitimate preceding failure;
- an order or fallback-depth mismatch;
- an omitted provider without a matching provider identity, order index, and
  nonblank reason;
- duplicate attempts, overlapping attempts, or parallel/fan-out execution;
- any call after a successful provider response;
- a selected provider that did not produce the first valid success;
- a selected provider when every attempt failed;
- a network call after preflight or budget denial;
- a network call after a quota-consuming allowance was exhausted;
- `network_called=false` with a nonzero network request count;
- `network_called=true` with a zero network request count;
- a network request count greater than one, because no retry contract is
  authorized;
- an unknown or unbounded network request count or quota cost;
- a prediction input flag without an independently valid selected source.

When the configured The Odds API provider is rejected, the evidence may be
accepted as a measurement of failure coverage, but `selected_provider` is null and
`prediction_input_allowed` is false. This is an evidence success with a
fail-closed prediction result, never a betting or publication success.

## Quota and cost policy

`QUOTA_CONSUMING_REQUEST`, `ZERO_COST_AUTHENTICATION`, and `FREE_CACHE` are
distinct request-cost classifications. `UNKNOWN` is rejected.

`QUOTA_CONSUMING_REQUEST` means only that the provider allowance may be
consumed. It does not mean paid overage, subscription upgrade, billing
authority, or monetary spend. Monetary spend authorization is explicitly
`false` for this contract, and no paid-provider request is authorized by this
PR.

A quota-consuming request must have explicit credential, preflight, budget,
network-request-count, and quota-cost evidence. If the before snapshot reports
an authenticated provider with zero remaining credits, the request must have
`network_called=false`, `network_request_count=0`, and be classified
`QUOTA_EXHAUSTED`. The validator records that rejection as
  `quota_rejected_before_network_count` and fails the football route closed.

Zero-cost authentication is allowed only as an explicit zero-cost failure or
authentication observation; it cannot be promoted into a valid odds success.
Free-cache evidence cannot claim a network call or a nonzero cost. Missing
credentials, budget rejection, and preflight denial are independently visible
and fail closed. There is no retry authorization in this contract.

## Readiness and source quality

The validator uses the merged `ProviderReadinessState` enum:

- `CONTRACT_SUPPORTED`
- `LIVE_PATH_PREREQUISITES_MISSING`
- `LIVE_PATH_READY_FOR_OBSERVATION`
- `REAL_OBSERVATION_VALIDATED`

Adapter or contract existence alone remains `CONTRACT_SUPPORTED`. Readiness is
never inferred or advanced by this validator. A success requires a configured
ready state and an attempt state that does not exceed it. The caller supplies
the readiness map for the validation run.

For a successful signal-time observation, the gate requires:

- exact expected league, teams, canonical fixture key, and kickoff tolerance;
- `h2h_1x2` only;
- pre-match odds only; in-play and closing odds are rejected;
- finite, sane, complete home/draw/away odds;
- bookmaker and source identity;
- source timestamp that is not future-dated or stale;
- provider record, adapter, request, raw digest, and evidence provenance.

Timing thresholds are policy inputs. There is no production freshness default
and no production kickoff-tolerance default in this module. The policy requires
the caller to provide both values for every validation run.

## Failure taxonomy

The minimum serialized outcome taxonomy is:

`SUCCESS`, `QUOTA_EXHAUSTED`, `RATE_LIMITED`, `AUTH_FAILED`, `TIMEOUT`,
`HTTP_401`, `HTTP_403`, `HTTP_404`, `HTTP_422`, `HTTP_429`, `HTTP_500`,
`HTTP_502`, `HTTP_503`, `HTTP_5XX`, `PROVIDER_UNAVAILABLE`,
`UNSUPPORTED_FIXTURE`, `UNSUPPORTED_LEAGUE`, `UNSUPPORTED_MARKET`, `STALE`,
`MALFORMED`, `EMPTY_RESPONSE`, `PARTIAL`, `QUALITY_REJECTED`,
`CONFIG_DISABLED`, `CREDENTIAL_MISSING`, and `BUDGET_REJECTED`.

Unknown outcomes and unknown validation contract versions fail closed as
`INVALID_SOURCE_CONTRACT`.

## Metrics and evidence shape

`CascadeMetrics` exposes only operational validation measures:

- attempts, successes, and rejections;
- maximum fallback depth and successful fallback count;
- fail-closed count;
- exact fixture-match rate;
- complete 1X2 rate;
- freshness acceptance rate;
- observed request latencies;
- quota rejections before network execution.

The contract deliberately exposes no profitability, significance, betting
winner, authority score, or model-performance claim.

## Offline chaos matrix

The test suite injects, without any network client:

- timeout;
- HTTP 401, 403, 404, 422, 429, 500, 502, and 503;
- malformed and empty responses;
- stale and future timestamps;
- wrong league, wrong fixture, alias mismatch, and inverted home/away;
- partial and missing-draw markets;
- duplicate attempts and illegal extra calls;
- The Odds API quota/rate/auth failure remaining fail-closed;
- no alternate provider selection or fallback;
- missing credentials, denied preflight, budget rejection, unknown request
  count, unknown cost, and readiness escalation;
- serialized round-trip and Builder 1 bridge compatibility.

No provider call, live verification, credential access, or spending is part of
this baseline.

## Integration seam and safety boundary

Builder 4 may serialize a `CascadeEvidence` object at its execution seam.
Builder 2 calls `validate_cascade_evidence` with an explicit policy and, only
for accepted evidence, may bridge to Builder 1’s existing
`top5-shadow-evidence-v1` observation/bundle contract. The bridge preserves
`no_bet=true`, publication disabled, no real bet, no ledger mutation, no sealed
data access, no Research mutation, and no production activation.

The safety report preserves exact violations independently. For example,
`no_bet != true` reports `NO_BET_VIOLATION`, publication enabled reports
`PUBLICATION_ENABLED`, ledger mutation reports `LEDGER_MUTATION`, activation
reports `PRODUCTION_ACTIVATION`, sealed-data access reports
`SEALED_DATA_ACCESS`, Research mutation reports `RESEARCH_MUTATION`, and
monetary authorization reports `MONETARY_SPEND_AUTHORIZED`. Multiple violations
are returned together in deterministic contract order and always force
rejection.

The state `APPROVED_FOR_CONTROLLED_ACTIVATION` in the existing v1 contract is
only a validation state. This merge creates no CEOAuthorization decision for
actual Controlled Activation and does not change M5/model binding, provider
authority, scheduler, launchd, Cloudflare, publication, or runtime state.
