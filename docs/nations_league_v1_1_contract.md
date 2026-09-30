# Nations League v1.1 contract

`nations_league_v1_1` is the corrected, research-only successor to the frozen
`nations_league_v1` contract. The old module and its materialized digest remain
unchanged. The successor does not reinterpret, migrate, or qualify old forward
records.

## Frozen training universe

The successor freezes the strict causal timeline subset retained by the #214
replay and the #215 canonical timeline:

- competition: `UEFA Nations League` only;
- timeline schema: `uefa-nations-league-fixture-timeline-v1`;
- timeline dataset digest:
  `2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef`;
- timeline source head:
  `065c6b40eb9911df3703d2e3079730a556136ee3`;
- 512 canonical fixture rows, of which 510 have source-backed result-safe
  timestamps and 2 are explicit administrative exceptions;
- training uses the 510 ordinary rows only, in result-safe timestamp and
  canonical fixture-id order.

The old v1 label `UEFA competitive` is not mapped or carried forward. No
training universe outside the retained canonical Nations League timeline is
claimed. The earliest kickoff is `2020-09-03T16:00:00Z`, the earliest
result-safe bound is `2020-09-03T22:00:00Z`, and the latest kickoff is
`2025-06-08T19:00:00Z`.

`result_safe_available_at` must be a UTC instant strictly after kickoff and
strictly before the prediction cutoff. Administrative exceptions remain in the
timeline for auditability, but cannot enter training or become targets.

## Model and lifecycle

The causal Elo algorithm and its approved parameters are reused from v1:
initial rating, home advantage, draw band, Nations League K factor, goal-
difference update, and neutral-site handling. No calibration, GBT/context,
market, squad, lineup, or subjective motivation input is allowed. Lifecycle
windows remain INITIAL at 22–26 hours and REFINEMENT at 60–120 minutes before
kickoff. All records are `SHADOW_ONLY`, `no_bet=true`, unpublished, and have
no ledger mutation.

Training and target validation are separate. Both require the canonical
competition, but training additionally requires result-safe scores and source
provenance; administrative rows are excluded. Targets require canonical
fixture provenance and cannot be administrative rows.

## Supersession and migration

The old digest is
`f55549e7225f55deac23c7a31b757acf509ad0b4810b93ba8244301d3395a8ee`.
It is superseded before real forward evidence because its declared training
universe is internally inconsistent and includes an unsupported competition
label. The new digest is
`50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626`.

The deterministic artifact
`results/audits/nations_league_v1_supersession_v1_1.json` records this status.
There are no real v1 forward predictions or outcomes, no promotion evidence is
transferred, and historical research remains background only. v1.1 record IDs
include the v1.1 model version and digest, so old and new records cannot collide.

This module is a narrow migration seam. It does not rewrite the existing
forward-evidence campaign or settlement artifacts, and it has no provider,
credential, publication, activation, betting, or production authority.
