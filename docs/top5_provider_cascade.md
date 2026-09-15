# Top-5 multi-provider odds cascade

Status: `SHADOW ONLY`, `NO-BET`, `UNPUBLISHED`, `CANDIDATE_ONLY`.

This document describes Builder 4's routing and provider-client boundary. It
does not activate Top-5, choose a live authority, call a provider, publish an
artifact, write the ledger, or change Research. Built-in adapters are blocked
unless a controlled shadow run explicitly sets `live_calls_authorized=True` and
provides `controlled_shadow_run_ref`; the default is fail closed.

## Verification date and official sources

Provider documentation was checked on **2026-09-16**. The implementation is
based on the current official pages below; remembered plan limits are not used
as billing authority.

| Provider | Official references checked | Current contract used by this seam |
| --- | --- | --- |
| The Odds API | [v4 API guide](https://the-odds-api.com/liveapi/guides/v4/), [v4 error codes](https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html), [sports coverage](https://the-odds-api.com/sports-odds-data/) | `https://api.the-odds-api.com/v4`; `GET /sports/{sport}/odds`; query `apiKey`, `regions`, `markets`, `oddsFormat`; bookmaker `last_update`; quota headers `x-requests-used`, `x-requests-remaining`, `x-requests-last`. |
| Odds-API.io | [documentation index](https://docs.odds-api.io/llms.txt), [fetching odds](https://docs.odds-api.io/guides/fetching-odds), [OpenAPI](https://docs.odds-api.io/api-reference/openapi.json), [authentication](https://docs.odds-api.io/authentication) | `https://api.odds-api.io/v3`; query-key authentication; `GET /events/search` for discovery and `GET /odds/multi` for up to ten known event IDs; football `ML`; bookmaker market `updatedAt`; `X-Next-Since` is a delta-polling cursor, not treated as a quota counter. |
| API-Football | [v3 reference](https://www.api-football.com/documentation-v3), [getting started](https://www.api-football.com/news/post/how-to-get-started-with-api-football), [rate-limit headers](https://www.api-football.com/news/post/how-ratelimit-works), [terms](https://www.api-football.com/terms) | `https://v3.football.api-sports.io`; header `x-apisports-key`; `GET /fixtures` for discovery and `GET /odds?fixture=...&page=1` for pre-match odds; `paging`; daily/per-minute rate headers. The public odds examples do not establish a reliable per-quote source timestamp, so the adapter rejects a response unless an explicit `updatedAt`/`lastUpdate` equivalent is present. |
| Betfair delayed | [Getting started and endpoints](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687786/Getting%2BStarted), [application keys](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687105/Application%2BKeys), [market catalogue](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687517), [market data limits](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687478), [betting types](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | JSON-RPC `https://api.betfair.com/exchange/betting/json-rpc/v1`; headers `X-Application` and `X-Authentication`; `listMarketCatalogue` discovery and `listMarketBook` prices; `publishTime`, `isMarketDataDelayed`, and `EX_BEST_OFFERS`. The delayed app key is explicitly retained as delayed and is never presented as real-time. |

### Capability matrix

| Capability | The Odds API | Odds-API.io | API-Football | Betfair delayed |
| --- | --- | --- | --- | --- |
| Auth | API key query parameter | API key query parameter | `x-apisports-key` header | application key + session token headers |
| Football fixtures/events | sport-level odds/events payload | `/events/search`, `/events/{id}` | `/fixtures` | `listMarketCatalogue` |
| Pre-match 1X2 | `h2h` | `ML` | `/odds`, Match Winner / bet id 1 | football Match Odds market |
| Source/update timestamp | bookmaker or market `last_update` | market `updatedAt` | not guaranteed by public odds schema; fail closed if absent | `publishTime` where returned |
| Source identity | bookmaker `key`/title | bookmaker map key | bookmaker name/id | exchange market, marked delayed |
| Bulk | sport-level odds; event odds separately | `/odds/multi`, up to 10 events | fixture odds response, paginated | multi-market book calls subject to data-weight limits |
| Quota/rate evidence | usage headers; local baseline is `used=500`, `remaining=0` | free-tier limits are documented, but response quota headers are not relied upon | daily and per-minute response headers | market-data request-weight limits; no generic daily quota assumed |
| Delay | near-real-time feed semantics | real-time REST/WebSocket marketing claim; timestamp still required | provider update cadence, not signal-time proof by itself | official delayed app-key snapshots, documented as 1–180 seconds |
| Initial status here | `CANDIDATE_ONLY`, preflight-exhausted by known baseline | `CANDIDATE_ONLY` | `CANDIDATE_ONLY` | `CANDIDATE_ONLY` |

The matrix is a capability inventory, not a provider-quality ranking. Builder 2
remains the independent quality and validation owner.

## Architecture

```text
Fixture + explicit provider IDs
              |
              v
     ProviderCascadeRouter
              |
      preflight / local budget
              |
      one bounded adapter attempt
              |
      strict provider parser
              |
   NormalizedOddsObservation
              |
      freshness + identity boundary
              |
      Builder 1 shadow seam
              |
   M5 / evidence (still no-bet)
```

`src/football/provider_cascade/adapters.py` is the only place that knows the
provider payload shapes and endpoint details. The router, budget manager,
health registry, and Builder 1 seam consume provider-neutral objects.

## Configuration

Provider order is data, not permanent business authority:

```python
ProviderCascadeConfig(
    provider_order=(
        "the_odds_api",
        "odds_api_io",
        "api_football",
        "betfair_delayed",
    ),
    providers=provider_configs,
    global_request_budget=4,
    per_run_cap=4,
    allow_candidate_only=True,
    live_calls_authorized=False,
)
```

Every `ProviderConfig` controls enabled state, league and market allow-lists,
the explicit bookmaker allow-list required by Odds-API.io,
per-provider request cap, quota reserve, timeout, credential environment names,
shadow-only state, quality eligibility, candidate state, and adapter version.
Configuration validation rejects duplicate or unknown order entries, unused
provider configurations, invalid budgets, non-positive timeouts, and retries.
`max_attempts` is intentionally fixed at one.

Legacy behavior is reproducible with:

```python
provider_order=("the_odds_api",)
```

The default The Odds API config retains the latest verified local exhausted
baseline (`used=500`, `remaining=0`). A controlled shadow run must provide a
quota override and a run reference before a built-in adapter can make a
request; tests use injected responses and never consume that quota.

## Normalized odds contract

`NormalizedOddsObservation` requires:

- Top-5 league, stable caller fixture key, provider fixture/event ID, exact
  home/away teams, kickoff in UTC.
- `football:pre_match:1x2`, decimal home/draw/away odds, all finite and `> 1`.
- Provider and bookmaker identity, explicit source timestamp, capture time,
  request identity/start/completion, latency, provider priority, and fallback
  depth.
- Quota before/after, rate state, source provenance, raw-record digest, and
  adapter version.
- Complete 1X2 state, error classification, candidate/validated state, and
  delayed semantics where applicable.

An observation with a missing or future source timestamp, partial 1X2, closing
market, in-play event, invalid decimal, wrong league, wrong team order, or
kickoff mismatch cannot be accepted. Source timestamp is never replaced with
capture time or cache-read time.

## Quota preflight and request budget

`RequestBudgetManager.preflight()` runs before an adapter is called. It checks,
in order:

1. provider configuration and credential availability;
2. global cap and per-run cap;
3. per-provider request cap;
4. known remaining quota plus provider safety reserve;
5. known rate-limit remaining capacity.

If any known guard cannot safely support the request, the manager records a
pre-network rejection and returns `network_called=false`. The router proceeds
to the next configured provider. No retry is made against an exhausted quota.

The manager tracks attempted, successful, rejected-before-network, and known
quota-consumed counts, plus remaining/reset/rate fields where available. It is
local run state and is not billing authority: it cannot buy quota, alter a
subscription, create accounts, or authorize paid overages.

## Fallback state machine

There is one sequential attempt per configured provider. Fallback is not a
retry and there is no automatic fan-out.

| Failure | State recorded | Action |
| --- | --- | --- |
| disabled / missing credentials | `CONFIG_DISABLED` / `CREDENTIAL_MISSING` | no network; continue |
| exhausted quota / reserve | `QUOTA_EXHAUSTED` | no network; continue |
| rate limit | `RATE_LIMITED` | continue |
| 401 / 403 | `AUTH_FAILED` | continue and record safe HTTP class |
| 404 / missing event | `UNSUPPORTED_FIXTURE` | continue |
| 422 / wrong market | `UNSUPPORTED_MARKET` | continue |
| timeout / network exception / 5xx | `TEMPORARILY_UNAVAILABLE` | continue |
| wrong league or swapped teams | `UNSUPPORTED_LEAGUE` / `QUALITY_REJECTED` | continue |
| malformed, partial, missing timestamp | `MALFORMED` / `PARTIAL` / `STALE` | continue |
| all providers rejected | `FAIL_CLOSED` trace | no odds reach M5 |

The trace records attempt index, provider, outcome, reason, safe request
identity, status code, latency, and `network_called`. It never records auth
headers, query keys, session tokens, or secret-bearing URLs.

## Fixture identity and market contract

Provider switching requires an exact match on normalized league, explicit team
aliases, home/away order, kickoff within the bounded tolerance, and provider
event identity. Explicit aliases are deterministic mappings; substring or
fuzzy matching is not used. Swapped home/away and ambiguous duplicate events
fail closed.

Only football pre-match 1X2 is accepted. The contract requires home, draw, and
away together. It does not convert two-way markets, synthesize a draw, accept
in-play odds, accept spreads/totals, or use closing odds as model input.

The current seam does not add a cache. An upstream cache may only be reused if
provider identity, original source timestamp, fixture identity, and freshness
contract are all preserved; cache-read time can never reset source time.

## Provider notes

### The Odds API

`TheOddsAPIAdapter` reuses the existing redacted SportsBrain transport when a
legacy transport is injected and parses the documented v4 sport-level odds
shape. It selects one deterministic, lexicographically ordered bookmaker with
complete h2h prices and preserves `last_update`. The existing
`RealTop5Provider` path remains present and usable; this adapter is the narrow
cascade integration seam and does not rewrite M5 or the existing generic odds
refresher.

The latest repository evidence reports `authenticated=true`, `used=500`, and
`remaining=0`. The cascade therefore refuses a quota-consuming call before the
network when that state is supplied.

### Odds-API.io

The adapter supports known provider event IDs through `/odds/multi` and parses
the documented `bookmakers -> ML -> odds[0]` shape. `discovery_request()` is
exposed for a separately budgeted event-discovery phase. The router does not
silently chain discovery plus odds calls because that would make cost and
fallback behavior non-deterministic. `updatedAt` is required. The provider is
candidate-only and no sharp-market authority is inferred from bookmaker data.

### API-Football

Fixture discovery is represented separately through `discovery_request()`;
odds retrieval uses `/odds` with a known fixture ID and reads `paging`. Page 1
is not promoted when additional pages are present. Results and predictions
are not conflated with bookmaker odds. The adapter requires explicit source
timestamp data even though the public odds contract does not guarantee it;
capture time and fixture kickoff are not acceptable substitutes.

### Betfair delayed

The adapter is restricted to `listMarketBook` with `EX_BEST_OFFERS` and a
known market ID. `listMarketCatalogue` is exposed as a discovery request. It
requires delayed market data, exact runner mapping for home/draw/away, and an
explicit `publishTime`-like source timestamp. `isMarketDataDelayed`, delayed
app-key semantics, and any returned delay metadata remain in the normalized
observation. The adapter is never called `betfair_realtime` and cannot place
orders.

## Builder 1 and Builder 2 boundaries

Builder 1 can call `accepted_for_builder1(result)` and receives only:

```python
Builder1OddsInput(
    observation=NormalizedOddsObservation(...),
    routing=CascadeDecisionTrace(...),
)
```

The seam can convert to the existing `Fixture` and signal-time
`MarketSnapshot` contracts, but it imports no model, changes no model
probabilities, and creates no artifact or signal sink. A fail-closed result
raises before M5 receives odds.

Builder 2 remains independent. The `candidate_only` flag and routing trace are
evidence, not self-authorization. Builder 2 may independently reject any
provider, timestamp, fixture, bookmaker, or cascade result.

## Health and observability

`ProviderHealthRegistry` retains only safe runtime state: configured and
credential-present booleans, candidate/validated state, preflight/attempt/
success/failure times, last failure class, rolling availability, quota, and
rate-limit state. It never stores credential values. The cascade trace is
serializable for Shadow Evidence provenance, with the same secret-redaction
boundary.

## Safety and rollout

This workstream remains:

- `NO-BET`, `UNPUBLISHED`, `SHADOW ONLY`;
- no real provider calls in tests;
- no ledger, Research, sealed-data, Cloudflare, scheduler, launchd, PWA,
  deployment, billing, or subscription mutation;
- no Champions League activation;
- no automatic provider authority or live activation.

The strongest conclusion supported by this branch is:

> **TOP-5 MULTI-PROVIDER CASCADE IMPLEMENTATION READY FOR CEO REVIEW**

That conclusion does not mean live-ready. The four adapters remain
`CANDIDATE_ONLY` until separately authorized controlled observations and
independent Builder 2 validation exist.

## Rollback

Rollback is a configuration change: stop using the cascade or set
`provider_order=("the_odds_api",)` with the existing The Odds API path. No data
migration, scheduler change, ledger change, deployment, or provider account
change is required. If a provider produces unexpected data, disable that
provider and keep the remaining order explicit; if none is eligible, the
router fails closed.

## Remaining CEO-controlled decisions

- authorize any real controlled shadow run and its exact provider credentials;
- approve provider quality after Builder 2's independent validation;
- approve freshness windows, bookmaker/source policy, and any future live
  authority;
- approve any future cache persistence, fan-out, retries, or activation.
