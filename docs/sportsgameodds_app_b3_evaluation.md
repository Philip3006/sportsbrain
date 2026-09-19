# APP-B3 SportsGameOdds evaluation

Status: experimental, candidate-only, and not part of the authoritative
Football provider repertoire.

## Implementation boundary

`src/football/provider_cascade/sportsgameodds.py` reuses the existing
`ProviderRequest`, `RawProviderResponse`, `AdapterResult`, and
`NormalizedOddsObservation` contracts. It does not register SportsGameOdds in
`FOOTBALL_PROVIDER_REPERTOIRE`, the active merger, a scheduler, a publisher,
the ledger, or a production activation path.

The internal experiment league code is `CL`; the provider league ID is
`UEFA_CHAMPIONS_LEAGUE`. The adapter accepts `type=match`, `sportID=SOCCER`,
and the provider's regulation 3-way moneyline IDs:

- `points-home-reg-ml3way-home`
- `points-all-reg-ml3way-draw`
- `points-away-reg-ml3way-away`

The `reg` period means 90 minutes plus stoppage time. The provider's `game`
period includes extra time and a penalty shootout where applicable, so a
full-game market is rejected rather than being treated as 1X2 regulation
evidence. American prices such as `+128`/`-105` are converted to decimal;
already-decimal prices are accepted only when greater than 1.0.

## Official provider evidence

The adapter was implemented against the current official documentation:

- [SportsGameOdds API reference](https://sportsgameodds.com/docs/reference)
- [Events endpoint](https://sportsgameodds.com/docs/endpoints/getEvents)
- [Champions League coverage](https://sportsgameodds.com/leagues/uefa-champions-league-odds-api)
- [League IDs](https://sportsgameodds.com/docs/data-types/leagues)
- [Markets and odd IDs](https://sportsgameodds.com/docs/endpoints/getMarkets)
- [Regulation/full-event periods](https://sportsgameodds.com/docs/data-types/periods)
- [Account usage endpoint](https://sportsgameodds.com/docs/endpoints/getUsageData)
- [Pricing and object/request accounting](https://sportsgameodds.com/docs/faq)

The documented response shape is an object with `success` and `data`; each
event has `eventID`, `sportID`, `leagueID`, `teams`, `status`, and an `odds`
mapping. Each odds record carries `periodID`, `betTypeID`, `sideID`, and
`byBookmaker`; bookmaker rows carry `odds`, `available`, and
`lastUpdatedAt`.

## Account diagnostic

`scripts/sportsgameodds_diagnostic.py` performs at most five requests, in this
order, and emits only a redacted summary:

1. `/account/usage` before the diagnostic
2. `/leagues?leagueID=UEFA_CHAMPIONS_LEAGUE`
3. `/markets?leagueID=UEFA_CHAMPIONS_LEAGUE&sportID=SOCCER&isSupported=true`
4. `/events` filtered to upcoming, non-live UCL matches and the three
   regulation 1X2 market IDs
5. `/account/usage` after the diagnostic

The current checkout had no `SPORTSGAMEODDS_API_KEY` in its local
environment during this review. Consequently, the bounded real diagnostic
made exactly **0 real requests** and returned `REAL_TEST_READY`. No key was
printed, persisted, or committed. The human-run command is:

```text
SPORTSGAMEODDS_API_KEY='…' python3 scripts/sportsgameodds_diagnostic.py --output /tmp/sportsgameodds-diagnostic.json
```

The key must be injected by the operator's secret manager or shell and must
not be placed in source, `.env` committed files, command history, or a report.

## Usage model and projection

SportsGameOdds documents billing by returned top-level event object, while a
request is counted once regardless of the number of markets/bookmakers in the
response. The account's actual tier, limits, current usage, and notices remain
unknown until the bounded diagnostic runs.

Let `E` be the number of event objects returned per poll, `P` polls per day,
and `D` days in the billing month. The projection is:

`objects/month = E × P × D`

For a 30-day month and once-hourly polling, this is `720 × E` objects. A
2,500-object allowance therefore supports only about 3 returned events per
hour on average. A full 18-matchday response once per hour would be about
12,960 objects before any Top-5 coverage. Dense near-kickoff polling can be
economical only when requests are narrowed to the exact scheduled event IDs;
the exact schedule and returned object count must be measured on the account.

For Top-5 domestic leagues plus Champions League, substitute the combined
returned-event count for `E`; continuous broad-feed shadow coverage is not
realistically compatible with a 2,500-object monthly allowance. A sparse,
fixture-ID-scoped qualification sample may be feasible, subject to the
account's observed limits and response notices.

The current OpenAPI surface exposes finalized event/results fields and
open/close odds flags, but no separate historical-odds endpoint. Historical
availability, retention, and plan restrictions therefore remain an account
qualification question rather than an assumption.

## Fail-closed behavior

The adapter rejects or classifies authentication and HTTP failures, malformed
responses, wrong sport/league/event type, cancelled or non-prematch events,
fixture/team/kickoff mismatches, missing regulation 1X2 or draw outcomes,
invalid American/decimal prices, missing or stale `lastUpdatedAt`, duplicate
bookmaker rows, and full-game-only markets. A valid result remains
`candidate_only=True`, carries source and capture timestamps, retains provider
and team IDs in metadata, and includes bookmaker coverage without exposing a
credential.

## Qualification blockers and next gate

Open evidence gaps are the actual account's UCL event availability, bookmaker
coverage, regulation 1X2 availability, freshness distribution, object/request
accounting, plan restrictions, and historical-data behavior. These are not
filled from marketing claims.

The next gate is a bounded real diagnostic followed by an injected UCL shadow
sample using the exact provider event IDs. Only after that evidence exists may
Builder 2 assess provider quality and source-time coverage. SportsGameOdds
must remain experimental and cannot become a live authority, publish signals,
activate betting, mutate Cloudflare, or touch the ledger as part of this work.
