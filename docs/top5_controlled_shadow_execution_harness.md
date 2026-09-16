# Top-5 controlled shadow execution harness V1

This package is a guarded, test-only execution seam for a future controlled
shadow run. It consumes a caller-supplied
`ControlledShadowRunPreparationV1` and an exact,
CEO-supplied `ControlledShadowRunAuthorizationV1`. The harness validates the
binding and never creates either authority.

The sequence is:

```text
preparation -> external CEO authorization -> injected cascade transport
-> cascade evidence and capture attestation -> independent Builder-2 intake
-> canonical qualification receipt -> Builder-1 consumer
```

The harness does not issue the qualification receipt. Candidate-only cascade
output and an attestation without independent qualification remain evidence,
not Builder-1 authority.

## Safety boundary

The normal CLI never executes a provider. `execute` fails closed unless
`--acceptance-fake` selects `FakeControlledShadowTransport`; the fake transport
is the only implementation shipped here. There is no credential forwarding,
network fallback, account or billing operation, ledger, Cloudflare, scheduler,
launchd, publication, production activation, bet, or sealed-data access.

Every result carries these banners:

```text
CONTROLLED SHADOW ONLY
CEO AUTHORIZATION REQUIRED
NO BET
NO PUBLICATION
NO PRODUCTION ACTIVATION
B2 QUALIFICATION REQUIRED AFTER CAPTURE
```

Discovery and odds requests are separately counted. Each request reserves both
the global and provider budget atomically before the injected transport is
called. A known quota of `used=500, remaining=0` causes zero transport calls
and explicit `QUOTA_EXHAUSTED` evidence. A repeated completed run ID returns
the stored evidence; a conflicting or already-in-flight replay fails closed.

The harness emits deterministic run evidence and, when an injected response
contains a complete observation, the existing provider-neutral observation
contracts. It does not create predictions, live signals, publication records,
bets, or qualification authority.
