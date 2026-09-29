# UEFA Nations League Competition-Context Ablation

- Status: `NL_CONTEXT_BLOCKED`
- Competition-state SHA-256: `4283e4246e1aa67e03d5c90dd8d3d22568420e6adfb9f1f3893a849f163520c3`
- Baseline source SHA-256: `unavailable`
- Source main SHA: `98e992621c7eb5c860456b8264ffd1692c652f1e`
- Eligible fixtures: 512
- Paired OOS fixtures: 0
- OOS coverage: 0.000
- Synthetic evidence used: false

- B4 artifact state: `unmerged_builder4_intermediate_not_ready`

## Blockers

- `B4_away_mathematical_goal_primitives_missing`
- `B4_away_qualification_state_not_team_bound`
- `B4_away_relegation_state_not_team_bound`
- `B4_complete_remaining_group_schedule_missing`
- `B4_home_mathematical_goal_primitives_missing`
- `B4_home_qualification_state_not_team_bound`
- `B4_home_relegation_state_not_team_bound`
- `B4_kickoff_timestamp_missing`
- `B4_league_phase_matchday_missing`
- `B4_official_schedule_coverage_not_verified`
- `B4_qualification_state_unresolved`
- `B4_relegation_state_unresolved`

## Excluded / unavailable

- empirical metrics: withheld until B4 dataset readiness and exact causal baseline join
- subjective motivation: not defined or used
- causal GBT/stacker: no independently verified row-level replay supplied

Empirical metrics are unavailable. The canonical Builder 4 competition-state artifact and a complete causal baseline join are required; no synthetic result is substituted.

Stacker-test recommendation: Do not include until a canonical B4 dataset is ready and the paired causal ablation is complete.

Research-only result. No production hook, activation, publication, betting, or ledger path is changed.
