# TheRundown Top-5 controlled-shadow canary

This PR adds a disabled-by-default, single-shot canary seam for a future
explicitly authorized TheRundown Top-5 shadow run. It is deliberately not a
provider registration, a provider-authority change, a live adapter, a
scheduler job, or a Builder-2 receipt issuer.

## Contract

`TheRundownCanaryConfigurationV1` and the externally supplied
`TheRundownCanaryAuthorizationV1` must agree on:

- provider `therundown`, one Top-5 league, one exact fixture key, and one exact
  provider event ID;
- authorization ID, CEO authorization identity, run ID, qualification-session
  ID, issue time, and expiry;
- adapter version, adapter source SHA, and configuration digest;
- maximum request count, expected request quota cost, total quota budget, and
  maximum source age;
- sequential execution, explicit pacing, and zero retries; and
- `no_bet=true`, `publication=false`, `production_activation=false`, and
  `monetary_spend_authorized=false`.

The configuration defaults to `enabled=false`. The harness has no automatic
entry point and is not imported by a production scheduler. Enabling it still
requires the caller to supply a valid, unexpired authorization and a caller-
supplied transport.

## Evidence boundary

The shipped `FakeTheRundownCanaryTransport` is test-only. Its output is
`TEST_FIXTURE`, has `network_execution=false`, and produces evidence inputs
for the future capture attestation, qualification report, and Builder-2
receipt boundary. It does not produce a canonical real observation, a
qualification receipt, or authority.

The `TheRundownCanaryNetworkTransport` class is only a marker for a future
separately reviewed adapter. No HTTP client or provider implementation is
present in this PR. A network-capable response must provide `REAL_OBSERVED`,
complete pre-match 1X2 prices, exact provider/request/event identity, source
and request timestamps, bookmaker/source provenance, adapter provenance, and
both raw and normalized digests. The output includes the exact fields needed
by `ControlledShadowCaptureAttestation`, but Builder-2 remains the only
receipt authority.

The canary fails closed on expired or mismatched authorization, wrong scope,
wrong adapter/configuration, request or quota overrun, non-sequential requests,
unexpected retries, missing/stale timestamps, incomplete or invalid odds,
provider mismatch, and any attempted betting, publication, activation,
authority, ledger, scheduler, or spend mutation.

## Offline lifecycle compatibility proof

`TheRundownCanaryRunResultV1.lifecycle_artifact()` projects a deterministic
result into three downstream-shaped inputs without issuing any downstream
authority:

1. the exact `ControlledShadowCaptureAttestation` field shape, including run,
   CEO authorization, qualification session, provider/fixture/event/request
   identity, adapter/configuration provenance, cascade/raw/normalized digests,
   capture time, and immutable safety flags;
2. the exact `RealProviderObservation` field shape, including bookmaker,
   pre-match 1X2 prices, source-time provenance, capture/request times, quota
   evidence, and request count; and
3. the exact binding fields expected by `Builder2QualificationReceiptV1`, with
   report/result digests left unavailable until independent qualification has
   accepted the observation.

The artifact remains `candidate_only=true` and never constructs a canonical
observation, qualification report, or receipt. `TEST_FIXTURE` results are
`TEST_ONLY` and cannot validate as a real capture attestation. A deterministic
network-shaped stub result is labelled
`BLOCKED_BY_CURRENT_PROVIDER_REPERTOIRE`: the current B1/B2 contracts accept
only the active Football repertoire (`the_odds_api`), while TheRundown is
intentionally not registered or authoritative. The artifact also records that
a separately reviewed provider integration must supply a canonical
`CascadeEvidence` digest before current Builder-2 validation could proceed.

This is an explicit dependency report, not a bypass. Receipt issuance remains
impossible before a valid canonical attestation and an independently accepted
`REAL_OBSERVATION_VALIDATED` qualification result.

## Safety status

- No real provider request is made by this PR.
- No credential or quota state is read or mutated.
- No provider is added to the active Football repertoire.
- No prediction, receipt, publication, activation, deployment, ledger,
  Cloudflare, scheduler, Research, or sealed-data path is exposed.
- Tests use only deterministic in-memory responses.

This branch is review-only. **DO NOT MERGE.**
