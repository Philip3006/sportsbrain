# Top-5 The Odds API Football Provider Route

Status: `SHADOW ONLY`, `NO-BET`, `UNPUBLISHED`, `CANDIDATE_ONLY`.

This document describes Builder 4's routing and provider-client boundary. It
does not activate Top-5, choose a live authority, call a provider, publish an
artifact, write the ledger, or change Research. Built-in adapters are blocked
unless a controlled shadow run explicitly sets `live_calls_authorized=True` and
provides `controlled_shadow_run_ref`; the default is fail closed.

**NO PRODUCTION FRESHNESS THRESHOLD APPROVED.**

**NO PRODUCTION KICKOFF TOLERANCE APPROVED.**

Every experiment must pass an explicit `CascadeTimingPolicy` containing both
values. These are experiment inputs, not production policy defaults.

## Verification date and official sources

Provider documentation was checked on **2026-09-16**. The implementation is
based on the current official pages below; remembered plan limits are not used
as billing authority.

| Provider | Official references checked | Current contract used by this seam |
| --- | --- | --- |
| The Odds API | [v4 API guide](https://the-odds-api.com/liveapi/guides/v4/), [v4 error codes](https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html), [sports coverage](https://the-odds-api.com/sports-odds-data/) | `https://api.the-odds-api.com/v4`; `GET /sports/{sport}/odds`; query `apiKey`, `regions`, `markets`, `oddsFormat`; bookmaker `last_update`; quota headers `x-requests-used`, `x-requests-remaining`, `x-requests-last`. |

### Capability matrix

| Capability | The Odds API |
| --- | --- |
| Auth | `ODDS_API_KEY` query parameter |
| Football fixtures/events | sport-level odds/events payload |
| Pre-match 1X2 | `h2h` |
| Source/update timestamp | bookmaker or market `last_update` |
| Source identity | bookmaker `key`/title |
| Bulk | sport-level odds; event odds separately |
| Quota/rate evidence | usage headers; local baseline is `used=500`, `remaining=0` |
| Initial status here | `CANDIDATE_ONLY`, preflight-exhausted by known baseline |

The matrix is a capability inventory, not a provider-quality ranking. Builder 2
remains the independent quality and validation owner.

## Architecture

```text
Fixture + explicit timing policy + resolved provider identities
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
      explicit timing + identity boundary
              |
      serialized Builder-2 evidence
              |
      external Builder2QualificationReceiptV1
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
    provider_order=("the_odds_api",),
    providers=provider_configs,
    global_request_budget=1,
    per_run_cap=1,
    allow_candidate_only=True,
    live_calls_authorized=False,
)
```

`ProviderCascadeRouter` requires the timing policy at construction; omitting it
fails closed. A network-capable adapter additionally requires a
`NetworkAuthorizationContract` derived from the controlled-shadow run and the
authorized provider list. Authorization cannot enable betting or publication.

```python
ProviderCascadeRouter(
    config,
    timing_policy=CascadeTimingPolicy(
        maximum_odds_age_seconds=900,  # experiment input only
        kickoff_tolerance_seconds=90,  # experiment input only
    ),
)
```

Every `ProviderConfig` controls enabled state, league and market allow-lists,
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
- Provider and bookmaker identity, capture time, request identity/start/
  completion, latency, provider priority, and fallback depth.
- `source_timing_provenance`: `SOURCE_TIMESTAMP` only when a legitimate source
  timestamp exists; otherwise an explicit `CAPTURE_TIME_ONLY` classification.
- Quota before/after, rate state, source provenance, raw-record digest, and
  adapter version.
- Complete 1X2 state, error classification, candidate/validated state, and
  delayed semantics where applicable.

An observation with an unqualified timing provenance, future source timestamp,
partial 1X2, closing market, in-play event, invalid decimal, wrong league,
wrong team order, or kickoff mismatch cannot be accepted. Capture time is never
presented as source time. Builder 2 decides whether `CAPTURE_TIME_ONLY` is
sufficient for a given experiment; it is not passed through a source-age gate.

## Quota preflight and request budget

`RequestBudgetManager.preflight()` runs before an adapter is called. It checks,
in order:

1. provider configuration and credential availability;
2. global cap and per-run cap;
3. per-provider request cap;
4. known remaining quota plus provider safety reserve;
5. known rate-limit remaining capacity.

If any known guard cannot safely support the request, the manager records a
pre-network rejection and returns `network_called=false`. The football route
then stops. No retry or provider substitution is made against an exhausted
quota.

The manager tracks `requests_attempted` as actual network-call count and
`quota_consumed` as provider quota units; these are intentionally separate.
It also tracks successful, rejected-before-network, and remaining/reset/rate
fields where available. It is
local run state and is not billing authority: it cannot buy quota, alter a
subscription, create accounts, or authorize paid overages.

## Fail-closed state machine

There is one sequential attempt for the canonical provider. There is no retry,
provider substitution, or automatic fan-out.

| Failure | State recorded | Action |
| --- | --- | --- |
| disabled / missing credentials | `CONFIG_DISABLED` / `CREDENTIAL_MISSING` | no network; stop |
| exhausted quota / reserve | `QUOTA_EXHAUSTED` | no network; stop |
| rate limit | `RATE_LIMITED` | stop |
| 401 / 403 | `AUTH_FAILED` | stop and record safe HTTP class |
| missing event / wrong fixture | `UNSUPPORTED_FIXTURE` | stop |
| wrong market | `UNSUPPORTED_MARKET` | stop |
| timeout / network exception / 5xx | `TEMPORARILY_UNAVAILABLE` | stop |
| wrong league or swapped teams | `UNSUPPORTED_LEAGUE` / `QUALITY_REJECTED` | stop |
| malformed, partial, unqualified timing | `MALFORMED` / `PARTIAL` / `QUALITY_REJECTED` | stop |
| provider rejected | `FAIL_CLOSED` trace | no odds reach M5 |

The trace records attempt index, provider, outcome, reason, safe request
identity, status code, latency, and `network_called`. It never records auth
headers, query keys, session tokens, or secret-bearing URLs.

## Fixture identity and market contract

The provider identity requires an exact match on normalized league, explicit
team aliases, home/away order, kickoff within the caller-supplied tolerance,
and The Odds API event identity. A resolved identity carries the canonical
fixture key, provider ID, teams, kickoff, league evidence, provenance,
resolution time, resolver version, and digest.
Explicit aliases are deterministic mappings; substring or fuzzy matching is
not used. Swapped home/away and ambiguous duplicate events fail closed.

Any future discovery is a separately budgeted prerequisite. The router never
hides a second discovery request inside an odds attempt; unresolved identity
is visible with `network_called=false`.

The cascade remains candidate-only until Builder 2 supplies the canonical
`Builder2QualificationReceiptV1` together with its exact `REAL_OBSERVED`
qualification observation. Builder 4 only validates and consumes that external
receipt; it cannot issue or recreate Builder-2 qualification authority. The
consumer checks the receipt's schema, accepted safety state, session/run/CEO
identity, fixture/provider event and request identity, observation and cascade
digests, capture attestation, adapter provenance, and the selected trace's
`NETWORK_CAPABLE` marker. `TEST_INJECTED` output therefore cannot be promoted
through the Builder-1 seam.

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

## Builder 1 and Builder 2 boundaries

Builder 1 can call `accepted_for_builder1(result, validation_receipt,
qualification_observation=...)` only after the external Builder-2 V1 receipt
and its exact real observation have been supplied. The canonical receipt binds
the exact observation, cascade evidence, controlled-shadow run, qualification
session, CEO authorization, fixture/provider event and request identity,
capture attestation, and adapter provenance. A missing, rejected, candidate-only,
or mismatched receipt or observation raises before model inputs are exposed. The
result is:

```python
Builder1OddsInput(
    observation=NormalizedOddsObservation(...),
    routing=CascadeDecisionTrace(...),
    builder2_receipt=Builder2QualificationReceiptV1(...),
    qualification_observation=RealProviderObservation(...),
)
```

The seam can convert to the existing `Fixture` and signal-time
`MarketSnapshot` contracts, but it imports no model, changes no model
probabilities, and creates no artifact or signal sink. A fail-closed result
raises before M5 receives odds.

Builder 2 remains independent. `builder2_evidence_payload(result)` serializes
the configured order, every bounded attempt, identity, timing, quota, budget,
readiness, failure taxonomy, selected provider, and safety assertions using
the merged `top5-provider-cascade-validation-v1` contract. The
`candidate_only` flag and routing trace are evidence, not self-authorization.
Builder 2 may independently reject any provider, timing provenance, fixture,
bookmaker, or cascade result.

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

> **TOP-5 THE ODDS API-ONLY FOOTBALL PROVIDER PATH READY FOR CEO REVIEW**

That conclusion does not mean live-ready. The canonical adapter remains
`CANDIDATE_ONLY` until a separately authorized controlled observation and
independent Builder 2 validation exist.

## Rollback

Rollback is a configuration change: stop using the cascade or keep
`provider_order=("the_odds_api",)` disabled at preflight. No data migration,
scheduler change, ledger change, deployment, or provider account change is
required. If The Odds API is ineligible, the router fails closed.

## Remaining CEO-controlled decisions

- authorize any real controlled shadow run and its exact provider credentials;
- approve provider quality after Builder 2's independent validation;
- approve freshness windows, bookmaker/source policy, and any future live
  authority;
- approve any future cache persistence, fan-out, retries, or activation.
