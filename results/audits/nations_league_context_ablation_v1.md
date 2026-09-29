# UEFA Nations League Competition-Context Ablation

- Status: `NL_SAFE_CONTEXT_REGRESSION`
- Classification: `material_log_loss_or_calibration_regression`
- B4 source state: `NL_COMPETITION_STATE_PARTIAL`
- B4 safe-subset integrity passed: true
- Competition-state SHA-256: `354d5c542f0646bf0914b17a1361f4d5723db2b1f117f6d6375a8579c44b7edf`
- B4 source commit: `037eb99148a39d57bd4aa09624ddc4841403d2b1`
- B4 source PR: `215`
- B4 coverage digest: `11f415e9f1964c3d034055bb62f49e0e78b46ebdc3bb9e09745a545ede441333`
- B4 timeline digest: `2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef`
- Baseline source SHA-256: `6b47d79b84891306d8bb2ce4c0abec810b11818c6e4c3fad9fa2415c228b7f2d`
- Source main SHA: `42b475d86c29aa99be7105f63fb380167c1cb1dc`
- Canonical B4 fixture identities: 512
- Verified kickoffs / exact causal cutoffs: 512 / 512
- Local result identity / baseline forecast coverage: 512 / 510
- Played fixtures / administratively decided outcomes excluded: 510 / 2
- Paired OOS fixtures after expanding-window warm-up: 478 / 510 (0.937)
- Synthetic evidence used: false

- Administrative outcome fixture IDs excluded from scoring and DC training: `uefa-nl:63f926e0ced3f284c077ee10`, `uefa-nl:c07e17c2e1a35c4b056be0aa`

## Excluded / unavailable

- `inferred_matchday`: not present in #215 timeline evidence; never inferred
- `official_uefa_fixture_ids`: unavailable and not a predictive feature
- `article_15_tiebreak_outputs`: unresolved or missing source inputs
- `disciplinary_and_access_list_tiebreaks`: no source data in the B4 artifact
- `c_league_relegation_allocation`: edition-specific allocation unresolved
- `exact_must_win_and_qualification_labels`: not proven by the partial points-bound artifact
- `subjective_motivation`: not defined or used
- `causal_gbt`: no certified row-level causal GBT forecast artifact supplied on current main or #216

## Included safe context

- Numeric: `home_matches_played_before`, `away_matches_played_before`, `home_points_before`, `away_points_before`, `points_diff_home_minus_away`, `home_goals_for_before`, `away_goals_for_before`, `home_goals_against_before`, `away_goals_against_before`, `home_goal_difference_before`, `away_goal_difference_before`, `home_points_per_game_before`, `away_points_per_game_before`, `home_remaining_games_before`, `away_remaining_games_before`, `home_table_position_min`, `home_table_position_max`, `away_table_position_min`, `away_table_position_max`, `home_table_position_tied`, `away_table_position_tied`, `home_points_bound_promotion_possible`, `away_points_bound_promotion_possible`, `home_points_bound_relegation_possible`, `away_points_bound_relegation_possible`
- Categorical: `league_tier`, `group`

## Current causal DC baseline on paired OOS rows

These are the unmodified event-level Dixon–Coles probabilities on the same paired OOS rows; the paired A/B table below compares the fold-local baseline-only head with that same head plus safe context.

| Metric | Raw causal DC |
|---|---:|
| Multiclass Brier | 0.600749 |
| Multiclass log loss | 1.006978 |
| ECE (10-bin macro OVR) | 0.050353 |
| Sharpness (mean max p) | 0.564374 |
| Accuracy (secondary) | 0.495816 |

## Primary paired metrics

| Metric | Baseline-only head | Baseline + context head | Context − baseline |
|---|---:|---:|---:|
| Multiclass Brier | 0.610821 | 0.730368 | +0.119547 |
| Multiclass log loss | 1.020344 | 1.361138 | +0.340794 |
| ECE (10-bin macro OVR) | 0.055010 | 0.164047 | +0.109037 |
| Sharpness (mean max p) | 0.513146 | 0.668603 | +0.155457 |

## Outcome calibration

| Outcome | Baseline-only mean p / observed / abs gap | Context mean p / observed / abs gap | Δ absolute gap |
|---|---:|---:|---:|
| Home | 0.3680 / 0.4247 / 0.0567 | 0.4108 / 0.4247 / 0.0139 | -0.0428 |
| Draw | 0.2696 / 0.2531 / 0.0165 | 0.2690 / 0.2531 / 0.0158 | -0.0007 |
| Away | 0.3624 / 0.3222 / 0.0402 | 0.3202 / 0.3222 / 0.0020 | -0.0382 |

## Paired date-cluster bootstrap (95% CI)

- Brier difference: [0.077058, 0.164126]
- Log-loss difference: [0.217103, 0.477750]

## Strata

| Stratum | Level | n | Status | Δ Brier | Δ log loss |
|---|---|---:|---|---:|---:|
| group_phase_progress | early_by_matches_played | 209 | reported | +0.122619 | +0.422263 |
| group_phase_progress | middle_by_matches_played | 85 | reported | +0.155794 | +0.313155 |
| group_phase_progress | late_by_matches_played | 146 | reported | +0.089454 | +0.246845 |
| group_phase_progress | not_group_stage_or_unavailable | 38 | reported | +0.137196 | +0.315506 |
| points_gap_band | level | 118 | reported | +0.043362 | +0.198206 |
| points_gap_band | tight_1_to_3 | 221 | reported | +0.166515 | +0.470684 |
| points_gap_band | wide_4_plus | 139 | reported | +0.109546 | +0.255325 |
| points_bound_constraint | no_closed_supported_points_bound | 279 | reported | +0.118787 | +0.381484 |
| points_bound_constraint | constrained_by_supported_points_bound | 161 | reported | +0.116699 | +0.276252 |
| points_bound_constraint | unavailable | 38 | reported | +0.137196 | +0.315506 |
| league_tier | C | 134 | reported | +0.040370 | +0.220151 |
| league_tier | D | 44 | reported | +0.324565 | +0.854746 |
| league_tier | B | 127 | reported | +0.120091 | +0.272860 |
| league_tier | A | 155 | reported | +0.140833 | +0.388303 |
| league_tier | C/D | 2 | insufficient_sample | +0.022903 | -0.035398 |
| league_tier | C/B | 4 | insufficient_sample | +0.115000 | +0.188109 |
| league_tier | B/A | 4 | insufficient_sample | +0.096360 | +0.230455 |
| league_tier | B/C | 4 | insufficient_sample | -0.012354 | -0.035238 |
| league_tier | A/B | 4 | insufficient_sample | -0.117358 | -0.128001 |
| edition | 2020/21 | 133 | reported | +0.201365 | +0.782703 |
| edition | 2022/23 | 162 | reported | +0.087087 | +0.189372 |
| edition | 2024/25 | 183 | reported | +0.088819 | +0.153672 |
| observed_outcome | home | 203 | reported | +0.056625 | +0.116067 |
| observed_outcome | draw | 121 | reported | +0.162524 | +0.587198 |
| observed_outcome | away | 154 | reported | +0.168722 | +0.443423 |

Stacker-test recommendation: Do not include as a stacker candidate based on this result; no supported safe-context gain was established.

Research-only result. No production hook, activation, publication, betting, or ledger path is changed.
