# Top-5 Controlled Shadow authorization package

This package is an offline preparation and post-run reconciliation boundary
for the candidate provider identity `therundown_experimental`. It does not
create CEO authority, enable a transport, register a scheduler, issue a
Builder-2 receipt, or alter the active Football provider repertoire. The Odds
API remains the only active production Football provider.

## Inputs and exact scope

`prepare_authorization_package()` accepts a reviewed
`TheRundownNetworkConfigurationV1` with `enabled=false`. It rejects any
provider identity other than `therundown_experimental` and requires the exact
league set and order used by the package:

`EPL`, `BL1`, `LL`, `SA`, `L1`.

The package copies, rather than invents, the reviewed PR-103 values for:

- fixture, provider-event, participant, and request scope;
- adapter version and adapter source SHA;
- maximum request count, maximum datapoints, quota cost per request, and
  maximum quota cost units;
- source-age limit, sequential pacing, and `maximum_retries=0`.

The authorization template leaves CEO identity, authorization ID, controlled
shadow run ID, qualification session ID, issue time, and expiry as `null`.
Those fields must be supplied by a caller holding the separate run-specific
CEO authorization. The package cannot self-authorize a run.

The generated package is inert: network execution, receipt issuance, active
provider authority, scheduler registration, publication, activation, betting,
and monetary spend are all false.

Before the first request, the operator must provide a separate
`TheRundownQuotaHeadroomEvidenceV1` artifact. It is an immutable, digest-bound
observation of the provider/account's remaining billable datapoints and must
prove at least 275 datapoints, be no older than the configured source-age
window, and bind to the package digest, authorization ID, run ID, session ID,
and CEO authorization identity. Missing, stale, malformed, provider-mismatched,
account-unattributed, or under-budget evidence fails closed without a
transport call. Response billing remains validated independently after every
request; the headroom artifact never invents or reserves quota. The current
repository has no provider-native non-billable account artifact or verifier:
TheRundown exposes these datapoints only in billed response headers. Therefore
the real operator path currently fails closed with
`NO_TRUSTWORTHY_PRE_REQUEST_QUOTA_SOURCE`; the structural artifact is usable
only by explicit offline tests and cannot authorize a real run.

## Post-run reconciliation

`reconcile_controlled_shadow_run()` consumes only a completed
`COMPLETED_NETWORK` PR-103 result plus the exact configuration and
caller-supplied authorization. It validates every capture and attestation,
then emits ordered qualification evidence for all five leagues. The evidence
binds provider, league, fixture, provider event, request, participants,
bookmaker, complete pre-match 1X2 prices, source/capture/request timestamps,
freshness, adapter/configuration hashes, raw/provider/normalized/cascade
digests, and quota/rate-limit/tier/delay provenance.

### Same-run B1 La Liga validation

The real execution does not require a separate B1 request or a pre-run B1
artifact. The single authorized `LL` request is the only new LL evidence. Once
that response is accepted, B4 passes the genuine LL target/request/response
through `validate_same_run_ll_capture(...)`, producing the B1 validation
attestation used by
`reconcile_controlled_shadow_run_with_b1_ll_artifact(...)`. This preserves B1
ownership of validation without creating a sixth request, stale-evidence
shortcut, authority, or receipt issuer. Offline reconciliation continues to
accept the previously reviewed B1 bundle format where applicable.

B4 accepts the bundle only when all of the following match the `LL` capture in
the completed PR-103 result byte-for-byte or by canonical timestamp
comparison:

- `therundown_experimental`, canonical `LL` identity, fixture, provider event,
  provider request, home/away participant IDs, teams, and kickoff;
- the exact controlled-shadow run ID, qualification session ID, CEO
  authorization ID, expiry, adapter version, adapter source SHA, and raw
  response digest;
- source and capture timestamps within the 300-second freshness contract;
- a bookmaker with a complete home/draw/away decimal 1X2 record and matching
  provider/normalized record digests;
- quota-before/after, rate-limit, and quota evidence provenance.

The same-run B1 validation must remain `REAL_OBSERVED`, pre-match, candidate-only,
no-bet, unpublished, inactive, and no-spend. Its cascade digest must remain
`null`, and it must explicitly require B4 to supply the canonical cascade
evidence and capture attestation. A synthetic/replay bundle, stale bundle,
wrong request, partial 1X2, missing bookmaker/provenance, or any identity or
digest mismatch fails closed. The final reconciled payload retains the exact
B1 bundle under `artifacts.b1_ll_artifact`; it does not turn it into a receipt.

### Expected five-league artifact set

The reconciled package is ordered and complete only when it contains exactly
these five slots:

1. `EPL`
2. `BL1`
3. `LL` — the slot bound to the repaired B1 artifact above
4. `SA`
5. `L1`

Each slot contains one PR-103 canonical capture attestation, one
`CandidateProviderEligibilityV1` payload, one qualification input, and one
Builder-2 receipt input. The five receipt inputs remain
`eligible=false`/`issuer_present=false`; the reconciliation status remains
`PENDING_BUILDER2_VALIDATION`. No slot can change production authority,
publication, scheduler registration, activation, betting, or spend.

Synthetic, replay, stale, post-kickoff, incomplete, ambiguous, duplicated,
rebinding, digest-mismatched, or otherwise malformed evidence fails closed.
The resulting artifacts contain canonical capture attestations and
`CandidateProviderEligibilityV1` payloads, but their Builder-2 receipt inputs
remain explicitly `eligible=false` and `issuer_present=false`. The
qualification status is `PENDING_BUILDER2_VALIDATION`; shadow success does
not grant authority or publication.

## Guarded operator entrypoint

The single canonical operator entrypoint is implemented in this module. With
no mode flag, it performs a zero-network preflight, but the current preflight
also fails closed until a provider-backed quota-headroom source and verifier
are integrated. The explicit `--execute-network` flag is required before the
reviewed HTTP transport can be constructed:

```text
python -m src.football.top5_controlled_shadow_authorization_package \
  --execute-network \
  --package /operator-only/top5/authorization-package.json \
  --authorization /operator-only/top5/ceo-authorization.json \
  --quota-headroom /operator-only/top5/quota-headroom-evidence.json \
  --credential-file /operator-only/top5/therundown.env \
  --output /operator-only/top5/top5-b2-five-league-shadow-package.json
```

The package file is the exact inert package produced by
`prepare_authorization_package()`. The CEO authorization file is a JSON
wrapper containing `package_digest` and the serialized
`TheRundownNetworkAuthorizationV1` payload, including its
`authorization_digest`. The entrypoint derives the enabled execution
configuration from the package only after verifying the authorization's
configuration digest, exact five-league scope, run/session/identity/expiry,
adapter hashes, `5/55/275` budgets, `>=1.1` second pacing, zero retries, and
all candidate-only safety flags. The quota-headroom file contains the
serialized `TheRundownQuotaHeadroomEvidenceV1` observation, including its
evidence digest, provider/account scope, observation timestamp, and remaining
datapoints. The entrypoint requires a fresh `>=275` headroom proof before the
first request. The LL response is validated immediately after the single LL
capture, before SA or L1 can be requested, and reconciled only after that
validation succeeds. The current operator command remains blocked by the
missing trustworthy pre-request quota source described above.

`--credential-file` must be an absolute, non-symlink, operator-only file with
no group/world permissions and an existing `THERUNDOWN_API_KEY=` entry. The
key is read only for transport construction, is never printed, and is never
written to the output artifact. `--output` is required for network execution;
the package is written atomically with mode `0600`, and a conflicting existing
package is rejected.

The successful artifact is the canonical
`top5-b2-five-league-shadow-package-v1` consumed by Builder 2. It contains a
`COMPLETED_NETWORK` run, exactly five capture envelopes, and exactly five
canonical `Builder2QualificationIntakeManifestV1` manifests. Its package ID
and digest are deterministic. B4 writes it atomically and reloads it through
`load_five_league_shadow_package()` before reporting success. B4 does not issue
the receipt or change authority. Any credential, package/configuration digest,
authorization, quota-headroom, B1, billing, freshness, event, participant,
provenance, retry, HTTP, or safety failure aborts without continuing to the
next request.

Preflight and all package/reconciliation functions perform no provider
request, do not register a scheduler, and have no receipt-issuer or authority
path. The explicit network flag is the only path that uses
`TheRundownHttpNetworkTransportV1`.

## Compatibility assumptions

- PR-95 remains the controlled-shadow lifecycle envelope.
- PR-103 remains the sequential, zero-retry, bounded network transport and
  attestation producer.
- PR-112 remains the candidate-provider eligibility boundary and keeps
  `therundown_experimental` out of active routing.
- Builder 2 still owns qualification reporting and
  `Builder2QualificationReceiptV1` issuance. B4 projects each validated
  network capture into the canonical cascade and intake-manifest contracts;
  this projection preserves the provider, raw, normalized, request, quota,
  timestamp, bookmaker, and safety bindings and does not issue a receipt.
- The single LL response is validated through the same-run B1 seam after
  capture; it is not a substitute for the PR-103 canonical capture attestation.
- No real La Liga capture or five-league Controlled Shadow is consumed during
  offline package preparation.
