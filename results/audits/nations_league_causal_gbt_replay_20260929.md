# Nations League causal GBT walk-forward replay

Research status: **NL_CAUSAL_GBT_BLOCKED**

This is an offline research artifact only. The date-only input lacks source-proven kickoff, schedule-publication, and result-publication timestamps. Metrics below are descriptive under conservative date-bound assumptions and are not certified point-in-time OOS evidence.

- Evaluation fixtures: 512 (common to all models: 512)
- Results source SHA-256: `6b47d79b84891306d8bb2ce4c0abec810b11818c6e4c3fad9fa2415c228b7f2d`
- Competitive training rows available in local source: 19498
- Prediction lead: 7 days; assumed result availability upper bound: 3 days
- Refresh cadence: once before each declared Nations League evaluation block
- GBT: sklearn 1.9.0 HistGradientBoostingClassifier; max_iter=250, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=60, L2=2.0, seed=20260929; random internal early stopping disabled
- Feature schema digest: `99c080c285cad92e9142a5a6fee7bede1e20ee33aeaa61a63ff0aa8ced6c8039`
- Model config digest (first block): `466b6a7834df1cccdc5cdbd9ed26df32746c236dd52a575df0d39cbeb5043dc7`
- Markets/odds: not used; no stacker

## Overall metrics (identical fixture set)

| Model | N | Brier | Log loss | ECE | Accuracy (secondary) | Mean max probability |
|---|---:|---:|---:|---:|---:|---:|
| Elo | 512 | 0.607452 | 1.034127 | 0.090249 | 0.527344 | 0.609932 |
| Dixon–Coles | 512 | 0.603478 | 1.011464 | 0.052458 | 0.501953 | 0.560851 |
| Causal-envelope GBT | 512 | 0.593347 | 1.002900 | 0.053347 | 0.517578 | 0.574298 |

## Home / draw / away calibration

| Model | Outcome | Mean predicted | Observed | Difference | OVR Brier | ECE |
|---|---|---:|---:|---:|---:|---:|
| Elo | Home | 0.531333 | 0.416016 | +0.115318 | 0.211739 | 0.115318 |
| Elo | Draw | 0.140784 | 0.253906 | -0.113122 | 0.198914 | 0.113122 |
| Elo | Away | 0.327883 | 0.330078 | -0.002196 | 0.196800 | 0.042306 |
| Dixon–Coles | Home | 0.424163 | 0.416016 | +0.008147 | 0.211321 | 0.049447 |
| Dixon–Coles | Draw | 0.268582 | 0.253906 | +0.014676 | 0.190448 | 0.048436 |
| Dixon–Coles | Away | 0.307255 | 0.330078 | -0.022823 | 0.201709 | 0.059492 |
| GBT | Home | 0.444832 | 0.416016 | +0.028816 | 0.203766 | 0.060090 |
| GBT | Draw | 0.243780 | 0.253906 | -0.010126 | 0.189270 | 0.042915 |
| GBT | Away | 0.311388 | 0.330078 | -0.018690 | 0.200311 | 0.057035 |

## Paired date-cluster bootstrap

Lower scores are better; negative GBT-minus-baseline favors GBT.

### gbt_minus_dixon_coles
- multiclass_brier: mean -0.010178; 95% CI [-0.039064, 0.016626]
- multiclass_log_loss: mean -0.008617; 95% CI [-0.053104, 0.034484]

### gbt_minus_elo
- multiclass_brier: mean -0.014570; 95% CI [-0.034632, 0.005245]
- multiclass_log_loss: mean -0.031744; 95% CI [-0.067811, 0.003890]

## Evaluation blocks

| Block | N | Elo Brier | DC Brier | GBT Brier | GBT log loss | GBT ECE |
|---|---:|---:|---:|---:|---:|---:|
| 2020/21 | 166 | 0.628431 | 0.631003 | 0.620794 | 1.032373 | 0.095392 |
| 2022/23 | 160 | 0.652633 | 0.629580 | 0.621603 | 1.061079 | 0.094565 |
| 2022/23-delayed-relegation-playoffs | 2 | 0.223824 | 0.050955 | 0.021117 | 0.112005 | 0.069541 |
| 2024/25 | 184 | 0.553408 | 0.561955 | 0.550236 | 0.935404 | 0.055472 |

## Measured uplift assessment

The descriptive full-set GBT-minus-DC Brier delta is -0.010131; improvement appears in every declared block, but the paired 95% interval includes zero. The Log Loss point delta is -0.008564, with uncertainty also spanning zero. Aggregate ECE delta is +0.000888; no calibration noninferiority threshold was prespecified, so class calibration is shown above without a post-hoc gate.

Descriptively this is promising, not confirmed; as causal evidence it remains blocked by absent point-in-time timestamp provenance. It does not justify advancing to a stacker.

## Feature policy

Included: as-of Elo, prior competitive-result form/rolling scoring, momentum, match load/rest, and prior head-to-head. DC-derived features and tournament stage are UNAVAILABLE for the GBT input. Current squad/market/static data, injuries, later ratings, xG, and odds are excluded.

## Limitations and decision

The ordering checks use an explicit seven-day simulated cutoff, a UTC kickoff-date envelope, and a three-day result-availability upper-bound assumption. The source has no exact kickoff, schedule-publication, or result-publication timestamps, so those bounds are not independently verified. The artifact must not be represented as strict timestamp-proven historical OOS evidence. No production or activation recommendation follows. Do not advance to a stacker on this artifact alone.

No network/provider calls, credentials, publication, activation, betting, or ledger operations were used.
