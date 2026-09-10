# EPL Data Audit (Top-5 Phase 1)

## Source and acquisition

- **Source:** football-data.co.uk `E0.csv` per season
- **URL template:** `https://www.football-data.co.uk/mmz4281/{season}/E0.csv`
- **Retrieval date:** 2026-09-10
- **Method:** direct HTTP + archive.org fallback (from top5.core.downloader)

## Seasons acquired

10/10 seasons, 380 matches per season, 3,800 total matches.

| Season | Rows | Bytes | SHA-256 (first 12) | Status |
|---|---|---|---|---|
| 1617 | 380 | 99,990 | 9625a7652b5f | OK |
| 1718 | 380 | 100,734 | 4f3389365ef3 | OK |
| 1819 | 380 | 96,144 | 7c096b3c2ecd | OK |
| 1920 | 380 | 175,475 | 100037618b94 | OK |
| 2021 | 380 | 175,815 | 5afe63f69401 | OK |
| 2122 | 380 | 175,363 | 335afcbabeb2 | OK |
| 2223 | 380 | 176,180 | 8442792d3b61 | OK |
| 2324 | 380 | 172,196 | b2e057b0ed95 | OK |
| 2425 | 380 | 197,110 | d0c8ce4a96d8 | OK |
| 2526 | 380 | 203,438 | 3e3a8352f9ad | OK |

## Coverage by source (2526 holdout, DIAGNOSTICS ONLY — no price values exposed)

| Source | Coverage | n / 380 |
|---|---|---|
| Bookmaker-avg pre-close (AvgH/D/A) | 100.0% | 380 |
| Bookmaker-avg close (AvgCH/D/A) | 100.0% | 380 |
| Bookmaker-max pre-close | 100.0% | 380 |
| Bookmaker-max close | 100.0% | 380 |
| Bet365 pre-close | 100.0% | 380 |
| Bet365 close | 100.0% | 380 |
| Pinnacle pre-close (PSH/D/A) | 0.0% | 0 — columns not present in 2526 CSV |
| Pinnacle close (PSCH/D/A) | 0.0% | 0 — columns not present in 2526 CSV |

**Important:** the Pinnacle columns are entirely ABSENT from the 2526 football-data.co.uk EPL CSV as of 2026-09-10. This is consistent with the post-2025-07-23 Pinnacle reliability warning already documented for BL1, but the current state is stricter: Pinnacle is missing, not just partially covered. Same observation applies to BL1 2526 in the freshly-downloaded dataset today. Bookmaker-average and Bet365 remain the only 100%-covered sources on 2526 for either league.

## DEV coverage (1617..2324)

Cumulative canonical (Bookmaker-avg pre-close) coverage on DEV outer folds
(2021..2324) is 100% — 1,520 matches usable for M5 evaluation.

## Anomalies detected

- Pinnacle columns absent from 2526 EPL and BL1 CSVs (see above). Bookmaker-avg is the correct canonical research baseline regardless (per the anti-cherry-picking rule already applied to BL1 v7).
- No duplicate matches within a season.
- No malformed rows (all Date fields parseable, all team names canonicalized via `src.config.canonical_name`).

## Post-processing outputs

- `research/top5/leagues/epl/dataset/epl_raw.pkl` — minimal cols, 3,800 rows
- `research/top5/leagues/epl/dataset/epl_raw_full.pkl` — full market + stats cols, 3,800 rows

## Compatibility with BL1 methodology

Direct compatibility: same schema, same season codes, same partition
boundaries (DEV=1617..2324, CALIB=2425, HOLDOUT=2526, LIVE_SHADOW=2627).
No EPL-specific data transformations required.
