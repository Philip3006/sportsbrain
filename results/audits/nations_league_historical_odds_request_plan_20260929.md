# Nations League historical odds request plan

`NL_HISTORICAL_ODDS_FINAL_PLAN_READY`

Offline deterministic plan only; no provider request or credential access occurred.

- PR #215 head: `065c6b40eb9911df3703d2e3079730a556136ee3`
- Timeline digest: `2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef`
- Request-plan digest: `b231bda6e22566529c0747cc8c110cb7049ba574b63a6892f883ac7015ddfb02`
- Historical coverage boundary: `2022-06-11T00:25:00Z`
- Fixture universe: `512`; before coverage: `233`; after coverage / odds eligible: `279`
- Administrative exceptions: `2`; genuinely unusable: `0`

## Plan totals

| Plan | Phase | Fixtures | Unique timestamps | HTTP requests | Credits | Dedup savings |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| PREDICTION_ONLY | INITIAL | 269 | 72 | 72 | 720 | 197 requests |
  - phase exclusions: `10` (snapshot before provider coverage boundary)
| PREDICTION_ONLY | REFINEMENT | 279 | 75 | 75 | 750 | 204 requests |
| PREDICTION_ONLY | DEDUPLICATED TOTAL | 548 | 147 | 147 | 1470 | 401 requests |
| FULL_RESEARCH | INITIAL | 269 | 72 | 72 | 720 | 197 requests |
  - phase exclusions: `10` (snapshot before provider coverage boundary)
| FULL_RESEARCH | REFINEMENT | 279 | 75 | 75 | 750 | 204 requests |
| FULL_RESEARCH | CLOSING | 279 | 75 | 75 | 750 | 204 requests |
| FULL_RESEARCH | DEDUPLICATED TOTAL | 827 | 182 | 182 | 1820 | 645 requests |

## Edition coverage

| Edition | Fixtures | Before | After | Admin | Eligible |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2020/21 | 166 | 166 | 0 | 1 | 0 |
| 2022/23 | 162 | 67 | 95 | 0 | 95 |
| 2024/25 | 184 | 0 | 184 | 1 | 184 |

## Administrative exceptions

The pre-coverage exception is excluded. The post-coverage exception remains eligible for pre-match odds retrieval; result-safe timing is not needed for an odds request.
- `uefa-nl:c07e17c2e1a35c4b056be0aa` Switzerland–Ukraine: `BEFORE_PROVIDER_COVERAGE` — before_historical_coverage_boundary
- `uefa-nl:63f926e0ced3f284c077ee10` Romania–Kosovo: `AFTER_PROVIDER_COVERAGE_ADMINISTRATIVE_EXCEPTION` — pre_match_odds_not_dependent_on_result_safe_timestamp

The JSON artifact contains all 512 classifications and every exact timestamp-to-fixture bulk request mapping.
