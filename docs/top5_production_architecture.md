# Top-5 Football Production Architecture Preparation

Status: prepared, disabled by default. This document does not approve a model,
league registration, signal time, scheduler, provider call, publication, or bet.

## Current Architecture

| Concern | Current source of truth | Top-5 disposition |
|---|---|---|
| League metadata | `src/config.py` `LEAGUE_REGISTRY` | Reusable shape; keep Top-5 entries unregistered until their adapters and approval exist. |
| Fixture and bulk odds | `src/data/odds_api.py`, BL2 scanner | Reusable provider pattern; retain bulk bookmaker reuse to avoid a per-fixture Tier-2 request. |
| Football odds | `src/football/odds/` | Reusable. The merger preserves source tier and enforces the three-bookie coverage gate. |
| BL2 model/feature flow | `scripts/bundesliga2_scan.py` | League-specific: data paths, model files, team universe, feature construction, thresholds, and ESPN fallback. Do not reuse directly. |
| WM daily scanner | `src/scanner/daily_scan.py`, `scripts/daily_scan.py` | WM-specific legacy. It has a tournament date guard and direct legacy scan orchestration. |
| Closing odds | `scripts/bundesliga2_closing_odds.py` | Reusable separation principle; current implementation is BL2-specific. |
| Settlement | `scripts/bundesliga2_settle.py`, `scripts/settle_bets.py` | Reusable result routing concept; preserve the existing ledger mutation semantics. |
| Financial placement | `src/betting/ledger.py`, Worker pending-bet consumer | Authoritative and unsafe to bypass. No scaffold calls it. |
| Publishing | `src/notifications/web_dashboard.py` plus isolated publisher | Reusable only through existing serializer and staged publisher boundaries. |
| Health and runtime state | `src/monitoring/`, `src/runtime/paths.py` | Reusable. Runtime state must stay external; staged public artifacts are published in the isolated checkout. |
| Retraining | `scripts/bundesliga2_retrain.yml`, `scripts/auto_retrain.py` | BL2 and WM-specific. A Top-5 adapter needs separate research approval and model lifecycle evidence. |

## Signal-Time Options

No Top-5 signal time is selected by this preparation work. Existing evidence
only supports these operational options:

| Candidate | Existing comparable evidence | Benefits | Risks and requirements |
|---|---|---|---|
| Fixed scheduled pre-match scan | BL2 scan runs at daily and selected pre-kickoff cron points. | Predictable operations and bulk fixture/odds reuse. | Does not guarantee a uniform lead time across league fixture windows; requires a league-specific kickoff-to-job mapping and retry policy. |
| Event-relative pre-match window | The disabled legacy prematch workflow evaluates fixture windows; odds refresh already changes cadence by minutes to kickoff. | Can define a real lead-time contract and freshness cap per fixture. | Needs a durable event scheduler or explicit dispatch contract, idempotency, and quota forecast before activation. |
| Earlier daily shadow snapshot | Existing daily/BL2 morning scans and three-hour tennis snapshots demonstrate low-frequency collection. | Low operational complexity and a conservative research/shadow starting point. | Greater stale-data risk; unsuitable for live placement without explicit freshness and revalidation. |
| Near-kickoff refresh plus prior signal | Odds refresher supports five-to-thirty-minute cadence based on time to kickoff. | Fresh price verification is already a separate concern. | It must remain a revalidation path, not a way to replace the approved signal-time contract or mix closing odds into prediction. |

Recommendation for CEO approval: use an event-relative signal window with an
explicit minimum and maximum lead time, a maximum odds age, a separate closing
capture window, idempotent dispatch, and one bulk market fetch per sport key.
The exact numbers must be selected only after research and a real schedule/
quota study. The new `SignalTimeContract` intentionally has no default.

## Generic Interface

`src/football/production_contracts.py` introduces only contracts:

- `LeagueProductionConfig` keeps league/provider/model metadata isolated.
- `SignalTimeContract` rejects activation without an explicit approved window.
- `FixtureIngestor`, `FeatureAdapter`, `ModelAdapter`, and `SignalDecider` are
  pluggable protocols, preventing five copies of a scanner.
- `MarketSnapshotKind` separates signal-time and closing snapshots.
- `PredictionInput.create()` rejects closing snapshots structurally.
- `ShadowSignalArtifact` requires full provenance and `no_bet_flag=True`.
- `validate_artifact_ownership()` rejects source and ledger paths.

There is no Top-5 registry, model adapter, provider invocation, scheduler,
publisher write, Cloudflare call, or ledger call in this scaffold.

## Provider Efficiency Findings

1. BL2 passes The Odds API bulk `bookmakers` data into the football merger,
   avoiding an extra per-fixture Tier-2 call. Future adapters should preserve
   that handoff.
2. Football discovery caches `/sports` responses for one hour. It is currently
   not wired into the WM daily scanner, whose generic path remains legacy.
3. `signals/odds_refresher.py` uses a tennis quote cache within a refresh run;
   football refresh currently builds an individual match hint with a hardcoded
   BL2 sport key. A generic Top-5 refresh adapter is deferred, not implemented.
4. The current signal refresher runs at event-relative intervals. Any new
   Top-5 job must forecast the combined scan plus refresh cost before a schedule
   is approved; no cadence change is proposed here.

## WM Legacy Classification

| Path | Classification | Reason |
|---|---|---|
| `src/scanner/daily_scan.py` and `scripts/daily_scan.py` | Potential production risk | The primary path remains WM date-guarded and bypasses the football odds merger. |
| `scripts/auto_retrain.py` | Unnecessary runtime work | Its decision and stacker trigger are WM-2026-specific. It is not a generic club-league retraining path. |
| `scripts/build_wm_forecast.py`, `scripts/build_post_wm_snapshot.py`, `scripts/wm2026_readiness_check.py` | Safe inactive legacy / historical compatibility | Explicit tournament artifacts; no evidence in active Top-5 runtime paths. |
| `src/phase_flags.py` `WC2026_BOOST` | Required historical compatibility | Neutralized and registered with a sunset; retain until a separately approved cleanup. |
| `src/notifications/web_dashboard.py` WM result/stat payload | Safe inactive legacy | Public product compatibility, but should not be copied into a club-league adapter. |

## Future Activation Sequence

Research-approved model -> production adapter -> shadow inference -> no-bet
and signal-time validation -> provider/source validation -> shadow performance
-> CEO gate -> league-by-league controlled activation.

Settlement, authoritative placement, P0-A acknowledgement/durability, and
public/private serialization remain existing independent boundaries.
