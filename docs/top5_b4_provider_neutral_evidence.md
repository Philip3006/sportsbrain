# Top-5 B4 provider-neutral evidence foundation

This contract is a read-only evidence boundary. It can validate captured
provider artifacts and produce a deterministic B4 dossier; it does not call a
provider, issue authority, create a B2 receipt, select a model, activate Top-5,
publish, or bet.

## Providers and compatibility

The generic evidence allowlist is exactly `isports_api` and `the_odds_api`.
This is not a routing list and does not change the active Football provider
repertoire. Unknown identities fail closed. Existing TheRundown modules and
historical schemas remain available through their explicit legacy path; their
sport IDs, native authorization, datapoint accounting, and quota vocabulary do
not define these generic schemas. iSports values are never translated or
labelled as TheRundown evidence.

## Canonical Builder 4 artifact

Builder 4 supplies one JSON `top5-b4-provider-neutral-shadow-v1` artifact,
parsed by `B4ControlledShadowEvidenceV1.from_payload(payload, now=...)`. The
artifact binds one provider, controlled-shadow run, qualification session,
authorization ID and digest, source-main SHA, configuration digest, adapter
version/source SHA, operation records, exactly five ordered Top-5 fixture
discoveries, five market records, provider readiness, and five capture records.

The typed records are:

- `B4ProviderOperationEvidenceV1`: one HTTP response per record, semantic
  operation kind, secret-free path, ordinal, request identity, status,
  requested/completed timestamps, response digest, and typed optional quota / rate
  metadata. Retries are currently required to be zero. No key, token, query
  string, raw headers, or datapoint fiction is accepted.
- `B4FixtureDiscoveryEvidenceV1`: canonical league, provider competition ID,
  provider fixture ID, fixture key, participants, kickoff, discovery timestamp,
  operation link, and completeness. iSports `matchId` maps to
  `provider_fixture_id`; league IDs map to `provider_competition_id`.
- `B4MarketEvidenceV1`: provider fixture and request IDs, bookmaker/source
  provenance, complete regulation pre-match Home/Draw/Away prices, source/update
  timestamp, capture timestamp, and raw/provider-record/normalized digests.
- `B4ProviderReadinessV1`: explicit local request budget, pre-run consumed count,
  authorized run budget, observed requests/retries, and a provider usage snapshot.
  Quota and rate metadata each state `available`, `not_exposed`, or
  `unavailable`. Missing metadata stays missing. If provider policy requires a
  quota, it must be exposed and sufficient; otherwise only the explicit bounded
  request budget is used.
- `B4ControlledShadowCaptureV1`: exact run evidence links discovery and market
  digests, provider/request/fixture identity, adapter version/source SHA,
  source/capture times, raw,
  provider-record, normalized, cascade and capture-attestation digests, and
  immutable `no_bet=true`, publication/activation/betting/ledger=false flags.

Freshness preserves the current B4 boundaries: discovery evidence is at most
900 seconds old; odds source and controlled-shadow capture are at most 300
seconds old. Future timestamps, missing fixture identities, incomplete 1X2,
provider/run/session/authorization mismatch, non-200 operations, retries,
orphan operations, digest mismatch, and insufficient headroom fail closed.

## Qualification and dossier

`Top5B4ProviderNeutralEvidenceDossierV1.build(...)` deterministically derives a
five-league reconciliation, structural qualification result, and dossier
digest. Qualification means only that the provided structural evidence passes
this contract. It sets production Signal-Time approval, receipt eligibility,
authority change, publication, activation, and betting to false. A digest is an
integrity binding, not an issuer signature or authorization.

The dossier does not authenticate a CEO authorization by itself. Its
authorization provenance binds the exact IDs, provider, league set, digest, and
safety flags from the supplied authorization artifact. Downstream authority
consumers must still validate the actual authorization using their own trusted
contract.

## B1 logical handoff and stacked dependency

`b1_evidence_inputs(now=...)` retains the exact ten logical keys required by
the merged composer:

`source_main_sha`, `b4_quota_proof_package`, `b4_quota_headroom`,
`discovery_evidence`, `provider_native_discovery_provenance`,
`controlled_shadow`, `b4_reconciliation`, `b4_qualification`,
`b4_native_authorization`, and `b4_dossier_digest`.

The two legacy quota-named keys carry the provider-neutral readiness and
operation-headroom schemas. They do not contain a TheRundown quota proof. The
discovery/provenance, shadow, reconciliation, qualification, and authorization
values likewise use the new generic schemas. No B1 evidence reconstruction is
required.

This is a stacked foundation PR: current `verify_final_acceptance` still parses
TheRundown-specific quota/discovery/shadow values and fixes its candidate
identity. Builder 1 must add a consumer path for these provider-neutral schemas
before iSports can pass Final Acceptance. The composer’s logical input shape
does not change; this PR does not edit Builder 1 files, candidate identity
allowlists, active provider order, routing, or activation authorization.

## Builder 4 handoff fields

For each captured run, Builder 4 must provide:

1. `provider_identity` exactly `isports_api` (or `the_odds_api` only where that
   provider actually produced the evidence).
2. Operation records for schedule/competition discovery and bulk odds, with
   actual endpoint path, ordinal, request identity/count, HTTP status, zero
   retries, request/completion timestamps, response digest, and only observed
   quota/rate metadata.
3. Exactly one complete fixture record per `BL1, EPL, LL, SA, L1`, including
   provider competition ID, provider fixture/match ID, canonical fixture key,
   teams, kickoff and discovery time.
4. Exactly one complete regulation pre-match 1X2 market record per fixture,
   including actual bookmaker/source identity and provenance, odds timestamp,
   capture time, and raw/provider-record/normalized digests.
5. A readiness record with the explicit provider quota policy, pre-run snapshot
   status/values, authorized request budget, observed operation count, and retry
   count. Never invent quota units when headers are absent.
6. Five capture records linking the discovery, market and operation evidence to
   run/session/authorization IDs, source SHA/configuration digest, capture
   attestation, cascade evidence and all capture digests. `REAL_OBSERVED`,
   `network_execution=true`, `no_bet=true`; publication, activation, betting and
   ledger mutation remain false.

All timestamps must be timezone-aware UTC/offset timestamps. IDs and digests
must match across records; the dossier constructor performs the offline
cross-check and refuses partial league sets.
