# Top-5 Controlled Shadow Provider Qualification

Status: independent validation baseline; CEO review required before any future
real observation. This document describes `top5-controlled-shadow-provider-
qualification-v1`.

## Boundary

The qualification gate consumes serialized evidence only. It has no provider
client, credential, network, subscription, billing, quota-purchase, scheduler,
launchd, publisher, ledger, Cloudflare, Worker, Research, sealed-data, model
binding, or deployment capability. The offline runner is intentionally a
deserializer and validator; it cannot make a provider request.

`APPROVED_FOR_CONTROLLED_ACTIVATION` is a validation state in the wider
contract. This workstream does not create CEO authorization for Controlled
Activation and never recommends or performs activation. Every result remains
NO-BET, unpublished, and validation-only.

## Provider state and evidence kind

The existing readiness enum is reused without changing its meaning:

- `CONTRACT_SUPPORTED`
- `LIVE_PATH_PREREQUISITES_MISSING`
- `LIVE_PATH_READY_FOR_OBSERVATION`
- `REAL_OBSERVATION_VALIDATED`

The canonical football provider scope is the single provider `the_odds_api`.

Only `REAL_OBSERVED` can produce `REAL_OBSERVATION_VALIDATED`. `TEST_FIXTURE`,
`MOCK`, and `OFFLINE_REPLAY` may demonstrate the contract but can never count
as a real observation or promote readiness.

The gate preserves the causal cascade contract from PR #63. The configured
singleton order is recorded in every session; a provider failure is terminal
for football and cannot be substituted. The gate does not rank providers or
infer an authority, consensus, or winner.

## Session and observation contract

`ProviderQualificationSession` is immutable and records a deterministic
session ID, schema version, creation time, provider scope, exact league and
fixture scope, configured order, adapter/version and source SHA, readiness
state, counts, network request count, quota units, and safety assertions.
The session cannot assert `REAL_OBSERVATION_VALIDATED` without accepted real
evidence.

`RealProviderObservation` is immutable and records:

- exact league, fixture key, home/away teams, kickoff, provider event ID;
- pre-match `h2h_1x2` only, with home/draw/away odds and actual bookmaker/source
  identity;
- provider request identity, raw response digest, normalized record digest,
  adapter version and adapter source SHA;
- source timestamp, timestamp provenance, capture time, request start/end,
  latency, and caller-supplied signal-time checks;
- cascade evidence, selected provider, canonical provider order and prior
  outcome;
- quota before/after, quota units and exactly one documented network request;
- immutable `NO-BET`, publication-off, activation-off, no-ledger,
  no-sealed-data, no-Research, and no-monetary-spend flags.

No raw response, secret, authentication header, API key, or credential is
serialized. Missing provider/event/request identity, digest, adapter
provenance, or capture-time proof fails closed. Capture time is not source
freshness.

Every `REAL_OBSERVED` record must also carry a serialized
`ControlledShadowCaptureAttestation` from the completed controlled shadow run.
It binds the controlled-run ID, CEO authorization ID, qualification-session ID,
provider, exact fixture, event/request IDs, adapter provenance, capture time,
raw and normalized digests, and the exact serialized `CascadeEvidence` digest.
The safety fields are fixed to `network_execution=true`, `no_bet=true`,
`publication=false`, and `monetary_spend_authorized=false`. A marker, copied
fixture, or fake attestation cannot create real evidence; this module validates
the serialized equality binding and makes no cryptographic or remote-attestation
claim.

The accepted timestamp provenance values are:

`PROVIDER_SOURCE_TIMESTAMP`, `BOOKMAKER_UPDATE_TIMESTAMP`,
`EXCHANGE_PUBLISH_TIMESTAMP`, `CAPTURE_TIME_ONLY`, and `UNKNOWN`.

`CAPTURE_TIME_ONLY` and `UNKNOWN` cannot pass the real freshness gate. The
gate never fabricates a delay interval.

## Quality and timing

Fixture matching is exact. Wrong league, wrong event, team inversion, team
alias mismatch, kickoff outside tolerance, provider-event collision, and
conflicting duplicate identity are rejected. There is no fuzzy correction or
silent normalization at this boundary.

Only complete finite pre-match 1X2 odds are accepted. Missing draw, partial
markets, malformed/non-finite odds, in-play, closing, totals, spread, and
synthetic reconstruction are rejected. Closing odds never enter this path.

Every timing value is caller-supplied:

- maximum odds age;
- kickoff tolerance;
- minimum lead time;
- maximum lead time.

There are no production timing defaults. Every report contains the exact
unresolved note: **NO PRODUCTION SIGNAL-TIME VALUES APPROVED**.

Freshness is reported as `n`, minimum, median, and maximum source age for each
provider and each of `BL1`, `EPL`, `LL`, `SA`, and `L1`. Capture latency and
time-to-kickoff distributions are reported alongside it. A missing observation
has no fabricated denominator: coverage is `null` until an actual observation
exists.

## Authorization contract

`CEOAuthorization` is a future input, not a factory. It contains the bound
controlled-shadow-run ID, qualification-session ID, provider/league/exact
fixture scopes, maximum network request budget, expiry, single-use semantics,
and an explicit `monetary_spend_authorized=false`. Missing, expired,
out-of-scope, copied-to-another-run, or paid-spend authorization rejects
`REAL_OBSERVED` qualification. Single-use is enforced through caller-supplied
`authorization_usage` or `consumed_authorization_ids`; the validator performs
no state write and does not itself consume an authorization. Reuse within the
same bound run/session is permitted by the supplied usage record.

The network budget is the sum of `network_request_count` across every
serialized cascade attempt, including failed attempts. Preflight-only attempts
contribute zero. The report and session expose that full cascade count
separately from the selected observation's request count. `quota_cost_units`
are summed independently and are not treated as a one-request/one-quota
mapping.

## Qualification results and archive

Results are one of:

- `NOT_OBSERVED`
- `OBSERVED_REJECTED`
- `OBSERVED_VALID_CONTRACT`
- `REAL_OBSERVATION_VALIDATED`

Reports contain provider/status, real/accepted/rejected counts, accepted
distinct-fixture count, exact provider-league coverage, freshness, latency,
full-cascade and selected-observation network counts, independent quota units,
failure taxonomy, observed leagues, and unresolved issues. An explicit
`MinimumSamplePolicy` must be satisfied in both dimensions: minimum accepted
real observations and minimum distinct accepted real fixtures. These counts
never authorize production and the report contains no profit, model-edge, bet,
activation, or publication recommendation.

The archive is isolated under:

`top5-provider-qualification/<qualification_session_id>/`

with deterministic `manifest.json`, `observations.jsonl`, `validation.jsonl`,
`rejections.jsonl`, `coverage.json`, and `freshness.json` paths. It is distinct
from replay, prediction, shadow-publication, and ledger namespaces.

Only an accepted real observation may cross
`bridge_real_observation_to_builder1_shadow_evidence`. That bridge delegates
to the merged PR-#63 causal validator and emits Builder 1-compatible
`top5-shadow-evidence-v1` NO-BET observation evidence. It does not create M5,
prediction input, provider authority, model selection, publication, or
Controlled Activation.

## Failure matrix

The focused tests cover wrong fixture/league/teams/inversion, missing draw,
stale/future/capture-only timestamps, malformed and partial odds, duplicate
and conflicting duplicate identity, provider-event collisions, timeout,
401/403/429/5xx and quota outcomes through the merged cascade contract,
missing provenance, fake `REAL_OBSERVED`, paid-spend authorization, and
offline replay. All fail closed. The Odds API failure remains terminal and
subject to the causal PR-#63 validator.

## Verification boundary

The focused test module is no-network and uses deterministic fixture records.
All offline records are `TEST_FIXTURE`; their `counts_as_real` value is false.
The implementation has no provider call, scheduler, launchd, Cloudflare,
ledger, Research, sealed-data, deployment, publication, betting, or live
activation path.
