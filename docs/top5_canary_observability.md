# Top-5 canary observability

This is a read-only evidence evaluator for the future five-fixture Top-5
canary. It does not call a provider, read credentials, write runtime state,
register a scheduler, publish, activate, or place bets. It consumes the
redacted evidence produced by the canary runner and returns a deterministic
health artifact plus one operator evidence bundle.

## Contract

The schema versions are:

- `top5-canary-observability-v1` for the health artifact;
- `top5-canary-evidence-bundle-v1` for the redacted evidence bundle.

The evaluator requires exactly one unique fixture for each `EPL`, `BL1`, `LL`,
`SA`, and `L1`. Each fixture must be in `INITIAL` with a 22–26 hour lead or
`REFINEMENT` with a 60–120 minute lead, and its provider response must be
fresh. Quota evidence and authorization expiry are checked against the supplied
evaluation time. The bundle also requires model completion, a `COMPLETE`
five-fixture batch, equal private/public/Worker digests, signals compatibility,
and a rollback snapshot.

The health artifact keeps these domains separate: `runtime`, `provider_quota`,
`fixture_binding`, `lifecycle`, `batch_storage`, `public_route`,
`observability`, and `rollback`. Any missing, stale, mismatched, or unknown
required value produces `CANARY_FAILED`; no implicit success is inferred.

Production safety is explicit in every successful result: production authority
remains `the_odds_api`, the candidate provider remains
`therundown_experimental`, and `no_bet`, `publication`,
`production_activation`, and `provider_authority_granted` remain safe.

## Read-only monitoring compatibility

No Checkly configuration or paid Browser check is added by this change. The
existing API-monitoring arrangement can use read-only checks for the PWA root,
the Worker `/signals.json` route, the public signals contract, and committed
Top-5 generation visibility. These checks must inspect returned artifacts only;
they must not invoke a provider or an authenticated SportsBrain endpoint.

Public reads are also represented in the bundle by
`public_read_provider_calls == 0`. A non-zero value fails closed, which makes a
hidden provider call observable in offline and production verification.

## Operator output

The bundle contains timestamps, the exact authorization-bound execution ID,
fixture IDs and leagues, lifecycle decisions, batch/generation IDs, storage
digests, per-fixture signal decisions, domain health, rollback evidence, and a
structured failure reason. It intentionally excludes credentials,
authorization material, user data, bankroll, and stake data.
