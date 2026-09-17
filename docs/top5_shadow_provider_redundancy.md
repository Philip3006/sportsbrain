# Top-5 Shadow Provider Redundancy

## Status and boundary

This workstream is Builder 2's provider/source contract for the five disabled
Top-5 leagues only:

| Code | League | The Odds API sport key |
| --- | --- | --- |
| `BL1` | Bundesliga | `soccer_germany_bundesliga` |
| `EPL` | Premier League | `soccer_epl` |
| `LL` | La Liga | `soccer_spain_la_liga` |
| `SA` | Serie A | `soccer_italy_serie_a` |
| `L1` | Ligue 1 | `soccer_france_ligue_1` |

The implementation in `src/football/top5_shadow_provider_redundancy.py` is
validation and planning only. It accepts injected payloads and never logs in,
makes a provider call, selects an authority, writes runtime state, publishes,
places a bet, mutates the ledger, activates a scheduler, or changes billing.
It does not change Builder 1's branch or duplicate Builder 1's Real Shadow
Execution runner.

The canonical odds source is labelled `CANDIDATE_ONLY` until real observations,
quality evidence, and a separate CEO decision exist. No provider, result
source, signal time, model, rollout league, publication policy, or Controlled
Activation is selected here.

The readiness state is explicit: an injected adapter is `CONTRACT_SUPPORTED`,
but a live path remains `LIVE_PATH_PREREQUISITES_MISSING` until configuration
proves Top-5 competition identity, fixture identity, exact kickoff, source
timestamp, complete 1X2, and provenance. Complete proof advances only to
`LIVE_PATH_READY_FOR_OBSERVATION`; a future controlled and validated real
observation is required for `REAL_OBSERVATION_VALIDATED`.

## Repository-level source inventory

The audit covers the currently implemented football fixture, odds, result, and
closing-benchmark paths. `provider_inventory()` exposes the same matrix as
structured records; the rows below summarize the evidence in the checkout.

### Fixtures

| Source | Implementation | Top-5 coverage | 1X2 | Timestamp / freshness | Auth / limits | Bulk and signal-time status |
| --- | --- | --- | --- | --- | --- | --- |
| The Odds API | `src/data/football_discovery.py`, `src/data/odds_api.py` | All five sport keys through whitelist/config | No | Event `commence_time`; one-hour discovery cache | `ODDS_API_KEY`; credit quota, currently exhausted | `/sports` plus bulk `/odds`; valid candidate, not approved |
| Football-Data CSV | `src/data/football_data.py` | All five, historical seasons | No | Date only, no exact kickoff; 30-day local cache | None; public CSV availability | League-season bulk; historical only |
| ESPN scoreboard | `src/data/football_live.py`, `scripts/bundesliga2_scan.py` | Current generic mapping covers BL1/EPL (plus out-of-scope leagues) | No | Event date and fetch `last_update`; documented result lag | None; public endpoint | One scoreboard per mapped league; result/fixture support only |
| Cache | `src/data/cache.py`, `data/cache/*` | Inherited and unknown | Inherited | Cache mtime is not source freshness unless payload retains source time | Inherited | Zero network on hit; never an independent source |
| Betexplorer | `src/data/betexplorer.py` | World Cup/Euro/Copa only, not Top-5 club leagues | No | Historical browser scrape | Playwright/browser dependency | Tournament page bulk, historical and out of scope |

### Odds

| Source | Implementation | Top-5 coverage | 1X2 / bookmaker identity | Timestamp / freshness | Bulk and signal-time status |
| --- | --- | --- | --- | --- | --- |
| The Odds API | `src/football/odds/the_odds_api.py`, `src/data/odds_api.py` | Configured for all five, subject to active sport and quota | Yes; raw bookmaker key/title, legacy quote is consensus | Bookmaker `last_update` when present; legacy cache may be stale | Sport-key bulk and event fallback; `IMPLEMENTED_CANDIDATE` |
| Football-Data CSV | `src/data/football_data.py` | All five historical seasons | Yes; historical opening/closing columns | Date only and historical file freshness | League-season bulk; historical/closing only |
| Cache | `src/data/cache.py`, `data/cache/odds_api_upcoming_wide.pkl` | Inherited | Inherited | Storage only; cache-read time cannot become source time | Zero-request cache path only, not independent |
| ESPN / Sofascore / FotMob | `src/data/football_live.py`, `src/data/sofascore.py`, `src/data/fotmob.py` | No Top-5 1X2 odds path | No | Not applicable to odds | No odds adapter; unsupported |

### Results

| Source | Implementation | Top-5 coverage | Timestamp / freshness | Bulk / status |
| --- | --- | --- | --- | --- |
| Football-Data CSV | `src/data/football_data.py`, `src/data/results_router.py` | All five configured codes (`D1`, `E0`, `SP1`, `I1`, `F1`) | Historical date only, final `FTHG`/`FTAG` | One league-season CSV; `HISTORICAL_ONLY` |
| ESPN | `src/data/football_live.py`, `scripts/settle_bets.py` | Current generic mapping verified for BL1/EPL; specialized BL2 path exists | Event date, status, score, fetch timestamp; possible lag | Scoreboard bulk by mapped league; result-only candidate |
| The Odds API | `src/data/results_router.py`, `src/data/odds_api.py` | Current `/scores` fallback is mapped for BL1/EPL; not LL/SA/L1 | Scores endpoint event timestamps | Sport-level fallback; credit-consuming candidate |

### Closing benchmark

| Source | Implementation | Coverage | Semantics | Status |
| --- | --- | --- | --- | --- |
| Football-Data CSV | `src/data/football_data.py` | All five historical seasons | `PSCH/PSCD/PSCA` historical closing proxy; no exact capture time | Historical benchmark only |
| Betexplorer | `src/data/betexplorer.py` | Tournament-only, not Top-5 | Historical odds scrape without explicit closing capture | Rejected for this contract |
| Cache | `src/data/cache.py` | Inherited | Cache is storage, not closing semantics | Unsupported as source |

Closing observations remain structurally separate from signal-time odds. The
normalizer rejects a closing observation as prediction input with
`CLOSING_LEAKAGE`; it cannot be promoted into signal-time evidence.

## Candidate adapter architecture

Only the following injected adapters are implemented:

| Adapter | Existing capability | Status | Safe input requirement |
| --- | --- | --- | --- |
| `TheOddsAPIAdapter` | Existing event/bookmaker/market payload | `CONTRACT_SUPPORTED` / candidate-only | Event sport/league, exact teams, timezone-aware kickoff, bookmaker key/title, h2h outcomes, bookmaker `last_update`, request identity; no live-path proof supplied |
| `FootballDataClosingAdapter` | Existing historical PSCH/PSCD/PSCA columns | `HISTORICAL_ONLY` | Explicit precise timestamps are required for normalization, and the resulting role is `CLOSING_BENCHMARK` only |

Supporting score, result, historical, and cache modules are not odds
authorities and cannot enter the Top-5 signal-time route.

An accepted candidate observation is usable for a future NO-BET shadow run; it
is not a provider authority decision. `authority_approved` is hard-coded false
in inventory, quality reports, plans, and v1 evidence bridges.

## Normalized provider contract

`ShadowSourceObservation` is the deterministic schema. It includes:

- Top-5 league code;
- canonical internal fixture key, home team, away team, and timezone-aware kickoff;
- provider and optional bookmaker identity;
- market type (`h2h_1x2` for this workstream);
- home, draw, and away decimal odds;
- capture timestamp and source timestamp;
- computed odds age;
- request identity and structured source provenance;
- completeness state and confidence;
- typed error classification;
- signal-time or closing-benchmark role; and
- request latency evidence.

`make_fixture_key()` uses exact league, normalized team, and UTC kickoff
identity. Team normalization is deterministic and alias-based; it does not use
fuzzy or substring matching. Unknown or ambiguous identity is rejected.

## Source-quality gates

`validate_source_observation()` fails closed for:

1. unknown or mismatched league;
2. fixture-key mismatch;
3. home/away inversion or team alias mismatch;
4. kickoff outside the explicit tolerance;
5. wrong market or missing draw;
6. missing bookmaker/provenance identity;
7. missing, future, negative, or stale source timestamp;
8. malformed, partial, non-finite, or implausible odds;
9. injected provider failure classification;
10. duplicate source snapshots; and
11. closing data entering signal-time input.

`SourceQualityPolicy` requires caller-supplied experiment values for maximum
odds age and kickoff tolerance. Static tests use clearly labelled `TEST`
constants only. There is currently:

```text
NO PRODUCTION FRESHNESS THRESHOLD APPROVED
NO PRODUCTION KICKOFF TOLERANCE APPROVED
```

Any structural observation violation, including invalid confidence, latency,
completeness/error pairing, identity, timestamp, provenance, or market fields,
adds `INVALID_SOURCE_CONTRACT` and forces both `accepted=false` and
`prediction_input_allowed=false`.

`validate_observation_batch()` catches the same source snapshot repeated under
another request identity. A bad observation is rejected and remains visible in
the report; it is never silently repaired, normalized into a different fixture,
or treated as usable odds.

## Quota and request planning

`ProviderPlanningRequest` and `plan_provider_paths()` answer whether each
explicitly allowed path is operationally possible for a league, event window,
required `h2h_1x2` market, freshness requirement, fixture count, credential
state, cache state, and remaining quota.

The result reports, per source:

- candidate source and candidate-only/historical/cache status;
- estimated requests;
- internal provider cost units;
- expected fixture coverage;
- fallback capability;
- signal-time usability; and
- a fail-closed reason when unavailable.

Cost units are an injected planning scale, not a claim about vendor billing.
The default request estimate is one bounded The Odds API request. The planner
returns only the canonical The Odds API path and sets `selected_source=None`;
planning never orders, switches, retries, fans out, buys credits, or upgrades
billing.

### Current The Odds API exhaustion

The current state is explicitly modeled as:

```text
authenticated = true
quota_used = 500
quota_remaining = 0
```

`authorize_odds_api_request()` distinguishes the zero-cost authentication
category from credit-consuming odds/results/closing requests. A paid request at
zero remaining quota returns `allowed=False`, `network_may_execute=False`,
`reason="quota_exhausted"`, `retry_count=0`, and
`fallback_fanout_allowed=False`. `guarded_odds_api_call()` does not invoke its
callable in that state. Exhaustion therefore cannot cause uncontrolled retries
or an expensive fallback fan-out.

The planner exposes `candidate_quota_independent_paths` only after explicit
live-path proof reaches `LIVE_PATH_READY_FOR_OBSERVATION` (or a future
`REAL_OBSERVATION_VALIDATED` state). Credentials or adapter existence alone
never make a path operationally possible. An empty candidate list is expected
today and does not imply that the exhausted Odds API blocker has been bypassed.
Listing is not execution and does not select a provider authority.

## Builder 1 interface

Builder 1 remains the evidence producer and Real Shadow Execution owner. It can
consume a normalized observation or serialize `as_payload()` into its own
boundary. The provider module does not import or run Builder 1's execution
runner.

For later handoff, Builder 1 supplies:

- an `ExpectedFixture` with canonical fixture identity;
- an injected The Odds API provider payload;
- the explicit evidence identity (`evidence_id`, `artifact_id`, artifact SHA,
  source SHA, candidate id, and model identity);
- the evidence window; and
- the NO-BET/shadow execution context.

`to_shadow_observation_evidence()` bridges one accepted or rejected
signal-time observation to the existing `top5-shadow-evidence-v1` contract.
`build_shadow_evidence_bundle()` adds explicit safety assertions:
`no_bet=true`, publication disabled, no real bet, no ledger mutation, no
sealed-data access, no Research mutation, and no production activation.

`to_shadow_provider_evidence()` provides the existing v1 provider evidence
shape with availability, coverage, odds age, latency, and typed failure
mapping. It does not add an authority field or make a winner decision.

## Compatibility and safety invariants

- Frozen Research SHA remains `6eaabbec7d0182103d815c72fae4976e261b40aa`.
- Only the five Top-5 leagues are accepted; Champions League is not accepted.
- Signal-time and closing-benchmark roles are distinct.
- Closing observations cannot enter prediction input.
- No provider, bookmaker, signal-time candidate, result source, model, or
  production authority is selected automatically.
- No provider module is called by this contract or its tests.
- `APPROVED_FOR_CONTROLLED_ACTIVATION` remains a validation state in PR #59;
  this module contains no activation operation and creates no CEO authorization.
- All output remains validation-only, NO-BET, unpublished, and disabled by
  default.

## Verification

The focused suite is
`tests/football/test_top5_shadow_provider_redundancy.py`. It covers:

- the canonical adapter and its legitimate source boundary;
- current source inventory and separate capability dimensions;
- exact alias, league, fixture, kickoff, market, home/away, and odds checks;
- malformed, partial, stale, duplicate, and provider-failure evidence;
- provenance and request identity;
- closing leakage rejection;
- v1 evidence and NO-BET compatibility;
- exhausted quota preflight, zero-cost authentication distinction, no retries,
  and no fan-out;
- readiness-state planning, candidate quota-independent paths, and explicit
  no-selection behavior; and
- structural contract rejection and caller-supplied timing policy; and
- no-network guarantees.

Broader football, runtime, monitoring, and financial-safety regressions remain
required before CEO review. No real observation has been performed here; real
observations are still required before any candidate can be considered for
authority or claimed as a quota-independent shadow path.

## Unresolved CEO decisions

The following remain intentionally unresolved:

- final fixture, odds, and result authorities;
- whether the canonical provider may be observed with live credentials;
- signal-time window, freshness threshold, retry cadence, and market scope;
- provider quota/cost budget and the fail-closed policy;
- minimum observation period and coverage thresholds;
- final production model and artifact;
- rollout league order and controlled scope;
- publication policy; and
- any actual Controlled Activation authorization.

## Recommendation

TOP-5 SHADOW PROVIDER CONTRACT READY FOR CEO REVIEW
