# Champions League publication and acceptance contract

This document defines the downstream, read-only CL delivery boundary. It is
pre-activation evidence only: this contract does not authorize provider
requests, live publication, scheduler changes, betting, or Worker deployment.

## Public shape

The existing public product remains the transport envelope. CL payloads add one
allowlisted top-level object:

```json
{
  "champions_league_release": {
    "schema_version": "champions-league-publication-v1",
    "competition": "UEFA Champions League",
    "league_code": "UCL",
    "generation_id": "...",
    "activation_state": "SHADOW",
    "publication_status": "UNPUBLISHED",
    "publication_enabled": false,
    "provider_authority": "...",
    "result_authority": "...",
    "source_sha": "sha256",
    "research_sha": "sha256",
    "model_artifact_hash": "sha256",
    "prediction_count": 1,
    "fixture_count": 1,
    "generated_at": "2026-09-23T12:09:30Z",
    "stale_after_seconds": 900,
    "no_bet": true
  },
  "football": [],
  "health": {"football_releases": []}
}
```

Each CL prediction is represented by exactly three football records for one
`prediction_id` and `fixture_key`: `home`, `draw`, and `away`. Accepted league
aliases are `ucl`, `champions_league`, `uefa_champs_league`, and
`soccer_uefa_champs_league`; release metadata and emitted record identities
are canonicalized to `UCL`, the runtime `CHAMPIONS_LEAGUE_CODE`.

Every record must carry football identity, kickoff, model identity, state,
result/settlement status, and a complete `provenance` object containing:

- `source`, `provider`, `source_sha`, `research_sha`, `model_artifact_hash`
- `snapshot_id`, `snapshot_kind`, `captured_at`, `source_age_seconds`
- `evidence_digest`

Record provenance is bound to the release envelope. The CL health release in
`health.football_releases` is also required and must bind its provider,
source SHA, publication state, and observation timestamp to the release.

## Safety and freshness

The publication boundary is fail-closed. CL records must remain `no_bet: true`,
must not be `ACTIVE`, and cannot be enabled or published without an explicit
controlled/live release envelope. Published records cannot be stale. Missing
release metadata, incomplete provenance, mismatched hashes/authorities,
duplicate/missing three-market groups, or stale timestamps reject the product.

The Worker applies the same contract to public `GET /signals.json` responses.
The PWA keeps CL competition filtering and renders the compatibility metadata,
result status, source age, and freshness state. CL cards are informational and
never expose a bet action, including compact mobile rendering.

## Offline acceptance harness

Use only a locally supplied snapshot:

```text
python3 scripts/champions_league_acceptance.py \
  tests/fixtures/champions_league/publication_offline.json \
  --now 2026-09-23T12:10:00Z \
  --max-age-seconds 900
```

The harness first applies the normal public serializer and then checks the CL
release, records, health, provenance bindings, and freshness. It returns
`CL_PUBLICATION_ACCEPTANCE_READY` only for a complete read-only snapshot and
`CL_PUBLICATION_ACCEPTANCE_BLOCKED` for incomplete or stale evidence.

## Production acceptance checklist

Before any future CL activation/publication review, collect fresh evidence from
the governed production runtime and record the observation time. Do not infer
freshness from a prior run.

- governed runtime root, runtime role, source release SHA, runtime data SHA
- source/runtime consistency and clean checkout status
- clean publisher state and the authoritative health source
- active provider order and explicit confirmation that no candidate provider is routed
- health status, capture timestamps, source ages, and staleness result
- absence of obsolete historical ownership and duplicate scheduler ownership
- absence of Top-5 publication keys before separate authorization
- public-read response validated through Worker and PWA read-only checks
- CL release, all three markets per fixture, results representation, and provenance bindings
- explicit `no_bet`, publication-disabled, and activation state evidence

Evidence that can only be collected after production activation/publication is
not fabricated by this package: an authorized publication timestamp, live
public-read confirmation, post-publication freshness, and final-result/
settlement evidence remain downstream acceptance gates.
