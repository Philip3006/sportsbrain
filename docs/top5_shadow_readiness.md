# Top-5 shadow readiness hardening

This workstream is readiness and simulation code for Bundesliga, Premier
League, La Liga, Serie A, and Ligue 1. It remains disabled-by-default and
does not register leagues, call providers, schedule jobs, publish artifacts,
write the ledger, or activate betting.

## Already implemented

- `top5_adapters.py` exposes five immutable, league-owned metadata boundaries.
  Each contains league identity, provider sport/competition mapping,
  fixture/result sources, narrow archive/staged/health namespaces, an
  `unbound` model slot, an `unconfigured` signal-time slot, and a distinct
  health identity. The objects remain outside `src.config.LEAGUE_REGISTRY`.
- `top5_provider_semantics.py` separates logical fixture evaluations, bulk
  provider requests, event fallback requests, fallback outcomes, and
  configurable provider cost units.
- `top5_signal_time.py` evaluates event-relative schedules, coalesces
  compatible fixture attempts into sport-key bulk requests, and preserves
  per-fixture diagnostics. All retries are bounded by the candidate's
  `max_retries`.
- `top5_offline.py` runs static injected fixtures, odds, features, a dummy
  model, a no-bet shadow decider, and an in-memory sink through the merged
  shadow pipeline. It produces prediction -> signal -> archive reference ->
  staged-public reference evidence, with publication disabled.
- `top5_dispatch.py` retains deterministic idempotency over league, fixture,
  signal-time contract, and snapshot generation. An explicit retry reason is
  required for a repeated low-level claim, while orchestration stops after a
  duplicate accepted dispatch and never generates a second signal for it.
- `top5_quota.py` estimates logical evaluations, bulk/event/result/
  revalidation/closing requests, raw HTTP count, and injected cost units for
  caller-supplied matchday, 72-hour, and one-week fixture counts.
- `top5_health.py` returns disabled in-memory readiness health; it has no
  registration or writer side effect.

## Normal bulk provider path

The repository's normal odds path is a sport-key bulk request:

`/sports/{sport}/odds`

One request can serve multiple fixtures for the same provider, sport key,
markets, regions, and deterministic request bucket. The simulator therefore
counts one `BulkProviderRequest` for a compatible group while retaining one
`LogicalFixtureEvaluation` per fixture and attempt.

For example, two BL1 fixtures in the same request bucket produce two logical
evaluations and one bulk request. An EPL fixture in the same time bucket is a
separate bulk request because its sport key and league identity differ.

## Event-level fallback path

The repository's fallback implementation can discover events through
`/sports/{sport}/events` and then request `/sports/{sport}/events/{id}/odds`
per event after a bulk 422/unsupported-market path. The simulator models
this as a bulk request with an explicit `BulkRequestOutcome`, followed by
zero or more `FallbackEventRequest` objects selected by a static
`FallbackScenario`.

Supported simulated outcomes include bulk success, unsupported markets,
partial event fallback, provider timeout, and empty payload. A scenario being
simulatable is not provider approval; no fallback client is imported or
called by this workstream.

## Request count versus provider cost

These are separate quantities:

| Quantity | Meaning |
| --- | --- |
| Fixtures | Events in the supplied horizon |
| Logical signal evaluations | One fixture at one candidate attempt |
| Bulk odds requests | Sport-key requests after compatibility coalescing |
| Event fallback requests | Per-event requests after an explicit bulk failure/inapplicability |
| Result requests | Configured result-source request assumptions |
| Raw HTTP request count | Sum of the request categories above |
| Provider cost units | Injected weighted sum; not a vendor billing claim |

The first readiness pass incorrectly treated signal requests as
fixtures × attempts. V2 reports that product as logical evaluations and
calculates normal-path bulk requests separately. Markets and regions remain
visible assumptions; they do not silently imply a billing rule.

## Quota/cost estimator assumptions

`QuotaAssumptions` supports independent choices for retry/evaluation count,
signal-window passes, bulk reuse, optional bulk batch caps, fallback counts or
probabilities, result requests, near-kickoff revalidation, closing capture,
markets, regions, and a `ProviderCostModel`. All five leagues are returned in
every estimate, with zero fixtures where appropriate. The estimator compares
named scenarios and does not select cadence, purchase quota, or approve a
provider plan.

Illustrative, non-approved example: with six fixtures across three leagues,
three logical attempts each, bulk reuse, one result request per active league,
and one revalidation per fixture, the estimate is 18 logical evaluations,
nine bulk odds requests, three result requests, six revalidation requests,
and 18 raw HTTP requests. A caller can assign different cost weights and the
estimated provider cost units will change without changing raw HTTP count.

## Candidate signal-time architecture

`SignalTimeCandidate` accepts explicit minimum lead, maximum lead, maximum
odds age, retry interval, maximum retries, inference duration, and request
bucket size. A test may use values such as 60/180 minutes and 120 seconds to
exercise the contract, but those values are examples only—not approved
production timings. Naive timestamps fail closed. The simulator reports:

- scheduled attempts and logical evaluations;
- eligible and missed fixtures and coverage;
- first eligible execution and expected inference time;
- stale/closing/no-snapshot failures;
- retry counts and max-retry exhaustion;
- coalesced bulk requests and event fallback requests;
- bulk provider failures and duplicate dispatch suppression.

## Results/source matrix correction

`top5_source_matrix.py` records the current repository semantics without
extending routing. Football-data CSV is the primary result source by code.
For the currently mapped D1 and E0 routes only, the fallback is the
sport-level:

`/sports/{sport}/scores`

The code returns and parses a sport-level result collection; this workstream
does not label it as inherently one request per match. SP1, I1, and F1 have
no current TheOddsAPI scores fallback mapping in `results_router`. Live
authority remains undecided and requires separate provider validation and
CEO approval.

## Five-league compatibility and safety

Parameterized tests cover BL1, EPL, LL, SA, and L1 for disabled metadata,
unbound models, unconfigured canonical signal time, immutable test shadow
config generation, provider mapping, fixture/league isolation, namespace and
health identity, no-bet signals, closing-odds rejection, and cross-league
rejection. Failure tests cover missed windows, no snapshots, stale/closing
snapshots, naive time, bulk failures, each fallback shape, duplicate claims,
legitimate retries, retry exhaustion, changed snapshots/contracts, compatible
multi-fixture bulk reuse, and separate cross-league requests.

## Unresolved CEO decisions

The following remain hypothetical and intentionally unselected:

- final signal-time values and retry cadence;
- provider and result authority after shadow evidence;
- model adapter binding and research approval;
- quota/cost budget and production request plan;
- any live league registration, controlled activation, or publication policy.

Builder A remains Research Owner. No research branch, model artifacts,
sealed outcomes, 2425/2526 data, or production runtime state is changed by
this workstream.
