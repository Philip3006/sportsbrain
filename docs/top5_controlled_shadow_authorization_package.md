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

## Post-run reconciliation

`reconcile_controlled_shadow_run()` consumes only a completed
`COMPLETED_NETWORK` PR-103 result plus the exact configuration and
caller-supplied authorization. It validates every capture and attestation,
then emits ordered qualification evidence for all five leagues. The evidence
binds provider, league, fixture, provider event, request, participants,
bookmaker, complete pre-match 1X2 prices, source/capture/request timestamps,
freshness, adapter/configuration hashes, raw/provider/normalized/cascade
digests, and quota/rate-limit/tier/delay provenance.

### Direct B1 La Liga injection

The repaired B1 La Liga artifact is injected as the keyword argument
`b1_ll_artifact` to
`reconcile_controlled_shadow_run_with_b1_ll_artifact(result, configuration,
authorization, b1_ll_artifact, now=...)`. The value is exactly the mapping
returned by `LaLigaCaptureEvidence.as_evidence_bundle()`; an object exposing
that method is also accepted and materialized once. No B1 object is imported
as an authority or receipt issuer.

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

The B1 bundle must remain `REAL_OBSERVED`/`CAPTURED`, pre-match, candidate-only,
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

The single canonical operator entrypoint is now implemented in this module.
With no mode flag, it performs a complete dry-run/preflight and makes zero
provider calls. The explicit `--execute-network` flag is required before the
reviewed HTTP transport can be constructed:

```text
python -m src.football.top5_controlled_shadow_authorization_package \
  --execute-network \
  --package /operator-only/top5/authorization-package.json \
  --authorization /operator-only/top5/ceo-authorization.json \
  --b1-ll-artifact /operator-only/top5/b1-ll-evidence-bundle.json \
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
all candidate-only safety flags. The B1 artifact is bound to the LL target
before the first request and reconciled again after the completed run.

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
authorization, B1, billing, freshness, event, participant, provenance, retry,
HTTP, or safety failure aborts without continuing to the next request.

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
- The B1 LL bundle is a validated input to the exact LL slot, not a substitute
  for the PR-103 canonical capture attestation.
- No real La Liga capture or five-league Controlled Shadow is consumed during
  offline package preparation.
