# Champions League dataset readiness

Status: **blocked for model research**. This is a local, read-only research
audit. It does not fetch providers, access production or financial state, train
a model, or alter any dataset.

## Current repository finding

The repository has no committed historical match dataset under
`data/champions_league/`. The manifest therefore records
`BLOCKED_NO_CL_DATA`, zero CL rows, and no season coverage. Existing loaders are
reference evidence only:

| Local reference | Finding |
| --- | --- |
| `src/data/football_data.py` | Enumerates domestic football-data.co.uk leagues; no CL league code or local CL snapshot. |
| `src/data/international.py` | Network-backed broad international results; no committed, point-in-time CL dataset. |
| `src/data/statsbomb.py` | Configured for World Cup, UEFA Euro, and Copa America competition IDs, not Champions League. |
| `src/data/football_discovery.py` / `src/data/odds_api.py` | Live/discovery access only; no historical CL evidence. |

The machine-readable result is
`docs/champions_league_dataset_readiness_manifest.json`. It contains content
hashes for every local dataset and reference file, so it has no wall-clock or
absolute-path fields and can be regenerated deterministically.

## Required dataset contract

Each JSON list/object or CSV row must provide:

```text
season, kickoff_at, home_team, away_team, home_score, away_score
```

`fixture_id`/`match_id` and `result_id` are recorded when supplied. Without a
stable fixture ID, the auditor reports a deterministic derived identity but
keeps the mapping conditional. Duplicate fixture keys, conflicting results,
missing scores, or missing teams are blocked.

Kickoff and `source_timestamp` must be timezone-aware instants. A date-only,
un-zoned, missing, or same/after-kickoff source timestamp cannot prove that a
feature was available before the prediction cutoff. Known feature groups are
audited independently: 1X2 odds, pre-match Elo, rolling form, xG, PPDA, squad
availability, and market value.

## Sealed partitions

| Partition | Seasons | Allowed use |
| --- | --- | --- |
| Development | 2015-16 through 2021-22 | fit and chronological development only |
| Calibration | 2022-23, 2023-24 | configuration/calibration only |
| Final | 2024-25 | read-only; never fit or select on it; score only after freeze |
| Shadow | 2025-26 | optional shadow evidence; never candidate selection |

The audit only reads rows and reports hashes/counts. The final guard in the
manifest explicitly sets `feature_fit_allowed`, `mapping_fit_allowed`, and
`mutated_by_audit` to false.

## Local commands

Audit the expected directory:

```bash
python3 scripts/champions_league_dataset_audit.py manifest \
  --root . \
  --output docs/champions_league_dataset_readiness_manifest.json
```

Audit explicitly supplied local files without network access:

```bash
python3 scripts/champions_league_dataset_audit.py audit \
  --root . \
  --dataset data/champions_league/matches.json
```

The tool refuses paths outside the audit root and private/production/financial
path components. A blocked or conditional result is evidence, not permission
to fetch or infer missing data.
