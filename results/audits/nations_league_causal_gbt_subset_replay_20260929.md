# Nations League causal GBT verified-timeline subset replay

Status marker: **NL_CAUSAL_GBT_SUBSET_NO_GAIN**

Interim research-only artifact. This is not final 512-fixture causal certification and must not advance to a stacker.

- PR #215 head: `71511e4fd9faec2e9aefbd97b644b7789f80171d`
- Timeline dataset digest: `842c0cc608b4221d63cbda079756dc392524e53349c9c24af5e8116c1d27e1af`
- Evaluated: 156 / 512
- Excluded: 356
- Coverage by edition: {'2020/21': {'timeline_rows': 166, 'verified_kickoffs': 4, 'result_safe_training_rows': 4, 'evaluated': 0, 'excluded': 166}, '2022/23': {'timeline_rows': 160, 'verified_kickoffs': 138, 'result_safe_training_rows': 138, 'evaluated': 0, 'excluded': 160}, '2022/23-delayed-relegation-playoffs': {'timeline_rows': 2, 'verified_kickoffs': 0, 'result_safe_training_rows': 0, 'evaluated': 0, 'excluded': 2}, '2024/25': {'timeline_rows': 184, 'verified_kickoffs': 156, 'result_safe_training_rows': 155, 'evaluated': 156, 'excluded': 28}}
- Training policy: verified `result_safe_available_at < model_training_cutoff`; no date-only timing fallback.

## Metrics

| Model | N | Brier | Log loss | ECE | Accuracy | Sharpness |
|---|---:|---:|---:|---:|---:|---:|
| Elo | 156 | 0.638616 | 1.066157 | 0.106082 | 0.435897 | 0.547539 |
| Dixon–Coles | 156 | 0.711396 | 1.193474 | 0.102702 | 0.410256 | 0.529202 |
| GBT | 156 | 0.670132 | 1.108662 | 0.096151 | 0.384615 | 0.476211 |

H/D/A calibration and paired date-cluster bootstrap intervals are in the JSON audit artifact.

GBT-minus-baseline: `{'interpretation': 'Point estimates only; this interim subset is not final causal certification.', 'paired_intervals_available': True, 'gbt_minus_dixon_coles_brier': -0.041264099031152246, 'gbt_minus_elo_brier': 0.031516799168977205, 'gbt_minus_dixon_coles_log_loss': -0.08481209221553021, 'gbt_minus_elo_log_loss': 0.04250476929091129}`
Previous PR #214 comparison: `{'target_fixture_count': 512, 'common_fixture_count': 512, 'metrics': {'dixon_coles': {'accuracy_secondary': 0.501953125, 'calibration_home_draw_away': {'away': {'brier_one_vs_rest': 0.20170931190801203, 'ece_10_bins': 0.059492407481029144, 'mean_probability': 0.30725507918818296, 'mean_probability_minus_frequency': -0.022823045811817044, 'observed_frequency': 0.330078125}, 'draw': {'brier_one_vs_rest': 0.19044768475948146, 'ece_10_bins': 0.048435641668758064, 'mean_probability': 0.268582079578002, 'mean_probability_minus_frequency': 0.014675829578002009, 'observed_frequency': 0.25390625}, 'home': {'brier_one_vs_rest': 0.21132148239329296, 'ece_10_bins': 0.049447093895623365, 'mean_probability': 0.424162841233815, 'mean_probability_minus_frequency': 0.00814721623381498, 'observed_frequency': 0.416015625}}, 'ece_10_bins_mean_one_vs_rest': 0.052458381015136855, 'matches_evaluated': 512, 'mean_max_probability_sharpness': 0.5608508915098052, 'multiclass_brier': 0.6034784790607864, 'multiclass_log_loss': 1.011464472717702}, 'elo': {'accuracy_secondary': 0.52734375, 'calibration_home_draw_away': {'away': {'brier_one_vs_rest': 0.19679974630128017, 'ece_10_bins': 0.04230572088367831, 'mean_probability': 0.3278826064011867, 'mean_probability_minus_frequency': -0.002195518598813284, 'observed_frequency': 0.330078125}, 'draw': {'brier_one_vs_rest': 0.19891375167486702, 'ece_10_bins': 0.11312225836491233, 'mean_probability': 0.1407839916350877, 'mean_probability_minus_frequency': -0.1131222583649123, 'observed_frequency': 0.25390625}, 'home': {'brier_one_vs_rest': 0.2117389244295521, 'ece_10_bins': 0.11531777696372561, 'mean_probability': 0.5313334019637257, 'mean_probability_minus_frequency': 0.1153177769637257, 'observed_frequency': 0.416015625}}, 'ece_10_bins_mean_one_vs_rest': 0.09024858540410541, 'matches_evaluated': 512, 'mean_max_probability_sharpness': 0.6099316526872447, 'multiclass_brier': 0.6074524224056992, 'multiclass_log_loss': 1.034127016440448}, 'gbt': {'accuracy_secondary': 0.517578125, 'calibration_home_draw_away': {'away': {'brier_one_vs_rest': 0.20031112670902051, 'ece_10_bins': 0.057034887918522674, 'mean_probability': 0.3113881925889336, 'mean_probability_minus_frequency': -0.018689932411066423, 'observed_frequency': 0.330078125}, 'draw': {'brier_one_vs_rest': 0.18927015872744507, 'ece_10_bins': 0.042914880809345954, 'mean_probability': 0.24377992930090653, 'mean_probability_minus_frequency': -0.010126320699093472, 'observed_frequency': 0.25390625}, 'home': {'brier_one_vs_rest': 0.2037661232444547, 'ece_10_bins': 0.06009035486240097, 'mean_probability': 0.4448318781101599, 'mean_probability_minus_frequency': 0.028816253110159895, 'observed_frequency': 0.416015625}}, 'ece_10_bins_mean_one_vs_rest': 0.053346707863423194, 'matches_evaluated': 512, 'mean_max_probability_sharpness': 0.5742977590655375, 'multiclass_brier': 0.5933474086809203, 'multiclass_log_loss': 1.0029004601186795}}, 'brier_delta_gbt_minus_dixon_coles': -0.010131070379866158}`

No provider requests, credentials, production/runtime changes, activation, publication, betting, or ledger activity occurred.

Wait for `NL_FIXTURE_TIMELINE_READY` before final certification.
