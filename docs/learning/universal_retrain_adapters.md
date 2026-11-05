# Universal retrain adapters

The model-training half of the learning loop is represented by
`src.learning.retrain_adapters`.  A plan is created only from validated
`CausalTrainingRowV1` rows.  Signal wins/losses, published rows, and ledger
records are not inputs to a retrain decision.

Execution is opt-in and requires an injected trainer and validator.  The
candidate is written to a caller-owned staging directory, receives a
deterministic artifact digest and validation receipt, and never updates an
active artifact or pointer.  The default behavior is `PLAN_ONLY`.

## Audited coverage

“LGBM TRAINED?” means the adapter includes a causal LGBM/tree candidate when
explicit execution is requested; it does not claim that this PR ran training.
“LGBM ACTIVE?” describes the current inference state on `origin/main`.

| family | components in retrain plan | LGBM TRAINED? | LGBM ACTIVE? | governance/current-state evidence |
| --- | --- | --- | --- | --- |
| Tennis | Tennis LGBM, LGBM calibrator, separate signal meta-calibration | yes | yes | `scripts/tennis_train.py` and `src/tennis/ensemble.py` |
| 2. Bundesliga | Dixon–Coles, Elo, LGBM, calibrator | yes | no | `scripts/bundesliga2_scan.py` consumes LGBM only when `gate.json` passes; the committed gate is false |
| Generic/international football | Dixon–Coles, Elo, LGBM, cadence-gated stacker | yes | yes | global LGBM gate passes; the disabled legacy auto-retrain workflow is not re-enabled |
| Nations League v1.1 | active Elo release plus tree-model candidate | yes | no | `src/analysis/nations_league_v1_1.py` remains the active Elo contract; tree output is candidate-only |
| Top-5 | approved M5 market formula | no | no | M3/M4 LGBM candidates remain disabled; retraining returns `RETRAIN_BLOCKED_BY_GOVERNANCE` |

All families bind the complete sorted causal-row digest and result-identity
digest.  Duplicate rows are idempotent; conflicting identities, inconsistent
feature schemas, and result/settlement feature names fail closed.
