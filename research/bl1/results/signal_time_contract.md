# SportsBrain Signal-Time Contract (BL1) — v5-correction revalidation

## Repository-verified BL1 production configuration (revalidation)

Per CEO §6 the intended SportsBrain BL1 production prediction/scan horizon has been re-checked against the current `worktree-flagship-bl1-research` codebase:

| Question | Repository evidence | Verdict |
|---|---|---|
| Is BL1 registered in `src/config.py::LEAGUE_REGISTRY`? | **No.** `soccer_germany_bundesliga` is absent. `soccer_germany_bundesliga2` (BL2) is present. | BL1 is not yet a production league. |
| Is there a BL1 launchd plist? | **No.** `launchd/*bundesliga*` returns no matches. | No production scheduler for BL1. |
| Is there a BL1 scan script? | **No.** `scripts/*bundesliga1*` and `scripts/*bl1*` return no matches. | No production scan pipeline for BL1. |
| Is there a BL1-specific signal-time constant? | **No.** No `SIGNAL_MINUTES`, `SCAN_CUTOFF`, or `T_MINUS` constant for BL1 in `src/config.py`. | No explicit horizon. |
| Where is the actual refresh cadence defined? | `src/signals/odds_refresher.py::_refresh_interval_minutes()` implements a **rolling schedule** keyed on minutes-to-kickoff: 5 min interval within 60 min to KO, 10 min within 180 min, 15 min within 720 min, 20 min within 1440 min, 30 min beyond. | Refresh cadence, not a fixed signal-time. |
| What is the production odds source? | For BL2 (the closest analogue): TheOddsAPI (multi-region) + Betfair Exchange + Pinnacle + WebSearch fallback (`scripts/bundesliga2_scan.py`). **Not** football-data.co.uk. | Production price feed differs from research archive. |

**Exact intended BL1 horizon: NOT DEFINED in the current repository.** The v5 handoff's "T−90 minutes" claim was aspirational, inherited from generic scanner cadence intuition, not from any BL1-specific configuration.

## Historical pre-closing market snapshot proxy verdict

**PARTIAL.**

Reasons the FDU pre-closing snapshot is NOT a true SportsBrain BL1 signal-time proxy:

1. **Different source.** Football-Data.co.uk `PSH/PSD/PSA` are a scraped archive; SportsBrain production uses TheOddsAPI + Betfair + Pinnacle live-refreshed prices. Odds levels differ per book and per capture timestamp.
2. **Unpublished timestamp.** Football-Data does not publish when the pre-closing snapshot was captured. Estimated "a few days before kickoff" from third-party (Buchdahl) documentation, but not verified.
3. **No BL1 production scanner.** There is nothing to compare the archive against.

Reasons it is SOMEWHAT informative (why PARTIAL rather than NO):

1. **All pre-closing snapshots are strictly before kickoff.** Whatever the exact timing, they are causally in the correct direction — earlier than kickoff and later than "opening".
2. **Bookmaker-average is a market-wide aggregate.** The v5-correction operational M5 uses `AvgH/AvgD/AvgA` (bookmaker-average pre-closing across all sampled books), which is less sensitive to a single provider's snapshot timing than picking Pinnacle alone.
3. **The pre-close vs close Brier gap is small.** The matched paired comparison shows closing beats bookmaker-avg pre-close by only 0.0025 Brier. This bounds the residual gain any T−90-vs-PSH timing refinement could deliver: at most on the order of that 0.0025 Brier.

## Implication for downstream conclusions

- M5 canonical (bookmaker-average pre-closing) is a **PARTIAL proxy** for a real SportsBrain BL1 signal-time price. It is closer than "opening" but is not the production price feed.
- The v4 conclusion "no football model beats M5 at 100% paired win rate" remains valid as a research statement on FDU archives; it does not directly translate into "no football model beats SportsBrain production BL1 signal-time prices" because production prices don't yet exist.
- The v5 conclusion "closing beats bookmaker-avg pre-close by 0.0025 Brier" is a real gap that any future SportsBrain BL1 signal-time entry would face against closing — bounding the theoretical CLV opportunity above zero for real production only if a T−90 entry could be captured that is meaningfully closer to closing than the FDU pre-closing snapshot.

## What the contract does NOT establish

- **Actual FDU pre-closing timestamp.** Unpublished by source.
- **What TheOddsAPI + Betfair + Pinnacle live-refreshed prices would show at BL1 kickoff-minus-N minutes.** Not testable from the current dataset.
- **Whether SportsBrain BL1 will ever have a stable T−90 entry.** Depends on production configuration, not research.

## Terminology fix applied in v5

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
