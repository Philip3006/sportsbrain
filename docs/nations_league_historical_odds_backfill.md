# Nations League historical-odds backfill

This is an offline research executor. It consumes the deterministic request-plan
artifact without recomputing target timestamps or fixture identities.

Current input commitments:

- plan SHA-256: f5611025da9d606ea056e720ab2e45a4a622dd7c6b397c7822b5c3df9d978c33
- timeline digest: 2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef
- final timeline head: 065c6b40eb9911df3703d2e3079730a556136ee3
- final plan: 279 provider-coverable fixtures; 0 pending kickoff mappings; two administrative exceptions are explicitly classified, with the after-coverage exception included in the provider plan.

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
