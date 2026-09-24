# Top-5 runtime operations seam

## Purpose

`src/football/top5_runtime_operations.py` is the single offline-first seam
between the accepted Top-5 evidence/qualification contracts and a future
operator-owned runtime. It composes the existing injected shadow pipeline; it
does not replace Builder 1 acceptance, Builder 4 provider execution, the
publication boundary, or the financial ledger.

The production provider identity remains `the_odds_api`. TheRundown remains a
candidate/shadow provider and is rejected by the runtime input contract. The
canonical runtime order is the repository order:

`BL1 → EPL → LL → SA → L1`

## Offline execution contract

```text
run_offline_top5_runtime(Top5RuntimeRequest, checkpoint_store=...)
```

The request must contain exactly one input for each canonical league, at least
one fixture per league, signal-time snapshots only, and the disabled/no-bet
safety flags. A result binding is either absent for every league (the report
stops at `SIGNALS_READY`) or covers every fixture in every league. Partial
settlement is rejected at input validation.

The runner validates the complete request before running any league. It then
uses the existing `run_offline_compatibility` seam for each league. A single
league failure returns `FAILED` with zero predictions, signals, settlements,
and committed outputs. A successful complete fixture/result rehearsal returns
`READY` and the stages:

```text
FIXTURES_DISCOVERED
→ ODDS_REFRESHED
→ PREDICTIONS_GENERATED
→ SIGNALS_READY
→ RESULTS_INGESTED
→ SETTLED
```

The `provider_request_count` in the report is the number of injected logical
requests exercised by the offline harness. `network_request_count` is always
zero. No credentials, quota, provider, scheduler, publisher, Cloudflare,
ledger, or financial writer is imported by the seam.

Closing snapshots may be supplied separately as benchmark evidence, but they
are never passed into prediction. Every emitted signal is shadow/no-bet.

## Restart and idempotency

`Top5RuntimeCheckpoint` is a deterministic, serializable marker containing the
run/session identity, request digest, report digest, and checkpoint digest.
`Top5RuntimeCheckpointStore` is the persistence interface for the future
operator-owned external state location. The included
`InMemoryTop5RuntimeCheckpointStore` is only a test double.

Replaying the same request returns the same report. Reusing a run ID with a
different request digest fails closed. A failed partial run is never committed
to the checkpoint store, so it cannot become a downstream authority input.

## Activation precheck

The precheck is side-effect-free:

```text
python3 scripts/top5_activation_precheck.py --input <operator-precheck.json>
```

It returns `TOP5_RUNTIME_ACTIVATION_BLOCKED` unless all of the following are
explicitly supplied:

- genuine five-league evidence reference;
- Builder 1 final acceptance;
- `the_odds_api` provider authority;
- model and frozen Research bindings;
- approved Signal-Time configuration;
- scheduler/workflow and health readiness;
- rollback readiness;
- explicit activation authorization; and
- non-synthetic evidence.

Even a successful precheck reports `activation_mode=disabled` and
`mutation_performed=false`. It is a gate report, not an activation command.
Publication remains a separate authorization and is reported as a warning when
its own preflight is not ready.

Example input shape (values are deliberately not fabricated here):

```json
{
  "evidence_reference": "<B4/B1 evidence reference>",
  "five_league_evidence_valid": false,
  "builder1_acceptance_passed": false,
  "provider_authority": "the_odds_api",
  "provider_authority_granted": false,
  "model_bound": false,
  "research_bound": false,
  "signal_time_approved": false,
  "scheduler_ready": false,
  "health_ready": false,
  "rollback_ready": false,
  "activation_authorized": false,
  "publication_preflight_ready": false,
  "no_synthetic_evidence": false,
  "league_scope": ["BL1", "EPL", "LL", "SA", "L1"],
  "runtime_config": {
    "provider_identity": "the_odds_api",
    "activation_mode": "disabled",
    "scheduler_enabled": false,
    "publication_enabled": false,
    "betting_enabled": false,
    "ledger_mutation_enabled": false,
    "max_retries": 0,
    "timeout_seconds": 30,
    "maximum_odds_age_seconds": 900
  }
}
```

## Operational decisions

### Partial failure

Fixture discovery, odds refresh, prediction, and signal preparation are an
all-or-nothing five-league rehearsal. Missing results stop the report at
`DEGRADED/SIGNALS_READY`; settlement is not attempted. A complete result set
is required before the offline settlement marker is emitted.

### Retry and timeout

The seam performs no network request and no retry. Runtime policy accepts only
bounded `max_retries` values (0 or 1) and a positive timeout, while the current
default is zero retries. Builder 4's request/quota limits remain authoritative
for any future network execution.

### Health states

The health contract exposes `DISABLED`, `READY`, `RUNNING`, `DEGRADED`,
`FAILED`, `BLOCKED_BY_EVIDENCE`, `BLOCKED_BY_AUTHORITY`, and `STALE`. The
payload always includes all five leagues, stage, failure detail, provider
authority, freshness timestamp, request/retry counters, and disabled/no-bet
flags.

### Rollback and canary

The existing `RollbackController` remains the rollback authority. Any future
runtime failure must restore disabled, unpublished, no-bet, non-scheduled,
ledger-untouched state and preserve evidence. The first real canary remains a
separate CEO gate after B4 evidence and Builder 1 acceptance; this seam does
not create or execute it.

## Builder interfaces

Builder 4 supplies genuine provider evidence and remains the only owner of
quota proof, Discovery, event IDs, and real provider execution. Its outputs
enter the accepted B2/B1 contracts; they do not alter runtime provider
authority.

Builder 1 may rely on the runtime report/checkpoint fields for run/session
identity, exact five-league coverage, stage status, no-bet/publication flags,
provider authority, request/retry counts, signal/provenance artifacts, result
bindings, settlement markers, health, and deterministic digests. The report is
not itself provider authority, activation authority, or publication authority.

## Explicit remaining gates

This change removes the offline orchestration and precheck glue. It does not
authorize or perform:

- real B4 provider/quota/Discovery execution;
- provider authority changes;
- production model binding or Signal-Time approval;
- production activation or scheduler registration;
- public publication;
- betting or ledger mutation;
- deployment or Cloudflare changes.
