# Top-5 shadow readiness

This workstream adds offline readiness evidence for Bundesliga, Premier
League, La Liga, Serie A, and Ligue 1. It does not register a league, call a
provider, schedule a job, publish an artifact, write the ledger, or activate
betting.

## Adapter boundary

`src/football/top5_adapters.py` exposes five immutable adapter descriptions
from the merged production contracts. Every entry has league identity,
provider sport and competition mapping, fixture/result sources, narrow
shadow/staged/health namespaces, a `unbound` model slot, and an
`unconfigured` signal-time slot. The descriptions are separate from
`src.config.LEAGUE_REGISTRY` and remain disabled by default.

## Offline compatibility

`src/football/top5_offline.py` runs an injected static fixture source, static
odds source, deterministic feature boundary, dummy model, no-bet shadow
decider, and in-memory sink through `run_shadow_pipeline`. It proves:

- fixture identity and provider mapping boundaries;
- feature -> model -> signal provenance;
- signal-time-only prediction input;
- shadow/no-bet artifact validation;
- league isolation and one bulk request;
- archive and staged-public artifact references under owner-specific Top-5 namespaces.

The staged-public reference is a readiness object only. Publication is always
`False`, and no current production signals artifact is changed.

## Signal-time simulator

`src/football/top5_signal_time.py` evaluates event-relative windows using an
explicit minimum lead, maximum lead, maximum odds age, retry interval, retry
limit, and expected inference duration. It reports fixture eligibility,
misses, first eligible execution, retry count, expected inference time, stale
snapshot rejections, duplicate dispatch suppression, coverage, and evaluated
request batches. All event and capture timestamps are timezone-aware; naive
timestamps fail closed.

## Dispatch contract

`src/football/top5_dispatch.py` builds stable keys from league, fixture,
signal-time contract identity, and snapshot generation. The in-memory claim
store suppresses repeats for the same key. A repeat is accepted only with a
nonblank explicit retry reason. Changed snapshot generations, contracts, or
leagues produce different keys.

## Provider/source matrix

`src/football/top5_source_matrix.py` records the repository's current routes:
TheOddsAPI discovery/bulk odds for fixtures and odds, football-data CSV for
results, and the current TheOddsAPI scores fallback only for D1 and E0. The
matrix explicitly records that the other three football-data codes have no
fallback in the current `results_router`. It keeps shadow authority static and
live authority undecided pending separate provider validation and CEO
approval; it does not inherit research conclusions or choose a live policy.

## Quota comparison

`src/football/top5_quota.py` estimates deterministic request units for any
caller-supplied matchday, week, or 72-hour fixture counts. Signal attempts,
revalidation, closing capture, bulk size, fallbacks, markets, and regions are
explicit assumptions. It compares named architectures without setting a
cadence or purchasing quota.

## Health

`src/football/top5_health.py` produces an in-memory disabled health payload
with league, fixture count, eligibility, predictions, skipped/stale counts,
provider failures, retries, duplicate suppression, model identity, contract
identity, and `no_bet=true`. It has no health registration or writer side
effect.

Final live decisions remain open: exact signal-time values, provider/result
authority, cadence, quota plan, model binding, and any activation approval.
