# Top-5 Shadow Validation Gate

## Status and ownership

This is Builder 2's independent validation layer for real Top-5 NO-BET shadow
evidence produced by Builder 1. It consumes external evidence records and
never calls, imports, or runs Builder 1's execution pipeline. It does not
activate Top-5, choose a provider, choose a production timing, bind a model,
publish an artifact, place a bet, or mutate runtime state.

The merged baselines are PR #57 Shadow Integration
(`2b285424daf1a6315f33020c282e522589c8a917`) and PR #58 Activation Readiness
(`8d401ff7a8c9b7f23f354f09b5563caa804332e2`). The frozen Research reference
is `6eaabbec7d0182103d815c72fae4976e261b40aa` and must remain unchanged.

## External evidence contract

The versioned contract is `top5-shadow-evidence-v1`. Every record contains a
`provenance` object with:

- unique `evidence_id` and `artifact_id`;
- unambiguous 40-64 character `artifact_sha` and `source_sha`;
- the exact frozen `research_sha`;
- `league_code`, `candidate_id`, and `model_identity`;
- timezone-aware `generated_at`;
- `fixture_key` for fixture-level records; and
- an optional feature-schema hash.

The bundle also contains an ordered, timezone-aware evidence window and
explicit safety assertions. Builder 1 may serialize these records in any
implementation language or storage format; Builder 2 only depends on the
fields in this contract.

The contract has these evidence collections:

1. `observations`: discovered, eligible, odds-valid, predicted, rejected,
   provider-covered, stale, fallback, error, duplicate, and result-resolution
   flags per fixture;
2. `predictions`: prediction identity, signal snapshot identity, prediction
   input kinds, probabilities, and optional market probabilities;
3. `provider_evidence`: provider name, outcome, request denominator,
   coverage numerator, latency, freshness, bulk reuse, fallback, retries, and
   failure rejection counts;
4. `signal_time_evidence`: named timing candidate measurements for coverage,
   odds age, request load, fallback/stale risk, latency, quota/cost, and
   operational complexity;
5. `quota_cost_evidence`: named horizon with fixtures, logical evaluations,
   independent request categories, raw HTTP requests, and cost units;
6. `health_evidence`: provider/inference/publisher/result health and explicit
   disabled-or-shadow activation state;
7. `result_attachments`: prediction-to-result linkage, resolution state,
   outcome, source, and result delay;
8. `closing_benchmark_evidence`: signal-vs-closing benchmark values kept out
   of prediction input; and
9. `failure_evidence`: typed, deterministic failure taxonomy with blocking
   status and a human-readable explanation.

The safety object must explicitly assert `no_bet=true`,
`publication_enabled=false`, `real_bet_created=false`, `ledger_mutated=false`,
`sealed_data_accessed=false`, `research_mutated=false`, and
`production_activation=false`. Missing or unsafe assertions fail closed.

## Independent validation method

`ingest_shadow_evidence` accepts either a `ShadowEvidenceBundle` or the
contract mapping. Ingestion is deterministic and read-only:

- validates every record and the evidence window;
- rejects missing or ambiguous provenance and SHA/artifact identity;
- rejects unknown leagues, missing fixture identity, cross-league fixture
  reuse, duplicate fixture evidence, and broken record links;
- verifies the frozen Research SHA without changing it;
- verifies every prediction uses signal-time input only;
- verifies every record remains NO-BET and unpublished; and
- produces a stable SHA-256 evidence digest for the canonical payload.

Builder 2 does not repair, normalize, infer, or overwrite Builder 1 evidence.
An invalid record produces a rejection state and remains visible in the
blocker list.

## Coverage validation

`validate_coverage` calculates independent per-league counts. Every rate has
an explicit numerator and denominator derived from discovered fixtures:

- discovered fixtures;
- eligible fixtures;
- valid odds;
- predictions;
- rejected fixtures;
- provider-covered fixtures;
- stale fixtures and stale rate;
- fallback fixtures and fallback rate;
- errors and error rate;
- duplicate suppression and duplicate rate; and
- resolved results and result-resolution rate.

Prediction/result identity is joined by `prediction_id`; result attachments,
not a caller-supplied summary flag, determine result resolution.

## Provider evaluation

`evaluate_provider_evidence` groups observed records by league and provider.
It reports availability, mean latency, freshness, completeness, bulk reuse,
fallback frequency, retry rate, and a failure taxonomy covering:

- HTTP 403 and 429;
- timeout, empty, malformed, and partial responses;
- stale responses;
- wrong-market and wrong-fixture rejection.

The output has no selected provider and no authority-winner field. It is
evidence for a later CEO decision only. This module never makes a provider
call or incurs provider spend.

## Signal-time comparison

`compare_signal_time_evidence` groups named Builder 1 shadow-experiment
configurations and returns comparative metrics for coverage, freshness, mean
odds age, request load, fallback frequency, stale risk, latency, quota/cost,
and operational complexity.

The result always has `selected_candidate=null` and
`recommendation=null`. Numeric timing, cadence, retry, market, region, and
scope decisions remain unresolved CEO decisions.

## Performance framework

`calculate_performance_metrics` uses only resolved prediction/result pairs and
always reports sample sizes. Where the sample permits it calculates:

- multiclass Brier score;
- log loss;
- calibration error;
- market-relative mean absolute probability difference; and
- benchmark-only signal-vs-closing diagnostics.

Closing values are represented in a separate collection and are structurally
rejected if marked as prediction input. The output explicitly makes no
profitability or statistical-significance claim. No deployable edge is
inferred from these diagnostics.

## Formal gate states and transitions

The gate uses these states:

`NO_EVIDENCE` → `EVIDENCE_INCOMPLETE` → `OBSERVING` →
`PROVIDER_VALIDATION_PENDING` → `SIGNAL_TIME_VALIDATION_PENDING` →
`SHADOW_PERFORMANCE_PENDING` → `READY_FOR_CEO_GATE` →
`CEO_DECISION_REQUIRED` → `APPROVED_FOR_CONTROLLED_ACTIVATION`.

`REJECTED_SAFETY` is a terminal fail-closed state for safety violations.

The normal assessment can reach only `READY_FOR_CEO_GATE`. A separate explicit
`require_ceo_decision` transition is required before a decision can be
applied. `APPROVED_FOR_CONTROLLED_ACTIVATION` is reachable only when:

- the state is `CEO_DECISION_REQUIRED`;
- the CEO authorization is explicit and approved;
- the authorization digest exactly matches the evidence digest; and
- the authorization includes a decision ID, scope, timestamp, and reason.

Even that state is a validation result. This module contains no activation
method, so it cannot execute a controlled run.

## Hard safety rejection rules

Reject immediately for:

- `no_bet=false` or a missing NO-BET assertion;
- publication enabled or a registered shadow artifact;
- real bet creation or ledger mutation;
- sealed-data access or Research mutation;
- production activation without CEO authorization;
- closing leakage into prediction inputs;
- unknown or mismatched league/fixture identity;
- missing or ambiguous provenance, SHA, or artifact identity;
- duplicate or cross-league evidence; and
- result, prediction, or snapshot identity mismatch.

No rejected evidence is silently dropped or repaired.

## CEO decision packet

`build_ceo_review_packet` produces a deterministic packet with:

- evidence window and digest;
- covered leagues and fixture counts;
- provider assessment and failure taxonomy;
- comparative signal-time matrix;
- quota/cost evidence;
- coverage and freshness;
- result completeness;
- performance sample size and diagnostics;
- separate benchmark-only closing/CLV-style diagnostics;
- unresolved decisions;
- exact blockers; and
- a recommendation of `CEO_DECISION_REQUIRED` only when evidence genuinely
  reaches the CEO gate, otherwise `NO_ACTIVATION`.

The packet never recommends automatic activation and never selects a provider,
timing candidate, model, league order, publication policy, or budget.

## Unresolved CEO decisions

- production model and artifact;
- signal-time numeric configuration;
- fixture, odds, and result authority;
- quota and cost budget;
- minimum shadow observation requirement;
- activation league order and scope; and
- publication policy.

## Verification

The independent test suite covers valid and incomplete evidence, tampered and
wrong SHA, missing provenance, cross-league contamination, duplicate and stale
evidence, fake publication, NO-BET violations, closing leakage, provider
failures, result mismatch, sample-size handling, comparative timing without a
winner, deterministic CEO packets, and every gate transition. Broader
SportsBrain regressions remain required before the branch is proposed for CEO
review.
