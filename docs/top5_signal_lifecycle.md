# Top-5 two-stage signal lifecycle

`top5-signal-lifecycle-v1` records an explicitly timed, immutable logical
signal in two offline-validated stages. It does not register a scheduler,
provider, model runtime, publisher, activation writer, or financial writer.
The contract's `signal_time_approved_for_production` value is permanently
`false`; this implementation is not production Signal-Time approval.

## Timing contract

| Stage | Lead relative to scheduled kickoff | Maximum snapshot age | Retries |
| --- | --- | --- | --- |
| INITIAL | 22–26 hours | 900 seconds | 0 |
| REFINEMENT | 60–120 minutes | 900 seconds | 0 |

Both stages require a `SIGNAL_TIME` snapshot for the same fixture, with a
non-empty snapshot identity and source timestamp no later than generation
time. `CLOSING` snapshots are rejected. The pure due planner reports an
initial/refinement window as due, waiting, or missed; it never widens a window
or performs work.

## Identity and transitions

The stable logical lifecycle identity binds the versioned timing-contract
digest, league, fixture, market/outcome, candidate, and model identity. Capture
and generation timestamps do not create a new logical signal. The INITIAL
record is retained verbatim when an explicit caller decision appends a
REFINED or WITHDRAWN record. Refinement requires a new snapshot and carries the
initial version digest as its predecessor. Classification
(`STRENGTHENED`, `WEAKENED`, or `UNCHANGED`) is supplied by the caller; there is
no implicit score threshold or re-ranking rule. An ineligible refinement
cannot erase INITIAL: it remains as-is unless a separately explicit withdrawal
decision is provided.

Each record binds the same candidate/model and source, Research, and model
artifact digests. It is always `no_bet=true`, `publication_enabled=false`, and
`activation_enabled=false`. This is a record lifecycle, not an authority or
publication lifecycle.

## Due planning and persistence

`plan_signal_lifecycle()` is deterministic and side-effect free. It reports
the next stage from kickoff time and lifecycle state. No scheduler entry or
workflow is added here.

`Top5SignalLifecycleStore` stores state through the existing external runtime
state resolver with `require_external=True`, under
`football/top5/signal_lifecycle/<logical-lifecycle-id>.json`. The store uses a
per-lifecycle file lock, atomic replacement, restrictive file mode, exact
schema/digest validation, and append-only transitions. Repeating the same
payload is idempotent; a conflicting replay, non-append update, or digest
corruption fails closed. Tests redirect this store to temporary external
state only.

## Deliberate integration boundary

The caller is responsible for deciding that a candidate may be recorded and
for supplying each probability/classification. No provider call, retry,
activation, publication, betting, or production runtime state mutation is
performed. A later explicit architecture decision is still required before
these timing values can be treated as approved production Signal-Time values.
