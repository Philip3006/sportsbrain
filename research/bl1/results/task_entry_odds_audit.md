# FLAGSHIP-BL1 — Correction 2: Entry-odds source audit

Auditor: CLAUDE · 2026-09-06 · Isolated worktree, no production changes.

## Objective

Prove which column(s) the Phase-A edge sweep used as *entry_odds*. Confirm those are not the closing prices. Produce a per-season timing table.

## Football-Data.co.uk column semantics (verified against raw D1_2324.csv)

Football-data.co.uk publishes odds per bookmaker + summary aggregates, for each match, at TWO snapshot times:

| Snapshot | Bookmaker column example | Column-name pattern | Meaning |
|---|---|---|---|
| Pre-closing (a few days before kickoff) | `PSH`, `PSD`, `PSA` | `<bookie><H/D/A>` | Pinnacle price captured at football-data.co.uk's periodic pre-kickoff scrape |
| Pre-closing avg / max | `AvgH`, `MaxH`, etc. | `Avg<H/D/A>`, `Max<H/D/A>` | Average / max across all sampled bookies at pre-closing snapshot |
| **Closing (kickoff)** | **`PSCH`, `PSCD`, `PSCA`** | **`<bookie>C<H/D/A>`** | **Pinnacle closing price at kickoff** — this is BENCHMARK ONLY |
| Closing avg / max | `AvgCH`, `MaxCH`, etc. | `<bookie>C<H/D/A>` | Average / max across bookies at kickoff |

The `C` letter between the bookmaker code and the outcome letter denotes "Closing".

Verified sample from 2324 first fixture (Werder Bremen vs Bayern, 2023-08-18):

| Field | Value | Interpretation |
|---|---|---|
| PSH | 8.59 | Pinnacle pre-closing home |
| PSD | 6.36 | Pinnacle pre-closing draw |
| PSA | 1.33 | Pinnacle pre-closing away |
| **PSCH** | **8.80** | **Pinnacle CLOSING home** |
| **PSCD** | **6.30** | **Pinnacle CLOSING draw** |
| **PSCA** | **1.31** | **Pinnacle CLOSING away** |

PSCA moved from 1.33 pre-close to 1.31 close — the market steamed on Bayern. That is the type of movement the CLV metric is designed to measure.

## Mapping used in `src/data/football_data.py::fetch_season`

Verified against source (line 82-88):

```python
rename = {
    ...
    "PSH": "ps_open_home", "PSD": "ps_open_draw", "PSA": "ps_open_away",
    "PSCH": "ps_close_home", "PSCD": "ps_close_draw", "PSCA": "ps_close_away",
}
```

The loader renames `PSH/PSD/PSA` → `ps_open_*` and `PSCH/PSCD/PSCA` → `ps_close_*`. The `_open` prefix is a slight misnomer inherited from earlier code — Football-Data's `PS*` columns are pre-closing snapshots, not the earliest opening line. Everywhere in Phase-A the two column-families are used consistently: `ps_open_*` = pre-closing entry candidate; `ps_close_*` = closing benchmark.

## Column used as entry_odds in the Phase-A edge sweep

File: `research/bl1/scripts/30_edge_sweep.py:79-80`

```python
open_odds = [r.get("ps_open_away"), r.get("ps_open_draw"), r.get("ps_open_home")]
close_odds = [r.get("ps_close_away"), r.get("ps_close_draw"), r.get("ps_close_home")]
```

- **entry price used:** `ps_open_*` = PSH/PSD/PSA = **Pinnacle pre-closing**
- **closing benchmark used:** `ps_close_*` = PSCH/PSCD/PSCA = Pinnacle kickoff

## Verdict

The Phase-A edge sweep is **NOT INVALID** with respect to CEO Correction 2. It used a pre-closing Pinnacle snapshot (PSH/PSD/PSA) as entry_odds, never the closing kickoff price (PSCH/PSCD/PSCA). The closing columns were used exclusively for the primary CLV metric.

## Nuance and residual timing risk (P1)

Football-Data.co.uk does not publish the exact timestamp of the `PS*` snapshot. Per project documentation (Joseph Buchdahl's `football-data.co.uk` note): pre-closing snapshots are typically captured a few days before kickoff. For SportsBrain production, the intended signal timing is **~90 minutes before kickoff** — closer to the closing snapshot than to Football-Data's pre-closing snapshot.

**Implication:** The `PSH → PSCH` price movement my sweep measures via `closing_price_edge` is likely *larger in magnitude* than the movement between a ~90-min-pre-KO price and PSCH. In production, we would see less line movement (both good moves and bad moves compress). The Phase-A `closing_price_edge = −0.027` on firing signals is therefore an **upper bound** on the adverse-selection cost. Real production cost may be smaller, but the sign is unlikely to reverse without a much better model.

**Not a P0.** The direction of the finding stands: DC-only produces signals that are worse than the closing market. Regardless of the exact pre-closing timestamp, that is a market-competitiveness problem, not a data-plumbing artefact.

## Per-season entry / closing coverage (from Task A + this audit)

| Season | Rows | entry (PSH/PSD/PSA) coverage | closing (PSCH/PSCD/PSCA) coverage | Source |
|---|---|---|---|---|
| 1617 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20170601 capture) |
| 1718 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20180601 capture) |
| 1819 | 306 | 99.7% | 99.7% | football-data.co.uk (via archive.org 20190601 capture) — single row of PSC missing |
| 1920 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20200601 capture) |
| 2021 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20210601 capture) |
| 2122 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20220601 capture) |
| 2223 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20230601 capture) |
| 2324 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20240601 capture) |
| 2425 | 306 | 100.0% | 100.0% | football-data.co.uk (via archive.org 20250601 capture) |
| **2526** | **306** | **49.0%** | **48.7%** | **football-data.co.uk (via archive.org 20260601 capture)** — Pinnacle feed reliability issue documented by football-data.co.uk since July 2025 |

The 2526 gap affects BOTH pre-closing and closing Pinnacle columns proportionally. Verified: rows missing PSCH also miss PSH. This is a Pinnacle-feed dropout, not a closing-only artefact.

## Actions

1. **Prior edge sweep stands.** No rerun triggered by this audit.
2. **Rerun in continuation still required** for CEO's expanded matrix (M1/M2/M3/M4 × unrestricted × one-per-match × class analysis), but for a different reason — expanded scope, not entry-odds correction.
3. **Rename symbolic:** consider renaming `ps_open_*` to `ps_pre_close_*` in a future refactor. Non-blocking.
4. **Market benchmark hierarchy:** the 2526 Pinnacle gap motivates the CEO's mandate to design a broader-market primary benchmark (average / max across bookies) with Pinnacle as diagnostic-only secondary. Executed in `research/bl1/results/task_market_hierarchy.md`.
