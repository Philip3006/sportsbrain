# Top-5 Offline Shadow Replay and Result Attachment

## Purpose and status

This package is an `OFFLINE_REPLAY` harness for `ENGINEERING_VALIDATION_ONLY`.
It exercises the merged M5 shadow path with static historical inputs, then
attaches later results and benchmark-only closing records. It is not real
shadow evidence, provider validation, signal-time validation, profitability
validation, model selection, or production activation.

The lifecycle is:

1. validate a historical candidate and pre-closing signal snapshot;
2. run the existing static M5 shadow pipeline;
3. emit an immutable prediction artifact with a stable digest;
4. attach a later result as a separate immutable record;
5. attach a closing benchmark as a separate immutable record; and
6. measure only fully attached observations.

No provider client, scheduler, publisher, ledger writer, Cloudflare binding,
or production archive writer is imported by the replay runner.

## Data boundary

Only explicit DEV partitions are accepted: `2021`, `2122`, `2223`, and
`2324`. `2425` calibration data and `2526` holdout data are sealed and are
rejected before replay input validation completes. Unknown or future
partitions also fail closed. The input source must use the `historical:`
namespace, and the five supported leagues are exactly BL1, EPL, LL, SA, and
L1. Champions League input is rejected.

No loader bypass is provided. The caller supplies an already-approved static
fixture and signal-time snapshot; this module never discovers or fetches
data.

## Prediction immutability

`ReplayPredictionArtifact` is frozen, wraps probability mappings in immutable
views, and records the fixture identity, kickoff, historical source identity,
source timestamp, replay timestamp, snapshot identity, M5 identity, frozen
Research SHA, integration SHA, and no-bet/publication state. Its digest is
computed over the prediction payload and verified on every serialization.

Result and closing attachments carry the prediction ID and prediction digest.
The archive rejects changed digests, wrong fixture or league identities,
conflicting duplicates, and attachments that precede prediction creation.
An identical duplicate attachment is idempotently suppressed.

## Result attachment

Results include historical source provenance, source and attachment timestamps,
home and away scores, the derived 1X2 outcome, and one of `final`,
`postponed`, `cancelled`, or `abandoned`. Non-final states remain unresolved.
Scores and outcome are required to agree for final results. An earlier result
cannot be silently overwritten by a conflicting result.

## Closing benchmark attachment

Closing records include historical source, bookmaker, closing timestamp,
attachment timestamp, odds, fixture identity, prediction identity, and the
original prediction digest. They are benchmark-only. `used_for_prediction`
is hard-rejected, and the prediction artifact contains no closing fields or
closing values. Builder-2 output records closing odds only in its separate
`closing_benchmark_evidence` section.

## Causality guarantees

The runner creates prediction artifacts before the archive accepts any result
or closing record. The prediction builder receives only the signal-time
snapshot. Attachments are accepted only after the replay timestamp, and the
prediction digest must remain unchanged. Replay ordering does not affect the
digest or serialized archive. Wrong fixture, wrong league, missing prediction,
conflicting result, and sealed partition attempts fail closed.

## Coverage and performance

Coverage reports every supported league, including zero-coverage leagues, with
candidate historical fixtures, legitimate replay inputs, produced
predictions, rejected candidates, attached results, unresolved results,
closing benchmarks, and performance-eligible observations. No full-coverage
claim is inferred from a partial input set.

Performance output reuses the existing Builder-2-compatible metrics contract
for sample size, Brier score, log loss, calibration error, market-relative
comparison, and benchmark-only closing comparison. It explicitly sets:

```text
profitability_claim = false
significance_claim = false
deployable_edge_claim = false
```

These are descriptive engineering diagnostics only. No profitability, market
edge, statistical significance, or activation recommendation is produced.

## Builder-2 compatibility

`OfflineReplayRun.to_builder2_bundle()` emits
`top5-shadow-evidence-v1` records with the frozen Research SHA, M5 identity,
NO-BET assertions, publication disabled, no provider validation assertion, and
separate result and closing sections. Builder 1 remains the evidence producer;
the independent Builder-2 validator remains an external compatibility
consumer. This harness does not invoke that validator as an authority gate.

## Namespace separation

Offline records use:

```text
results/offline_replay/top5/
```

Real provider shadow records use the existing:

```text
results/shadow/top5/
```

The serialized archive includes both namespaces and
`promotable_to_real_observed = false`. There is no promotion function, no
production archive writer, and no path that treats offline records as real
observations.

## Explicit non-proofs and unresolved decisions

This package does not prove real provider availability, real signal-time
behavior, provider quota health, production performance, profitability,
statistical significance, model superiority, or live activation readiness. It
does not unlock 2425 or 2526, tune M5, compare models, register a league, or
change any scheduler, launchd, Cloudflare, PWA, ledger, Research, or
production state.

The exact production signal-time values, provider schedule, model activation,
shadow activation, and controlled activation remain separate CEO decisions.
