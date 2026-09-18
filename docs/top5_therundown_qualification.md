# APP-B1 TheRundown qualification gate

This is a read-only, no-network evaluator for candidate TheRundown evidence.
It reports evidence readiness for each Top-5 league. It does not register the
provider, select provider authority, enable a model, publish, bet, or mutate
runtime or ledger state.

## Contract

The input is a JSON object with exactly these top-level fields:

```json
{
  "schema_version": "top5-therundown-qualification-evidence-v1",
  "provider_identity": "therundown_experimental",
  "evidence": []
}
```

Each item in `evidence` is a redacted projection of one APP-B2 provider
observation. The provider adapter's normalized values use the existing
provider-neutral names: `market_type` is
`football:pre_match:1x2`, `odds` contains `home`, `draw`, and `away`,
`request_identity` is retained as `provider_request_id`, and the three quota
objects use the existing `QuotaSnapshot` fields. The projection must also
make the real-evidence boundary explicit with `evidence_kind`,
`network_request_count`, and `synthetic_reconstruction`.

Required item fields are:

```text
schema_version, evidence_id, provider_identity, evidence_kind,
canonical_league, provider_league_code,
provider_league_identity_verified, fixture_observed, fixture_key,
provider_event_id, home_team, away_team, home_away_identity_verified,
kickoff, market_type, market_phase, odds, bookmaker_observed,
bookmaker_identity, source_timestamp, source_timing_provenance, captured_at,
provider_request_id, observation_id, source_provenance, raw_record_digest,
normalized_record_digest, provider_record_digest, adapter_version,
adapter_source_sha, authorization_metadata, quota_state_before,
quota_state_after, rate_limit_state, quota_cost_units,
network_request_count, synthetic_reconstruction, provider_status,
failure_codes
```

`raw_record_digest` and `normalized_record_digest` are SHA-256 digests. The
evaluator never treats a digest or an `accepted` flag as proof by itself;
`REAL_OBSERVED`, one documented network request, and every required evidence
field must pass together. `TEST_FIXTURE`, `MOCK`, `OFFLINE_REPLAY`, synthetic
reconstruction, provider failure, and incomplete provenance cannot qualify.

## Adapter bridge

`src/football/top5_therundown_qualification_bridge.py` provides the narrow
adapter-to-envelope projection. It accepts an existing `AdapterResult` with a
`NormalizedOddsObservation`, validates it against an explicit expected
`Fixture`, and returns one envelope item without I/O. The adapter result supplies
the raw response digest, normalized digest, status, and quota-after state. The
normalized observation supplies the provider identity, canonical fixture and
league, event ID, bookmaker, prices, source timestamp, capture time, request
identity, source provenance, adapter version, provider-record digest, and
quota-before state. The caller must explicitly supply the evidence ID,
observation ID, provider league code/verification, adapter source SHA, quota
cost, evidence kind, request count, and complete authorization metadata.

Authorization metadata is exact-bound to the provider, league, fixture, event,
and request and retains the controlled-run, qualification-session, and CEO
authorization IDs. It also carries provider/league/fixture scopes and the
no-bet, publication-disabled, monetary-spend-disabled safety flags. A
`TEST_FIXTURE` bridge result records zero network requests and synthetic
reconstruction; it remains explicitly non-real and the evaluator rejects it.
`REAL_OBSERVED` requires one network request, HTTP 200, non-synthetic evidence,
fresh source time, and matching authorization metadata.

## Criteria and statuses

One record is `QUALIFIED_EVIDENCE_READY` only when all nine criteria pass:

1. provider league identity is explicitly verified;
2. a real provider fixture and event ID are observed;
3. home and away identities are explicitly verified;
4. the market is pre-match regulation 1X2;
5. home, draw, and away prices are complete finite decimal odds above 1;
6. at least one bookmaker is explicitly observed;
7. an aware provider source timestamp is present and no older than the
   supplied maximum age at capture;
8. request, observation, source, adapter, and record provenance are retained;
9. quota-before, quota-after, rate-limit state, and request cost are recorded.

Per league, `UNOBSERVED` means there is no evidence item for that code.
`PARTIAL_EVIDENCE` means evidence is present and has no hard execution or
synthetic failure, but one or more criteria are missing. `FAILED` means a hard
failure or malformed/unsafe evidence is present. The overall status is
`QUALIFIED_EVIDENCE_READY` only when all five league results are qualified;
otherwise it remains partial or failed and never authorizes production.

## Offline command

```text
python3 scripts/top5_therundown_qualification.py \
  /absolute/path/evidence.json \
  --maximum-odds-age-seconds 300
```

The command makes no provider or network request. Exit code `0` means all five
league evidence results are ready. Exit code `2` means the report is partial,
failed, unobserved, or malformed.

## PR-88 real-evidence handoff

The reviewed PR-88 run is available to this gate as a redacted, machine-readable
summary at:

```text
tests/fixtures/therundown/pr88_top5_real_evidence_summary.json
```

The summary is pinned to PR-88 head
`c550e8037704eda58f536246e4563b47e3c2e8bb`, CEO review `5241854809`, and the
exact evaluation document blob recorded in the source block. It records 15 real
requests, 44 datapoints consumed, Free-tier access, a five-minute delay, and
DraftKings, BetMGM, and FanDuel entitlement. EPL, BL1, SA, and L1 each report a
real fixture with complete 1X2 coverage from three bookmakers; LL is unobserved
for the sampled date.

This summary is intentionally not a canonical observation envelope: it omits
exact odds, provider event/request identities, capture timestamp, raw-response
digest, normalized-record digest, and adapter-source binding. The consumer
therefore returns `PARTIAL_EVIDENCE`, lists those blockers, and cannot create or
validate a Builder-2 receipt. It also records the independently reviewed PR-91
shadow compatibility head for traceability; PR-91 is not modified or merged.
The `source_update_timestamp_range` values remain the ranges reported by PR-88;
they are not reinterpreted as exact per-price timestamps.

Run it offline with:

```text
python3 scripts/top5_therundown_qualification.py \
  tests/fixtures/therundown/pr88_top5_real_evidence_summary.json \
  --maximum-odds-age-seconds 300
```
