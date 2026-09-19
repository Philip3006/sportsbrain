# Top-5 candidate-provider eligibility bridge

`therundown_experimental` is an explicitly candidate-only identity.  This
bridge extends the validation vocabulary used by the controlled-shadow and
qualification contracts without extending the active Football provider
repertoire.

| Capability | The Odds API | `therundown_experimental` |
| --- | --- | --- |
| Active production routing | allowed | forbidden |
| Candidate observation | allowed | allowed with eligibility binding |
| Controlled Shadow | allowed | allowed with exact run authorization |
| Qualification input | allowed | allowed only after real evidence and eligibility validation |
| Publication | governed separately | forbidden |
| Scheduler registration | governed separately | forbidden |
| Betting | governed separately | forbidden |

## Canonical binding

`CandidateProviderEligibilityV1` is constructed from the completed PR #103
network-shadow capture or an equivalent canonical payload.  It binds the
candidate identity to the adapter version and source SHA, configuration
digest, canonical league and fixture, provider event and request identities,
participant IDs, bookmaker, pre-match regulation 1X2 prices, source/capture
timestamps, freshness, provenance, raw/provider/normalized/cascade digests,
quota and rate-limit evidence, tier, and provider delay.

The object requires `REAL_OBSERVED` plus network execution and permanently
requires `no_bet=true`, `publication=false`,
`production_activation=false`, and `monetary_spend_authorized=false`.  It
rejects test fixtures, mocks, offline replay, capture-time-only timestamps,
post-kickoff/stale data, incomplete prices, missing bookmaker/provenance,
identity or digest mismatches, and unsafe capability flags.

## Downstream gate

Candidate observations are recognized by the qualification and cascade
validators, but `qualify_provider_observations` rejects them unless the
caller supplies the matching `CandidateProviderEligibilityV1`.  The Builder-2
intake manifest carries the same optional object and requires it for the
candidate identity before issuing its existing receipt.  The receipt issuer
and authority state are unchanged; qualification success is evidence only.

`ProviderCascadeConfig` rejects candidate identities in active routing, while
the explicit PR #95/PR #103 controlled-shadow path remains available for
future run-specific authorization.  No scheduler, provider client, or
production activation path is registered by this bridge.

## Compatibility assumptions

- PR #94 remains the pure qualification/intake consumer and does not create
  candidate eligibility.
- PR #95 and PR #103 remain the controlled-shadow capture/transport producers;
  this bridge consumes their validated capture structures.
- The active production order remains exactly `("the_odds_api",)`.
- A future real run still requires a separate exact CEO authorization and
  independently validated source readiness.
