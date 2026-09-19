# Champions League provider-cascade shadow readiness

This is an offline/replay-only readiness seam. It does not add UCL to the
existing Top-5 router, authorize a provider, spend quota, publish a signal, or
write production/financial state.

## Compatibility matrix

| Source | Current status | UCL 1X2 readiness | Main gap or boundary |
| --- | --- | --- | --- |
| `the_odds_api` | implemented candidate | exact identity, source timestamps, regulation 1X2, raw multi-bookmaker payload, quota evidence | active cascade still selects one normalized bookmaker and rejects UCL at the Top-5 router boundary |
| `api_football` | implemented, not UCL-mapped | generic odds parser exists; capture-only timing | `league_ids` has no Champions League entry and source-time evidence is not guaranteed |
| `odds_api_io` | decommissioned | parser exists | prohibited by the provider-cascade contract |
| `betfair_delayed` | decommissioned | delayed exchange market | not an approved pre-match bookmaker source |
| `football_data` | historical-only | closing-style 1X2 benchmark only | no signal-time capture or bookmaker identity |
| `espn` | result-only | none | no odds path |
| `cache` | cache-only | inherited data only | not an independent source or authority |
| `oddsportal` | disabled legacy | aggregate only | fetch is permanently disabled and no cascade adapter is registered |
| `websearch` | disabled legacy | none | not a football odds authority; fetch is disabled |

The code representation is `CL_SOURCE_MATRIX` in
`src/football/provider_cascade/champions_league_shadow.py`.

## Observation contract

Each accepted replay row must retain the exact schedule `fixture_key`, optional
source `provider_event_id`, competition `UCL`, both participants and kickoff,
the `football:pre_match:regulation_1x2` market, all three decimal prices, one
`bookmaker_identity`, source and capture timestamps, source provenance, and
quota metadata. One row is retained per bookmaker; bookmakers are never
collapsed into a consensus row by the replay seam.

Rows are rejected when the event is stale or in-play, participants or fixture
identity do not match, timestamps are missing/invalid, the market is partial or
wrong, provenance is absent, or quota metadata is absent/invalid. Duplicate
fixture definitions fail the replay; duplicate observations are recorded as a
rejection rather than silently overwriting the first row.

`replay_cl_shadow` sorts fixtures, accepted observations, and rejections before
building its digest. Replaying the same evidence in a different input order
therefore produces the same payload and digest, with `network_called: false`.
