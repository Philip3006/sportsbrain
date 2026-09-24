# The Odds API current Top-5 fixture source

The B4 discovery bootstrap uses the existing SportsBrain The Odds API client
as its only current Football fixture source. This adapter is a thin mapping
layer; it does not implement a second HTTP client, credentials, cache,
retries, quota accounting, or provider authority.

The bounded acquisition invokes the existing production path once per ordered
Top-5 sport key with its smallest existing 1X2 request shape:

| League | The Odds API sport key |
| --- | --- |
| EPL | `soccer_epl` |
| BL1 | `soccer_germany_bundesliga` |
| LL | `soccer_spain_la_liga` |
| SA | `soccer_italy_serie_a` |
| L1 | `soccer_france_ligue_1` |

The requested market is `h2h` and the region is `eu`. The existing provider
budget guard remains authoritative and can refuse the acquisition before any
credential or network access. A source failure aborts the batch; no partial
target manifest is emitted.

Each returned event is mapped to a canonical `Fixture` using its league,
teams, and UTC `commence_time`; `fixture_key` is always derived by
`make_fixture_key()`. The resulting batch is passed to
`select_current_top5_discovery_targets()`, which enforces freshness, lead
time, seven-day bounds, exact league order, deterministic earliest selection,
and no provider event/participant IDs before TheRundown Discovery.

Source provenance records the exact production path and shape as
`the_odds_api:production:/v4/sports/{sport_key}/odds;markets=h2h;regions=eu`,
bound to the successful source-release metadata and the acquisition
observation timestamp. The Odds API remains the production Football
authority. TheRundown remains candidate-only and is used only after a
separately authorized Discovery stage.
