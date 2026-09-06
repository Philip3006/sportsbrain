# SportsBrain Signal-Time Contract (BL1)

Purpose: establish the actual production signal-time semantics and reconcile them with the historical odds snapshots available in the football-data.co.uk feed. This document exists because the v4 handoff used the word "opening" to describe the PSH/PSD/PSA columns — which is documentation-drift; those columns are not true opening lines.

## Football-Data.co.uk column-name convention

| Column pattern | Meaning | Timing |
|---|---|---|
| `<bookie><H/D/A>` (e.g. `PSH`, `AvgH`, `MaxH`, `B365H`) | Bookmaker's price at the **pre-closing** snapshot | Typically captured a few days before kickoff. **Exact timestamp is NOT published** by football-data.co.uk. |
| `<bookie>C<H/D/A>` (e.g. `PSCH`, `AvgCH`, `MaxCH`, `B365CH`) | Bookmaker's **closing** price | Kickoff time |

The `C` letter between the bookmaker code and the outcome letter denotes "Closing". The un-C-ed columns are **pre-closing**, not "opening". Football-Data does not publish true opening lines.

## SportsBrain production signal-time (intended)

The SportsBrain live scanner is designed to fire at **~T−90 minutes** (90 minutes before kickoff). At that timing:
- Some bookmakers have made intraday price movements toward the closing line.
- Most punter action, sharp action, and steam has NOT yet happened (steam typically clusters in the final 30 minutes).
- Live in-play markets have not opened yet.

## The three time-anchors

```
football-data.co.uk PSH     SportsBrain          football-data.co.uk PSCH
  (~days before)             signal-time            (kickoff)
     |                        |                        |
     +------ hours ~ days ----+------- ~90 min --------+
```

## Implication for M5 (pre-closing market baseline)

M5 uses PSH/PSD/PSA (and Avg/Max/B365 equivalents) as the input prices. These prices are further from kickoff than the SportsBrain production signal-time. Consequences:

1. **M5 Brier is a LOWER BOUND** on what a T−90 snapshot would achieve. A price closer to kickoff is more informed; the closing market's Brier (0.5799) is the theoretical upper limit; PSH's Brier (0.5821) is a lower quality signal in expectation.
2. **M5 CLV against closing (PSCH) is an UPPER BOUND** on what SportsBrain's real entry-vs-closing move would show. Real production entry at T−90 is closer to closing than PSH is, so real move-against-us should be smaller in magnitude (both positive and negative moves compress). Absolute values of both `closing_price_edge` and `odds_clv` computed against PSH as entry are inflated.
3. **The 0.0022 Brier gap between M5 and closing** (0.5821 → 0.5799) is an underestimate of the real gap between "SportsBrain signal-time" and closing. SportsBrain's actual gap is somewhere between 0 and 0.0022.

## Implication for the "no defensible edge" conclusion

The v4 finding that no football model produces a defensible edge over the market is **strengthened** by the signal-time contract analysis. If PSH (a stale entry price) already beats every football model at 100% paired win rate, then a T−90 entry price (closer to closing, more informed) would beat football models by an even wider margin. The football-features-cannot-close-the-gap conclusion holds a fortiori.

## What this contract does NOT establish

- **Actual pre-closing timestamp of PSH.** Not published by source. Estimated "a few days before kickoff" from Buchdahl documentation; not empirically verified.
- **Whether a real-time T−90 Pinnacle price would beat closing.** Not testable with the current dataset — we have only two snapshots (pre-close and close), not a T−90 point. Requires alternate data source (live-scraped odds archive, if procurable, would supply T−90 prices).
- **The gap between the SportsBrain scanner's current implementation and the T−90 intent.** Depends on live production timing, which is a production-configuration question outside this research scope.

## Terminology fix applied in v5

All research artefacts previously labelled "opening market" or "M5 opening" have been renamed to "pre-closing" or "M5 preclose". Specifically:
- Script `15_m5_market_baseline.py` — comments and output filenames updated
- Script `16_m6_m7_market_aware.py` — comments updated
- Output files: `oof_m5_dev_v3.csv` (v4) → `oof_m5_preclose_dev.csv` (v5)
- Output files: `m5_market_baseline_summary.csv` (v4) → `m5_preclose_baseline_summary.csv` (v5)
- Handoff document uses "pre-closing" throughout.

## Next-phase research direction (deferred)

The signal-time contract motivates the next research direction: **acquire a signal-time-representative entry-price sequence** (either by scraping T−90 Pinnacle historically or by shadow-recording production scanner picks) and repeat the CLV analysis on that entry. Without such data, the current v5 CLV numbers are the best available estimate but are known-inflated in magnitude.
