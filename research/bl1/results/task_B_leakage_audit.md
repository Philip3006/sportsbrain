# FLAGSHIP-BL1 — Task B: Data-Leakage Audit

Auditor: CLAUDE
Scope: Feature-availability audit for the BL1 challenger pipeline. Classifies every candidate feature by whether it can be reconstructed strictly as-of signal time (matchday T = t − ε).

## Classification legend

| Symbol | Meaning |
|---|---|
| **SAFE AS-OF** | Reproducible strictly from match rows with `date < T` and constants known before T |
| **SAFE W/ RECONSTRUCTION** | Requires as-of reconstruction (e.g. retrain DC per fold) but data is intrinsically available |
| **NOT SAFE** | Current codebase uses a future-anchored source; unsafe to reuse without refactor |
| **CLOSING-MARKET ONLY** | Available only from closing odds; benchmark/evaluation data, never a deployable prediction feature |
| **UNKNOWN** | Availability depends on external source coverage; needs empirical audit once data lands |

## Feature matrix

| Feature | Source in current codebase | Classification | Notes |
|---|---|---|---|
| DC 1X2 probabilities (`dc_p_home/draw/away`) | `dixon_coles.fit()` + `predict_match()` | **SAFE W/ RECONSTRUCTION** | Existing `train_lgbm_bundesliga2.py:278` loads global `params_latest.pkl` — **NOT SAFE for backfill**. Must refit DC per fold with `today = fold_cutoff` |
| DC attack/defence parameters | `DixonColesParams.attack/defence` | **SAFE W/ RECONSTRUCTION** | Same as above — per-fold refit required |
| DC lambdas | derived | **SAFE W/ RECONSTRUCTION** | derives from as-of params |
| Elo pre-match rating (`elo_home_pre`, `elo_away_pre`) | `compute_elo_series()` | **SAFE AS-OF** | Iterates chronologically; pre-match state is correctly frozen. Verified: `elo.py:72-90` uses only completed prior matches. **However:** current LGBM code (`train_lgbm_bundesliga2.py:189`) reads Elo via `elo_snap[home_col]` which doesn't exist on the frame → falls through to `ELO_DEFAULT=1500` (silent Elo-broken bug, feature is constant). |
| Rolling form (last N matches) | `_rolling_form()` | **SAFE AS-OF** | Uses `date < before_date` filter (line 60) |
| Venue form (home/away split) | `_venue_form()` | **SAFE AS-OF** | Same filter pattern |
| H2H home win-rate | `_h2h()` | **SAFE AS-OF** | Same filter pattern |
| Rest days since last match | `_days_rest()` | **SAFE AS-OF** | Same filter pattern |
| Momentum (short-window − long-window form) | derived | **SAFE AS-OF** | pure difference of as-of features |
| Rolling goals for/against | `_goals_avg()` | **SAFE AS-OF** | Same filter pattern |
| Home attendance ratio | `src/data/attendance.py::get_attendance_ratio` | **NOT SAFE** | Static snapshot from 25/26 basis; encodes future crowd data. BL1 teams (Bayern, Dortmund, Leipzig, etc.) are **not in the dict**, so the ratio collapses to `26,600/26,600 = 1.0` → uninformative noise. Exclude for BL1 until historical per-season attendance data is added. |
| Squad market value ratio | `src/data/market_values.py::get_market_value_ratio` | **NOT SAFE** | Static Transfermarkt snapshot as of June 2026 (see `market_values.py:8`). No BL1 club values present; would default to `100/100 = 1.0` for BL1 matches. Even if BL1 values were added, one snapshot cannot serve 2017–2026 backfill without severe leakage. Exclude for BL1 until historical per-season market-value snapshots are sourced. |
| European match load (Champions League / Europa League games in last 14 d) | not implemented | **SAFE W/ RECONSTRUCTION** | Requires external fixture list per season. Data available (football-data.co.uk has European league CSVs) but must be built. Explicit candidate for M3 vs M4. |
| xG | `src/features/xg_live.py` (StatsBomb) | **UNKNOWN** | StatsBomb open-data covers earlier seasons; live cache is future-anchored. Would need historical StatsBomb backfill. Excluded from BL1 v1. |
| Injuries / squad availability | `src/features/squad_context.py` | **NOT SAFE** | Current data source is live only. No historical injury feed. Exclude for BL1 v1. |
| Table position | not currently a feature | **SAFE W/ RECONSTRUCTION** | Trivial to compute from as-of match history |
| Promoted-team prior | not currently a feature | **SAFE W/ RECONSTRUCTION** | Requires research — see Task K |
| Opening odds `ps_open_*` | football-data.co.uk PSH/PSD/PSA | **UNKNOWN → measured after download** | Availability varies by season. Coverage report pending. |
| Current odds (near-kickoff) | TheOddsAPI | **UNKNOWN** | Historical retrieval not straightforward. Not planned for backfill. |
| Closing odds `ps_close_*` | football-data.co.uk PSCH/PSCD/PSCA | **CLOSING-MARKET ONLY** | Never a prediction feature. Used only for CLV metric + benchmark. |

## Confirmed leakage in current BL2 code — must NOT be reproduced for BL1

### L1 — Global DC params load in LGBM training (SEVERE)

File: `scripts/train_lgbm_bundesliga2.py:278-281`

```python
dc_path = MODELS_DIR / "dc_bundesliga2" / "params_latest.pkl"
dc_params = dixon_coles.load(dc_path)
```

Then in `_build_features()`, the same `dc_params` is used for every historical match's features (line 171). This means when computing features for a 2017 match, the DC model reflects the state as of the current "latest" symlink (2026-09-05 today). This is **9 years of future information** embedded into a supposedly historical feature. The walk-forward "CV" (line 316-346) refits LGBM per fold, but its inputs are already leaked. Reported OOF metrics are optimistic.

**BL1 mitigation:** Refit DC at each fold cutoff with `today = fold_start_date` and `phi` decay. Never load `params_latest`.

### L2 — Broken Elo lookup (bug, not leakage, but material)

File: `scripts/train_lgbm_bundesliga2.py:185-200`

The code searches for `home_team` as a column in `elo_series` (which has `elo_home_pre` / `elo_away_pre` per match, not per-team columns). Every lookup silently misses and returns `ELO_DEFAULT = 1500`. Result: `elo_home`, `elo_away`, `elo_diff` are all constant in the BL2 LGBM feature matrix and contribute nothing.

**BL1 mitigation:** Join `elo_home_pre` / `elo_away_pre` from `compute_elo_series(df_all)` per-row via merge or the same DataFrame after `compute_elo_series` return.

### L3 — Static market_values.py snapshot (SEVERE)

File: `src/data/market_values.py:12` — data is "Transfermarkt, June 2026". No BL1 teams present. Any use of this file for BL1 historical backfill would leak future values. And even for BL2, using a single 2026 snapshot to compute features for a 2020 match is future-leaked.

**BL1 mitigation:** Exclude for v1. Add per-season historical Transfermarkt values (multi-year effort) before including in v2.

### L4 — Static attendance.py snapshot (MODERATE)

File: `src/data/attendance.py:15` — "25/26-Basis für 26/27". BL1 clubs not present. Same leakage class, though attendance is more stable than market value.

**BL1 mitigation:** Exclude for v1.

### L5 — Scanner hardcoded model path (STRUCTURAL, not backfill leakage)

File: `scripts/bundesliga2_scan.py:62` — loads `models/dc_bundesliga2/params_latest.pkl`. This is not a leakage risk for live signals (loading current params for future prediction is correct); it is a **portability blocker** for BL1. Fix: parameterize the load path by league.

## Recommended BL1 v1 feature set (for M3/M4 challenger)

- DC 1X2 probabilities (per-fold refit)
- DC attack/defence per team
- DC lambdas (product)
- Elo pre-match (per-fold cumulative)
- Elo diff
- Rolling form (last 3 and last 6, plus their difference = momentum) — as-of
- Venue form (last 5 at home / last 5 away) — as-of
- Rolling goals for/against (last 5) — as-of
- Rest days — as-of
- H2H home win-rate (last 5) — as-of
- Promoted-team indicator (binary) + optional prior weight — see Task K
- **European load** (M3 only): count of European fixtures (CL + EL + UECL group/KO) in the trailing 14 days for each team — requires external CSV join (feasible from football-data.co.uk cup/European feeds; not yet built)

Explicitly excluded from v1: market_value_*, attendance_*, injuries, xG.

## Open items (require data availability audit — depends on Task A completing)

- Actual PSH/PSD/PSA opening-odds coverage per season
- Actual PSCH/PSCD/PSCA closing-odds coverage per season
- Season length variation (COVID 19/20)
- Team renames / canonical_name coverage across all 10 seasons
- Duplicate fixtures / rescheduled fixtures (COVID lockdowns)
