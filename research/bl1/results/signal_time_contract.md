# SportsBrain Signal-Time Contract (BL1) — v6

## Repository-verified BL1 production configuration

The intended SportsBrain BL1 production prediction/scan horizon has been re-checked against the current `worktree-flagship-bl1-research` codebase:

| Question | Repository evidence | Verdict |
|---|---|---|
| Is BL1 registered in `src/config.py::LEAGUE_REGISTRY`? | **No.** `soccer_germany_bundesliga` is absent. `soccer_germany_bundesliga2` (BL2) is present. | BL1 is not yet a production league. |
| Is there a BL1 launchd plist? | **No.** `launchd/*bundesliga*` returns no matches. | No production scheduler for BL1. |
| Is there a BL1 scan script? | **No.** `scripts/*bundesliga1*` and `scripts/*bl1*` return no matches. | No production scan pipeline for BL1. |
| Is there a BL1-specific signal-time constant? | **No.** No `SIGNAL_MINUTES`, `SCAN_CUTOFF`, or `T_MINUS` constant for BL1 in `src/config.py`. | No explicit horizon. |
| Where is the actual refresh cadence defined? | `src/signals/odds_refresher.py::_refresh_interval_minutes()` implements a **rolling schedule** keyed on minutes-to-kickoff: 5 min interval within 60 min to KO, 10 min within 180 min, 15 min within 720 min, 20 min within 1440 min, 30 min beyond. | Refresh cadence, not a fixed signal-time. |
| What is the production odds source? | For BL2 (the closest analogue): TheOddsAPI (multi-region) + Betfair Exchange + Pinnacle + WebSearch fallback (`scripts/bundesliga2_scan.py`). **Not** football-data.co.uk. | Production price feed differs from research archive. |

**Exact intended BL1 horizon: NOT DEFINED in the current repository.**

## HYPOTHETICAL / PREVIOUS ASPIRATION — NOT CURRENT BL1 CONTRACT

Earlier handoffs mentioned a "T−90 minutes" target. That figure appears nowhere in current BL1 configuration and is not a current contract. Any reference to T−90 in this or older documents is a **hypothetical / previous aspiration**, retained only for historical continuity. It does not imply a configured BL1 production schedule exists.

## Historical pre-closing market snapshot proxy verdict

**PARTIAL.**

The football-data.co.uk pre-closing snapshot is a broad pre-kickoff market-information proxy — nothing more. It is:

1. **Different source** from any prospective SportsBrain BL1 production feed (FDU archive vs TheOddsAPI/Betfair/Pinnacle).
2. **Unpublished timestamp** — Football-Data does not document when the pre-closing capture occurs. "A few days before kickoff" is a third-party estimate.
3. **No production BL1 scanner exists** to compare against.

The verdict is not "NO" only because:
- The snapshot is causally strictly before kickoff.
- Bookmaker-average is a market-wide aggregate, not a single-feed idiosyncratic snapshot.

## What the observed evidence supports (v6 canonical interpretation)

The matched-sample paired test (n=1224, DEV outer folds) yields:

- Canonical pre-closing Brier: 0.5822254854702447
- Bookmaker-average closing Brier: 0.5797615693192255
- Observed gap: 0.002463916151019152 (CI excludes zero)

**Canonical interpretation:**

1. The historical pre-closing market **beats current football models** on this DEV sample.
2. The closing market is **modestly but statistically better** than the historical pre-closing snapshot on the same DEV matches.
3. Performance of any actual future SportsBrain BL1 signal-time market snapshot is **UNKNOWN** until representative prices are captured at that time. It could be closer to pre-close, closer to close, or anywhere else.
4. Interpolation between the two snapshots is a hypothesis, not a proven bound. There is no monotonic Brier trajectory established across intermediate T-N prices.

**What v6 explicitly does NOT claim:**

- M5 Brier is NOT a "lower bound" on Brier at any hypothetical intermediate time.
- Closing Brier is NOT a "theoretical upper limit" on any hypothetical entry-time model.
- The 0.0025 gap is NOT an "underestimate" nor an "a fortiori" bound on any future SportsBrain-signal-time-vs-closing comparison.

## Football-Data.co.uk column-name convention (for record)

| Column pattern | Meaning | Timing |
|---|---|---|
| `<bookie><H/D/A>` (e.g. `PSH`, `AvgH`, `MaxH`, `B365H`) | Bookmaker pre-closing price | "A few days before kickoff" per third-party estimate. **Exact timestamp not published.** |
| `<bookie>C<H/D/A>` (e.g. `PSCH`, `AvgCH`, `MaxCH`, `B365CH`) | Bookmaker closing price | Kickoff time |

The un-`C` columns are pre-closing (NOT "opening" — Football-Data does not publish true opening lines).

## What this contract does NOT establish

- **Actual pre-closing timestamp of PSH/AvgH etc.** Not published by source.
- **Any current SportsBrain BL1 signal-time horizon.** None is configured.
- **Whether SportsBrain BL1 will ever have a stable signal-time entry.** Depends on production configuration, not research.
- **Brier of any hypothetical future BL1 signal-time market.** Not testable from FDU data.

## Next-phase research direction (deferred, requires separate CEO scoping)

Any future work on market-timing / alternate entry-price research requires new data acquisition (scraping a live-refreshed odds archive or shadow-recording a production scanner) and separate CEO scoping. Cannot be done from the current football-data.co.uk two-snapshot feed.
