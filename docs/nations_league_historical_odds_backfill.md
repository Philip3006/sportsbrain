# Nations League historical-odds backfill

This is an offline research executor. It consumes the deterministic request-plan
artifact without recomputing target timestamps or fixture identities.

Current input commitments:

- plan SHA-256: ab9fea3a925c0a9ccbb5a6d8d7d9fb1cd9cbe1c20941c19bf07d3e676ae13882
- timeline digest: 842c0cc608b4221d63cbda079756dc392524e53349c9c24af5e8116c1d27e1af

## Dry run

DRY_RUN does not import a credential, create a transport, or make a network
request. It emits both named plans and their exact request identities.

The command requires explicit entitlement, balance, reset metadata, mode and
both digests. These are confirmations, not quota lookups.

## Real execution boundary

PREDICTION_ONLY and FULL_RESEARCH are supported by BackfillExecutor.execute()
only with an explicitly injected transport and credential resolver. The CLI
intentionally exposes DRY_RUN only; wiring a real transport is a separate
authorization step.

Before credential access, the executor checks entitlement, balance plus safety
buffer, reset metadata, requested mode, plan SHA-256 and timeline digest.
Requests are identified by provider, sport, region, market, phase and exact
historical timestamp. The append-only execution manifest treats a completed
request as paid and skips it on restart. A transport exception leaves the
request uncertain and blocks automatic retry, preventing an accidental second
paid request.

Raw responses and joined fixture observations are write-once. Only complete,
unambiguous 1X2 observations with valid pre-kickoff timing become prediction
inputs. Closing observations are marked RESEARCH_BENCHMARK_ONLY and are
structurally excluded from prediction inputs.
