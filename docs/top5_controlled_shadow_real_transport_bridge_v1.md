# Top-5 controlled-shadow real transport bridge V1

This package adds one separately reviewable `NETWORK_CAPABLE` seam between the
controlled-shadow execution harness and the existing The Odds API adapter.

`ConfiguredNetworkProviderTransport` accepts exactly the canonical The Odds API
adapter, a secret-free `ProviderConfig`, an existing `NetworkAuthorizationContract`, an
exact adapter source SHA, a pre-resolved provider fixture ID, and an explicit
timing policy. The caller must still supply the exact
`ControlledShadowRunAuthorizationV1` to the harness. The bridge validates that
the two supplied contracts bind to the same run, the canonical The Odds API
provider, adapter version, adapter source SHA, and prepared fixture before an
adapter is invoked. Fixture discovery is rejected until its evidence path is
separately reviewed; this V1 seam consumes a pre-resolved fixture identity and
performs one bounded odds request.

On a valid adapter response, the bridge forwards the adapter's exact event
identity, run request identity, adapter version/source SHA, source timestamp
provenance, raw response digest, normalized record digest, and request timing.
The harness then creates the canonical real capture attestation after the
complete cascade evidence digest exists. The bridge never creates CEO
authorization, preparation authority, a Builder-2 qualification receipt, a
prediction, publication, betting, or production authority.

All bridge tests inject a stub HTTP transport. No test performs a provider
call, uses credentials, consumes quota, spends money, publishes, bets, or
mutates production/runtime/ledger state. The existing `TEST_INJECTED` path
remains non-real and cannot emit `REAL_OBSERVED` or a canonical real capture
attestation.
