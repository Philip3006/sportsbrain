# Champions League model research readiness

Status: **BLOCKED for shadow scoring**. This is an offline research/evidence
contract only. It does not fetch data, call an odds provider, train a
production model, publish a signal, or touch runtime/financial stores.

## Repository finding

There is no committed Champions League match or prediction artifact in this
worktree. The existing pipeline is reusable only in bounded pieces:

| Existing component | CL research use | Gate finding |
| --- | --- | --- |
| `src/models/elo.py` | chronological Elo baseline | compatible after a CL event-time result table and venue metadata exist |
| `src/models/dixon_coles.py` | goal-model candidate | compatible after causal CL rows and kickoff-safe parameter snapshots exist |
| `src/models/lgbm_model.py` | challenger family | blocked until the feature vector has point-in-time provenance |
| `src/features/builder.py` | feature assembly | not CL-safe as-is; its odds and snapshot inputs do not carry a full cutoff contract |
| `src/data/football_data.py` | domestic football-data.co.uk loader | does not enumerate Champions League |
| `src/data/international.py` | broad international-results loader | network-backed and not a CL point-in-time research pipeline |

The machine-readable contract is implemented in
`src/football/champions_league_research.py`.

## Sealed partitions

The default `PartitionSpec` is season-based and mutually exclusive:

| Partition | Seasons | Permitted use |
| --- | --- | --- |
| Development | 2015-16 through 2021-22 | fit and walk-forward development only |
| Calibration | 2022-23, 2023-24 | choose calibration/configuration, never fit on final |
| Final | 2024-25 | untouched until the candidate and calibration rules are frozen; score once for final evidence |
| Shadow | 2025-26 | optional future shadow evidence; never used to select a candidate |

Rows without an explicit season, or with a season outside this specification,
are rejected. The final partition is not used for selection. A final score is
allowed only after the artifacts and calibration decisions have been frozen.
The code sorts deterministically by season, kickoff/date, match identity, and
input order.

Required input fields are:

```text
season, kickoff_at, home_team, away_team, home_score, away_score
```

`kickoff_at` must be timezone-aware for a strict timestamp audit. A calendar
date is not an acceptable substitute for a source release timestamp.

## Feature timestamp audit

The strict audit treats a source timestamp at or after kickoff as leakage and
treats a date-only source as blocked because it cannot prove that the source
was published before kickoff. The current code audit records:

- rolling form, momentum, load, H2H, and Elo as conditional: they use
  `date < match_date` but do not retain exact same-day event ordering;
- Dixon-Coles snapshots as blocked: snapshot selection uses `<= match_date`;
- odds as blocked: the existing lookup has no `captured_at`/`source_timestamp`;
- market values as blocked: the source is a current hard-coded table, not an
  historical as-of snapshot;
- squad availability as blocked: current/default caches lack release-time
  attestation;
- StatsBomb, FotMob, and PPDA features as blocked until publication/as-of
  timestamps are retained;
- fixture metadata (`neutral`, tournament context) as the only current pass.

`audit_feature_timestamps()` is the row-level check. In strict mode it returns
`BLOCKED` for missing, unzoned, same/after-kickoff, and date-only timestamps.

## Baselines and compatible families

The deterministic candidate manifest registers these families:

- `uniform_prior_v1`: parameter-free sanity baseline;
- `empirical_prior_v1`: outcome prior fit only on permitted pre-score rows;
- `elo_v1`: compatible chronological football baseline, conditional on
  event-time CL results;
- `dixon_coles_v1`: compatible goal-model family, conditional on causal
  snapshots;
- `market_prior_v1`: blocked until point-in-time CL 1X2 odds are supplied;
- `hist_gbm_v1`: blocked until the feature timestamp audit passes.

`evaluate_predictions()` reports multiclass log loss, ten-bin confidence ECE,
and mean absolute class calibration error by candidate and season. It does not
invent metrics: with the repository's current state, the readiness report is
`BLOCKED_NO_CL_DATA_OR_PREDICTIONS` and no season metrics are reported.

## Deterministic manifest and commands

The committed manifest is `docs/champions_league_candidate_manifest.json`. It
contains only allow-listed source hashes, the partition policy, the feature
audit, candidate status, and the explicit missing-input finding. It has no
absolute paths, wall-clock fields, provider output, or runtime data.

Regenerate or validate the same artifact locally with:

```bash
python3 scripts/champions_league_research.py manifest \
  --root . \
  --output docs/champions_league_candidate_manifest.json
python3 scripts/champions_league_research.py report --root .
```

To score supplied offline evidence, pass JSON lists (or objects with
`matches`/`predictions` fields) explicitly:

```bash
python3 scripts/champions_league_research.py report \
  --root . \
  --matches /path/to/cl_matches.json \
  --predictions /path/to/cl_predictions.json \
  --output /path/to/cl_readiness_report.json
```

No provider, production, or private-data access is part of this workflow.
