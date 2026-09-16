# Top-5 shadow experiment evidence matrix V1

`top5_shadow_experiment_matrix.py` consumes immutable Measurement Pack V1
outputs and produces neutral, deterministic evidence for future CEO review of
Signal-Time experiments. It does not choose, rank, approve, or recommend an
experiment, provider, Signal-Time, model, or production action.

## Admission and integrity

Each Measurement Pack digest, schema, frozen Research SHA, M5 identity,
cohort state, and immutable safety field is revalidated. Only its redacted
`REAL_OBSERVED` eligible-prediction records enter the matrix. Malformed
digests, non-real evidence, mixed identities, duplicate prediction IDs,
divergent artifacts, duplicate fixtures within an experiment, and conflicting
results fail the matrix closed. Measurement inputs are never modified.

## Descriptive evidence

The matrix reports deterministic experiment summaries and descriptive
breakdowns by league, provider, controlled run, and qualification session.
Metrics include eligible N, distinct fixtures, multiclass Brier, log loss,
top-probability accuracy, mean realized-outcome probability, calibration,
confidence, and outcome distributions. It emits no winner, ranking,
recommendation, production threshold, or Signal-Time approval.

Where experiments contain the same canonical fixture, paired deltas use the
explicit direction `experiment_A - experiment_B`. A deterministic paired
bootstrap percentile interval is reported only when at least two shared
fixtures exist; otherwise the interval is
`UNAVAILABLE / INSUFFICIENT_PAIRED_SAMPLE`. No significance claim is emitted.

Closing odds are reported only as:

```text
BENCHMARK / CLV MEASUREMENT ONLY
```

They are never prediction inputs, admission authority, training data, or a
basis for selecting an experiment.

## CLI

```text
python3 scripts/top5_shadow_experiment_matrix.py directory measurement-artifacts/
```

Use `--format json`, `--format markdown`, or `--format both`. The CLI is local,
read-only, and has no provider, network, ledger, publication, scheduler,
launchd, deployment, Cloudflare, or sealed-data path.
