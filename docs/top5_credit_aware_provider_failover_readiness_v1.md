# Top-5 Football Provider Quota and Release-Day Readiness V1

This package is preparation only. It adds no provider authority, does not
select a source, and performs no provider request. All acceptance traffic is
injected.

## Current provider behavior audit

| Provider | Credential / budget | Current live behavior | Top-5 status |
| --- | --- | --- | --- |
| The Odds API | `ODDS_API_KEY`; monthly request credits; redacted external usage state; one configured Top-5 request cost unit | The active Football refresher checks quota and authorization before one The Odds API request. 401/403/429, timeout, malformed, stale, or incomplete data fail closed. | The only approved football/Top-5 odds provider; candidate-only until the separately authorized real run and Builder-2 qualification. |

The active football refresher and Top-5 cascade both use The Odds API only.
There is no provider substitution, WebSearch authority, stale-quote promotion,
or alternate real-observation path.

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

## Deterministic Top-5 provider route

The existing `ProviderCascadeRouter` remains the one Top-5 routing authority:
sequential configured order, one attempt per provider, request count separate
from quota-cost units, preflight before network, and immediate return after one
successful candidate observation. Zero remaining quota, missing credentials,
429, auth failure, timeout, stale, malformed, wrong-fixture, or identity
failure records a typed attempt and stops. When the provider fails, the result
is fail-closed with no fabricated observation or provider substitution. The
single candidate and configured credentials do not establish provider
authority or qualification.

## Secret-free readiness and release preflight

`build_provider_readiness_view()` exposes one deterministic view for The Odds API:

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
and operator state. They report `READY_FOR_PROVIDER_RUN` only when The Odds API
is explicitly authorized and satisfies the evidence, identity, source-time,
credential, quota, and budget gates. Otherwise they report exact blockers
including `CREDENTIAL_MISSING`, `QUOTA_EXHAUSTED`, `QUOTA_STATE_STALE`,
`QUOTA_REVALIDATION_ELIGIBLE`, `PROVIDER_NOT_AUTHORIZED`,
`IDENTITY_UNRESOLVED`, `SOURCE_TIMESTAMP_UNSUPPORTED`, and
`REQUEST_BUDGET_INVALID`. There is no `FALLBACK_PROVIDER_AVAILABLE` state or
recommendation. The command has no adapter or HTTP call path.

## External prerequisites for a real run

- A new run-specific CEO authorization must be supplied to the existing
  controlled-shadow harness.
- A fresh, externally observed quota state is required; a reset boundary only
  permits one bounded future revalidation and is not quota evidence itself.
- The selected provider needs explicit credential presence, provider/run
  authorization, exact fixture identity, complete 1X2, and canonical source
  timestamp provenance.
- Builder 2 must independently validate any real observation; this package
  neither creates nor issues a qualification receipt.

No real provider request, quota consumption, account or billing change, spend,
publication, betting, production activation, ledger mutation, Research
mutation, or sealed-data access is performed by this package.
