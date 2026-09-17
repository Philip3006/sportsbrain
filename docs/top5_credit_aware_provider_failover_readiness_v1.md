# Top-5 Credit-Aware Provider Failover and Release-Day Readiness V1

This package is preparation only. It adds no provider authority, does not
select a source, and performs no provider request. All acceptance traffic is
injected.

## Current provider behavior audit

| Provider | Credential / budget | Current live behavior | Top-5 status |
| --- | --- | --- | --- |
| The Odds API | `ODDS_API_KEY`; monthly request credits; legacy `api_usage.json` records used/remaining; Top-5 request cost is one unit | Legacy `src/data/odds_api.py` uses a one-hour cache, three HTTP attempts with 5/15-second backoff, 422 market fallback, and stale-cache fallback. Legacy football refresher checks quota/circuit before calling. 401/403/429 open the circuit. | Existing adapter implemented; source-time capable; candidate-only; current observed quota is exhausted until a legitimate reset boundary or new evidence. |
| Odds-API.io | `ODDS_API_IO_KEY`; provider quota/rate limits are not persisted by the legacy path; one Top-5 odds request costs the configured unit | No legacy football refresher path. Top-5 adapter requires a pre-resolved event ID, bookmaker allow-list, exact event identity, `updatedAt`, and complete ML 1X2. 401/403/429 and malformed/partial responses fail closed. | Adapter implemented and source-time capable; controlled-shadow candidate; provider qualification and authority are absent. |
| API-Football | `API_FOOTBALL_KEY`; provider quota/rate limits are not persisted by the legacy path; one configured request cost unit | No legacy football odds fallback. Top-5 adapter requires a resolved fixture ID, checks body errors and pagination, and parses documented Match Winner rows. Current normalized path is capture-time-only when no source timestamp is supplied. | Adapter implemented, but source timestamp capability is unsupported for this bridge; candidate-only and blocked for real signal-time readiness. |
| Betfair Delayed | `BETFAIR_APP_KEY` + `BETFAIR_SESSION_TOKEN` for the Top-5 adapter; legacy football module instead requires app key, username, and password. Vendor request weights are not declared. | Legacy Betfair login/session is in-memory for four hours and football catalogue/book data is cached in-memory for five minutes. The legacy quote does not retain exact kickoff/source-time evidence. Top-5 adapter requires resolved market/runner mapping and delayed semantics. | Adapter implemented, but current source-time capability is capture-time-only; candidate-only and blocked for real signal-time readiness. |
| OddsPortal | No declared credential; no persisted quota; Cloudflare/403 is a normal failure | Legacy refresher calls the cached day page for a 15-minute in-memory window and accepts only a 1X2 aggregate. The page does not retain exact kickoff, league identity, or source timestamp. | Legacy fallback only; not part of the Top-5 cascade and not signal-time eligible. |

The exact legacy football refresher order is Betfair, then OddsPortal, then The
Odds API, followed by fail-closed `None`. WebSearch is not an authoritative
football fallback. Its module-level documentation describes a different
historical order; this document records the executable order.

Legacy circuit state is stored in external operator runtime state. The mutable
The Odds API usage evidence is now written there as redacted data containing
`requests_used`, `requests_remaining`, `observed_at`, the next calendar-month
`reset_at`, state, and evidence source. A legacy file without reset evidence
remains fail-closed.

## Monthly reset deadlock fix

The legacy provider budget now distinguishes a monthly The Odds API quota from
daily transient circuit resets:

1. Known `remaining=0` before `reset_at` opens/keeps the circuit closed to
   network dispatch; no call is made.
2. Missing, malformed, or unknown reset evidence also remains blocked.
3. At or after the explicit next-calendar-month boundary, the provider becomes
   eligible for one future authorized bounded revalidation. The decision does
   not perform or schedule that probe.
4. New response headers replace the state with fresh observed quota evidence.

No automatic retry loop, quota purchase, account creation, or hidden override
is introduced.

## Top-5 persistent quota state

`QuotaStateStore` stores only redacted per-provider state under
`SPORTSBRAIN_RUNTIME_STATE_DIR` (or the platform operator runtime directory):

```text
provider, observed_at, used, remaining, rate_limit, rate_remaining, reset_at,
rate_reset_at, state, source
```

Writes are atomic and the store rejects paths inside the active checkout.
`RequestBudgetManager` accepts the store explicitly, loads persisted state for a
new run, and persists quota evidence after an adapter result. Explicit test or
run quota overrides take precedence. Persistence is state only; it cannot
authorize a provider or issue a Builder-2 receipt.

## Deterministic Top-5 failover

The existing `ProviderCascadeRouter` remains the one Top-5 routing authority:
sequential configured order, one attempt per provider, request count separate
from quota-cost units, preflight before network, and immediate return after one
successful candidate observation. Zero remaining quota, missing credentials,
429, auth failure, timeout, stale, malformed, wrong-fixture, or identity
failure records a typed attempt and advances only to the next explicitly
configured and authorized provider. When all paths fail, the result is
fail-closed with no fabricated observation.

The Odds API and Odds-API.io are the current source-time-capable candidates.
API-Football and Betfair Delayed remain explicit provider-specific blockers for
the real signal-time bridge until their existing adapters provide canonical
source timestamp provenance. Candidate adapters and configured credentials do
not establish provider authority or qualification.

## Secret-free readiness and release preflight

`build_provider_readiness_view()` exposes one deterministic view per provider:

- provider and current state;
- credential-present boolean only;
- authorization for the supplied current run;
- quota known/unknown, remaining, reset time, evidence timestamp and age;
- rate-limit state;
- last successful observation and last failure class;
- fallback eligibility;
- source timestamp capability;
- identity readiness; and
- explicit parser, quota/header, and runtime-health contract status.

`release_day_preflight()` and
`scripts/top5_provider_release_preflight.py` read only caller-supplied config
and operator state. They report `READY_FOR_PROVIDER_RUN` only when at least one
provider is explicitly authorized and satisfies the evidence, identity,
source-time, credential, quota, and budget gates. Otherwise they report exact
blockers including `CREDENTIAL_MISSING`, `QUOTA_EXHAUSTED`,
`QUOTA_STATE_STALE`, `PROVIDER_NOT_AUTHORIZED`, `IDENTITY_UNRESOLVED`,
`SOURCE_TIMESTAMP_UNSUPPORTED`, `REQUEST_BUDGET_INVALID`,
`PROVIDER_NOT_READY`, `RATE_LIMITED`, and `NO_ELIGIBLE_PROVIDER`. The command
has no adapter or HTTP call path.

## External prerequisites for a real run

- A new run-specific CEO authorization must be supplied to the existing
  controlled-shadow harness.
- A fresh, externally observed quota state is required; a reset boundary only
  permits one bounded future revalidation and is not quota evidence itself.
- Each selected provider needs explicit credential presence, provider/run
  authorization, exact fixture identity, complete 1X2, and canonical source
  timestamp provenance.
- API-Football and Betfair Delayed need a separately reviewed source-time
  contract or remain unavailable for the real signal-time bridge.
- Builder 2 must independently validate any real observation; this package
  neither creates nor issues a qualification receipt.

No real provider request, quota consumption, account or billing change, spend,
publication, betting, production activation, ledger mutation, Research
mutation, or sealed-data access is performed by this package.
