# TheRundown provider-event discovery gate

This is a separate, non-authorizing bootstrap stage for the later strict
TheRundown controlled-shadow run. It resolves exact provider event IDs from a
dated event snapshot without weakening the real-run invariant that every
`TheRundownCanaryTargetV1` is pre-bound to an event ID before network
execution.

The discovery authorization is structurally distinct from
`TheRundownNetworkAuthorizationV1`. It binds the candidate provider,
`EPL → BL1 → LL → SA → L1` fixture scopes, adapter version/source SHA, exact
request-shape digest, CEO discovery identity, expiry, five requests, 55
datapoints per request, 275 cumulative datapoints, zero retries, and the
non-authorizing safety flags. Discovery evidence has no qualification,
receipt, provider-authority, activation, publication, ledger, or spend
authority.

`discover_five_league_events()` uses the adapter's existing strict league,
participant, kickoff, event-state, and pre-match identity classifier. It
rejects zero/multiple matches, duplicate/malformed IDs, live/in-play or stale
events, HTTP failures, retries, malformed quota headers, and budget overruns.
Failures return no partial artifact set. The only later conversion is
`materialize_prebound_network_configuration()`, which copies the five exact
discovered IDs into the existing disabled-by-default strict run configuration.

All tests use an injected fake transport. No provider request, credential, or
quota is used by this stage or its test suite.
