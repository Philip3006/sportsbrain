# Top-5 Football Production Architecture Preparation

Status: prepared, disabled by default. This work does not approve a model,
signal horizon, scheduler, provider call, publication, bet, or ledger write.
The five leagues remain absent from `src.config.LEAGUE_REGISTRY` and are
represented only by immutable disabled metadata in
`src.football.production_contracts`.

## 1. Current production architecture map

| Concern | Current implementation | Classification and Top-5 disposition |
|---|---|---|
| Match discovery | `src/data/football_discovery.py` caches `/sports` for one hour and applies `FOOTBALL_LEAGUES_WHITELIST`; `src/data/odds_api.py` provides The Odds API bulk upcoming matches. | Discovery pattern is reusable; the Top-5 ingestor remains preparation-only. |
| Competition/sport configuration | `src/config.py` contains the whitelist and live `LEAGUE_REGISTRY` entries for WM 2026 and BL2; tennis has separate tournament registries. | Registry shape is reusable; WM/BL2 entries and date windows are existing production behavior; Top-5 live registration is intentionally missing. |
| Odds acquisition | `src/data/odds_api.py` and `src/football/odds/the_odds_api.py` use The Odds API bulk/event payloads. | The Odds API is the only active football odds provider; invalid or unavailable data fails closed. |
| Scores/results acquisition | `src/data/results_router.py`, `src/data/odds_api.py`, `scripts/settle_bets.py`, and BL2/tennis live-score jobs. | Result routing is reusable; WM score endpoint and BL2/tennis jobs are domain-specific. |
| Provider fallback | The football merger and refresher expose The Odds API only; `src/signals/provider_budget.py` circuit-breaks the provider and keeps quota state externally. | There is no provider substitution; the route fails closed before Top-5 activation. |
| Cache/state | `src/data/cache.py` disk cache, provider budget state, odds refresher sidecar, `src/runtime/paths.py` external runtime-state resolver. | Cache and ownership principles are reusable; some cache keys are not parameterized by sport/market and require review before reuse. |
| Prematch scans | `scripts/prematch_scan_cron.sh` gates a schedule window and calls `scripts/daily_scan.py`; active BL2 scans run from `.github/workflows/bundesliga2_scan.yml`. | Scheduler wrapper/health pattern is reusable; the daily scan is WM-shaped and the BL2 job is league-specific; generic Top-5 orchestration was missing and is now injected-only. |
| Daily scan | `src/scanner/daily_scan.py` plus `scripts/daily_scan.py`; it loads WM historical context, model data, and dashboard payloads. | WM-specific legacy path; unsafe as a Top-5 scanner without an adapter boundary. |
| Closing odds | `scripts/update_closing_odds.py`, `scripts/bundesliga2_closing_odds.py`, and tennis closing-odds workflows. | The signal/closing separation principle is reusable; capture windows and storage remain league-specific. |
| Retraining | `scripts/auto_retrain.py` is WM-specific; BL2 retraining is split across `build_bundesliga2_universe.py`, `train_*_bundesliga2.py`, and `bundesliga2_retrain.yml`; tennis has independent workflows. | Lifecycle/health concepts are reusable; model code and training data are BL2/WM/tennis-specific; Top-5 retraining is missing. |
| Model loading | BL2 model bundle is loaded in `scripts/bundesliga2_scan.py`; WM models are loaded by `src/scanner/daily_scan.py`; tennis uses `src/models/tennis_*`. | All current loaders are domain-specific. The new `ModelAdapter` protocol leaves the Top-5 model slot unbound. |
| Feature generation | `src/features/*`, `src/scanner/scoring.py`, and inline BL2 feature construction. | Shared numeric helpers may be reusable; feature sets and team-history sources must be league adapters. The new `FeatureAdapter` protocol is intentionally empty of production logic. |
| Inference | `src/scanner/scoring.py` and BL2 `_scan_match()` call the domain models. | Inference is reusable only through an adapter; no Top-5 live inference path exists. |
| Signal selection | `src/betting/value_detector.py`, `src/betting/gates.py`, portfolio caps, and BL2 market gates. | Risk gates are authoritative and must remain downstream of shadow work; Top-5 signal policy is missing. |
| Publishing | `src/notifications/web_dashboard.py` builds payloads; `scripts/publish_runtime_artifacts.sh` copies allowlisted staged files into the isolated publisher checkout. | Publisher boundary is reusable but unsafe for a new scaffold; the Top-5 pipeline has no publisher dependency. |
| Worker/public consumption | `cloudflare/worker.js` validates and serves canonical public health/signals artifacts from KV; `cloudflare/contract.js` defines the public contract. | Public contract is reusable; no Top-5 artifact is added to it. |
| Settlement | `scripts/settle_bets.py`, `scripts/bundesliga2_settle.py`, and tennis settlement route scores to the appropriate market settler. | Settlement boundary is reusable; financial writers remain out of scope for Top-5 preparation. |
| Private ledger | `src/config.py` resolves `SPORTSBRAIN_LEDGER_DIR`; `src/betting/ledger.py` and CI workflows write the private repository. | Authoritative and unsafe to bypass; no new path or permission is added. |
| Scheduler/launchd | Launchd wrappers under `launchd/`, `.github/workflows/*`, and Cloudflare cron dispatch in `cloudflare/wrangler.toml`. | Existing cadences remain unchanged; no Top-5 job or launchd entry is registered. |
| Health | `src/monitoring/health_writer.py`, `job_schedule.py`, aggregate health, and Worker validation. | Health schema is reusable; `ShadowRunHealth` is an in-memory integration hook and does not write a health snapshot. |
| Runtime state | `src/runtime/paths.py` keeps mutable state outside the checkout and prevents checkout-root configuration. | Reusable ownership rule; Top-5 code does not create a new state directory. |
| Staged artifacts | Runtime publisher allowlist and per-run stage directory in the cron wrappers. | Reusable isolation rule; `validate_artifact_ownership()` requires an explicit Top-5 owner namespace: `results/shadow/top5/`, `docs/data/top5/shadow/`, or `results/health/top5/`, and rejects ledger/source/model/research/secrets paths. |

## 2. Signal-Time architecture

No production horizon is selected here. The current evidence supports these
options for later CEO review:

| Candidate | Mechanics | Availability/freshness | Cost and risk |
|---|---|---|---|
| Fixed kickoff offset | Cron or a dispatcher fires at one nominal offset per league. | Simple to reason about, but scheduler jitter and fixture changes make the actual lead time non-uniform. | Low implementation cost; weak signal-time guarantee and duplicate scans around dense fixtures. |
| Event-relative bounded window | Select fixtures inside `[minimum_lead, maximum_lead]`, make one bulk request per sport/market set, retry only inside the window, and publish a no-bet shadow artifact. | Best fit for odds freshness and rescheduled kickoffs; requires authoritative kickoff updates and a max-age check. | Needs idempotency, retry/backoff, quota forecasting, and a distinct closing capture. |
| Earlier daily shadow snapshot | One scheduled snapshot provides model inputs long before kickoff. | Available with existing daily jobs but can become stale as lineups and markets move. | Cheap for research/shadow; not suitable for live placement without revalidation. |
| Near-kickoff revalidation of an existing signal | Refresh odds at the current event-relative cadence and compare with the original signal snapshot. | Existing odds refresher has 5–30 minute cadence and terminal-state guards. | Useful as a separate freshness check; it must not replace the approved signal snapshot or mix closing odds into prediction. |

Recommended architecture for a later decision: an event-relative bounded
window represented by `SignalTimeContract`, with explicit minimum/maximum lead
time, maximum odds age, idempotent dispatch, one coalesced bulk request per
provider/sport/market set, a bounded retry window, and a separate closing
snapshot path. Exact values, provider/source semantics, and quota budget still
require CEO approval after real schedule and provider evidence.

## 3. Generic Top-5 production design

The scaffold in `src/football/production_contracts.py` provides:

- `LeagueProductionConfig` and `ProviderMapping` for isolated competition and
  provider identifiers.
- `DISABLED_TOP5_LEAGUE_CONFIGS` for the five leagues, with `model_adapter_id`
  set to `unbound`, no signal-time default, and `ActivationMode.DISABLED`.
- `SignalTimeContract`, `MarketSnapshotKind`, and `PredictionInput` so a
  closing snapshot cannot be passed to a prediction adapter.
- `OddsRequest` as a stable key for bulk request coalescing and cache reuse.
- `FixtureIngestor`, `OddsProvider`, `FeatureAdapter`, `ModelAdapter`,
  `SignalDecider`, and `ShadowArtifactSink` protocols.
- `PredictionArtifact` and `ShadowSignalArtifact` with provenance and a hard
  `no_bet_flag` requirement.
- Fixture kickoff, market capture, request, artifact-generation, signal-time,
  and pipeline timestamps must be timezone-aware; aware values normalize to
  UTC and naive values fail closed with `ProductionContractError`.
- `RolloutEvidence` for the ordered, league-by-league gate sequence.
  Each requested stage requires that stage and every predecessor; no evidence
  flag is inferred or changed by validation.
- `RuntimeStateBinding` for external ownership of mutable provider, odds,
  bankroll-control, and future shadow state.
- `validate_artifact_ownership()` for explicit `ArtifactOwner` namespaces:
  `results/shadow/top5/`, `docs/data/top5/shadow/`, and
  `results/health/top5/`; source, publisher, financial, runtime-cache, model,
  research, secret, and traversal paths are rejected.

`src/football/production_pipeline.py` adds one injected shadow orchestration
function. It ingests fixtures, builds one bulk request, accepts only validated
signal-time snapshots, calls the injected feature/model/decision adapters, and
returns in-memory predictions, signals, and health. It contains no provider
client, model loader, scheduler, publisher, Cloudflare, or ledger import.

Future rollout sequence, one league at a time:

```
research-approved model
  -> production adapter
  -> offline compatibility test
  -> no-bet shadow inference
  -> signal-time validation
  -> provider/source validation
  -> shadow performance observation
  -> CEO gate
  -> controlled activation
```

Closing odds remain a benchmark/CLV artifact only and are not admissible to
`PredictionInput`, `PredictionArtifact`, or `ShadowSignalArtifact`.

## 4. Provider-efficiency audit

### Safe now

1. Preserve the BL2 bulk bookmaker handoff into
   `src/football/odds/the_odds_api.py`; it avoids a second per-fixture API
   request when the bulk response already contains bookmakers.
2. Preserve the one-hour `/sports` discovery cache and external provider
   circuit-breaker state.
3. Use the new `OddsRequest.request_key` as the future boundary for request
   coalescing; no live caller is wired to it yet.
4. Keep freshness and correctness checks ahead of any quota optimization.

### Needs review

1. `src/signals/odds_refresher.py::_refresh_football()` currently hardcodes
   `soccer_germany_bundesliga2`; a generic Top-5 refresh adapter must receive
   its sport key from the league contract.
2. `disk_cache("odds_api_upcoming_wide")` stores multiple function variants
   behind one cache filename. Sport/market/region-aware keys are required
   before the cache can be shared safely across leagues.
3. The The Odds API 422 handling may issue a same-provider event request; a
   quota study must keep that path bounded and fail closed when the budget is
   exhausted.
4. The Top-5 provider mapping is singular by architecture; no source
   authority or provider substitution may be inferred from a missing quote.

### Do not change in this task

- Scheduler cadence, free-plan/billing configuration, or launchd activation.
- Provider regions/markets solely to fit current quota.
- Any provider substitution or stale-cache promotion without production evidence.
- Any financial writer, public publisher, Cloudflare Worker, or ledger path.

No live provider request was made by this architecture task; meaningful Odds
API credits consumed: 0.

## 5. WM 2026 legacy audit

| Area | Classification | Finding |
|---|---|---|
| Daily scan | Potential production risk for reuse | `src/scanner/daily_scan.py` and `scripts/daily_scan.py` retain WM historical context, tournament filters, and dashboard assumptions. Keep active behavior unchanged; do not copy into Top-5. |
| Injuries/suspensions | WM-specific inactive legacy | `refresh_injuries.py` and the disabled suspension workflow target WM teams and external runtime state. A Top-5 availability source is missing. |
| Squads | WM-specific / reusable concept only | `src/data/squad_availability.py`, `squad_merger.py`, and the publisher's staged squad artifact provide a boundary pattern, but the team universe is not a Top-5 contract. |
| Retraining | Unnecessary WM runtime work for Top-5 | `auto_retrain.py` counts WM matches and can train the WM stacker; BL2 retraining is separately scheduled. No Top-5 model lifecycle is activated. |
| Settlement | Shared boundary, WM defaults remain | `settle_bets.py` still has a WM score endpoint and must not be treated as a generic Top-5 settlement implementation. |
| Publisher | Shared boundary, WM payload compatibility | `web_dashboard.py` and public serializers retain WM stats/fields. Top-5 artifacts must use the isolated staged publisher only after separate approval. |
| Models/phase flags | Historical compatibility | `WC2026_BOOST` and WM-specific data paths remain for historical compatibility. No broad deletion or cleanup was performed. |

Safe conclusion: WM code remains isolated by this work through non-use. No WM
path, scheduler, provider behavior, or artifact was changed.

## 6. Governance boundaries

- 2425 calibration and 2526 holdout outcomes were not opened or evaluated.
- No Top-5 model was loaded, no live signal was generated, and no bet was
  created or settled.
- The formal 72-hour soak remains deferred for the existing quota reason.
- This branch does not change `main`, launchd, Cloudflare, the private ledger,
  or the active publisher checkout.
