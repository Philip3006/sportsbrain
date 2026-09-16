# Top-5 real-shadow measurement pack V1

`top5_real_shadow_measurement.py` is a deterministic, read-only consumer of
the merged Top-5 real-shadow auditor. It measures only eligible
`REAL_OBSERVED` prediction/result lifecycles that retain the frozen M5 and
Research bindings. It never generates predictions, calls a provider, issues a
qualification receipt, publishes, bets, writes a ledger, or authorizes
production.

## Admission boundary

The canonical auditor validates the Builder2QualificationReceiptV1, observation,
session, prediction, result, closing, evidence bundle, and immutable digests.
The measurement pack then admits only real observations with a final result,
valid provenance, valid identity, valid digests, and a valid lifecycle. A
pending closing attachment does not remove a prediction from outcome metrics;
it makes the closing benchmark unavailable for that prediction. Non-real,
pending-result, conflicting, malformed, or mismatched evidence is excluded
with an explicit reason and never enters totals.

At cohort level, divergent duplicate prediction identities and mixed Research
or model identities fail the measurement closed. The report retains exclusion
evidence and emits no partial metrics for a failed cohort.

## Metrics and grouping

The JSON and Markdown reports include deterministic N, multiclass Brier score,
log loss, top-probability accuracy, mean predicted probability of the realized
outcome, outcome and predicted-class distributions, confidence histograms,
calibration bins, and descriptive calibration summaries. Descriptive groups
are provided by league, provider, experiment, controlled run, qualification
session, and canonical bookmaker where present. These are summaries, not
provider rankings or production thresholds.

Closing odds are a separate `BENCHMARK / CLV MEASUREMENT ONLY` section. It
reports signal prediction probability against normalized closing implied
probability and signed probability deltas. Closing values are never admitted
as prediction inputs, retraining data, or production decision criteria.

Every report carries deterministic audit-input, cohort, and measurement
digests, excluded evidence, and immutable safety statements:

```text
production_activation_authorized = false
publication_authorized = false
betting_authorized = false
model_approved_for_production = false
signal_time_approved_for_production = false
```

## CLI

```text
python3 scripts/top5_shadow_measure.py session session.json --evidence evidence.json
python3 scripts/top5_shadow_measure.py directory shadow-artifacts/
```

Use `--format json`, `--format markdown`, or `--format both`. Input is local
JSON only. The CLI has no write path and rejects ledger paths through the
auditor's loader.
