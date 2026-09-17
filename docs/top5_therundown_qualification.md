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
normalized_record_digest, adapter_version, quota_state_before,
quota_state_after, rate_limit_state, quota_cost_units,
network_request_count, synthetic_reconstruction, provider_status,
failure_codes
```

`raw_record_digest` and `normalized_record_digest` are SHA-256 digests. The
evaluator never treats a digest or an `accepted` flag as proof by itself;
`REAL_OBSERVED`, one documented network request, and every required evidence
field must pass together. `TEST_FIXTURE`, `MOCK`, `OFFLINE_REPLAY`, synthetic
reconstruction, provider failure, and incomplete provenance cannot qualify.

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
