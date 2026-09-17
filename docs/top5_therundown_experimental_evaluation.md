# TheRundown experimental Football provider evaluation

Status: candidate-only prototype, 17 September 2026.

This work is an independent Builder-2 evaluation of TheRundown V2. It is not
registered in the active provider cascade, cannot become provider authority,
and does not publish, qualify, bet, activate, mutate runtime state, or call a
provider during deterministic tests. Real requests are available only through
the bounded diagnostic script when `THERUNDOWN_API_KEY` is already present in
the process environment.

## Contract mapping

| TheRundown V2 | SportsBrain candidate contract |
| --- | --- |
| `sport_id=16` / `UEFA.CHAMP` | Champions League-only fixture allowlist |
| `event_id` | `provider_fixture_id` and event provenance; `event_uuid` is retained only as metadata |
| `event_date` | kickoff identity, strict timezone parsing, configured tolerance |
| `teams` | home/away identity; documented V2 `[away, home]` order is used only when explicit markers are absent |
| market `1` / `period_id=0` | pre-match 1X2 moneyline |
| three participants | home, draw, away; all three are mandatory |
| American `price` | finite decimal odds greater than 1, with off-board sentinel `0.0001` rejected |
| `updated_at` | source timestamp and stale-age gate |
| affiliate price map | one complete candidate observation per configured sportsbook |
| `X-Datapoints*` and rate headers | redacted `QuotaSnapshot` |

The adapter accepts only complete main-line prices. It rejects ambiguous team
markers, duplicate events or outcome prices, wrong competitions, fixture
mismatches, missing markets/books/outcomes, malformed timestamps, future source
timestamps, stale books, and provider error responses. Every accepted result
remains `candidate_only=True` and uses the existing
`NormalizedOddsObservation` / `AdapterResult` contracts.

The official documentation describes Champions League as sport ID 16 and
moneyline as market ID 1; soccer moneyline is represented by three participants
for home/draw/away: [sports](https://docs.therundown.io/reference/sports),
[markets](https://docs.therundown.io/reference/markets), and
[V2 data model](https://docs.therundown.io/reference/data-model).

## Request and credential boundary

The request is the documented dated-event route:

```text
GET /api/v2/sports/16/events/YYYY-MM-DD
market_ids=1&main_line=true&hide_closed=true&hide_no_markets=true
```

The API key is read only from `THERUNDOWN_API_KEY` and sent as
`X-TheRundown-Key`. Request metadata redacts that header. The adapter is not
added to `FOOTBALL_PROVIDER_REPERTOIRE` or the active cascade registry.

The event endpoint and filters are documented in the official
[V2 events reference](https://docs.therundown.io/api-reference/generated/v2-events/get-events-with-markets-for-a-sport-and-date).
The available-date and affiliate discovery endpoints are described in the
[available dates reference](https://docs.therundown.io/api-reference/generated/v2-sports/get-available-dates-for-a-sport)
and [affiliate reference](https://docs.therundown.io/api-reference/generated/v2-reference/list-all-sportsbooksaffiliates).

## Quota, freshness, and economics

The adapter preserves TheRundown's datapoint and rate-limit headers without
inventing an account tier. The official free tier is documented as 20,000
datapoints per day and 200,000 per month, with three sportsbooks, pre-match
data, and a five-minute delay; paid tiers change request rate, history,
freshness, and bookmaker access. See the current
[API pricing](https://therundown.io/pricing/api),
[quickstart](https://therundown.io/docs/quickstart), and
[rate-limit documentation](https://docs.therundown.io/rate-limits).

No live account entitlement or current bookmaker breadth is claimed here. A
later operator diagnostic must retain the response headers as evidence. Usage
must be calculated from the observed `X-Datapoints` value:

```text
monthly datapoints = fixtures requested × polling cycles × observed datapoints per event response
```

For orientation only, if one response happened to return one event plus three
outcomes from three books, a rough event/score component plus nine price rows
would be 11 datapoints. That is illustrative, not a quota denominator: the
actual response header is authoritative and event count, book count, filters,
and returned rows vary.

Efficiency controls are therefore `market_ids=1`, `main_line=true`,
`hide_closed=true`, `hide_no_markets=true`, a bounded affiliate list, one
request per fixture date, and no retries in the diagnostic. HTTP 401/403/404,
429, malformed data, and entitlement failures remain visible as safe adapter
states; 429 distinguishes datapoint exhaustion from burst throttling when the
header permits it. The official
[error reference](https://docs.therundown.io/errors) defines these response
classes and the `0.0001` off-board sentinel.

## Evaluation scorecard

| Area | Result |
| --- | --- |
| API schema mapping | GOOD — documented fields map deterministically to existing contracts |
| Champions League coverage | READY TO VERIFY — no live coverage claim without an authorized diagnostic |
| Pre-match 1X2 | GOOD — market 1 and three soccer participants are handled |
| Bookmaker breadth | VERIFY — depends on account tier, league, and current affiliate roster |
| Freshness | VERIFY — tier/data-delay headers are retained; stale input is rejected |
| History | VERIFY — entitlement and window are account-dependent |
| Schema quality | GOOD — malformed and ambiguous cases fail closed |
| Integration complexity | MODERATE — identity and affiliate mapping require evidence |
| Reliability | FAIL-CLOSED — candidate-only and never registered as authority |

## Bounded real diagnostic

Offline tests use only the saved synthetic fixture and injected transport. If a
key is not already safely exported, the diagnostic returns `REAL_TEST_READY`
and performs zero requests. With an explicitly exported key, it performs at
most five sequential requests, never retries, and prints only paths, status,
response sizes, safe quota headers, and aggregate counts:

```text
python3 scripts/therundown_diagnostic.py --max-requests 5
```

The diagnostic checks sports, affiliates, available UCL dates, one filtered
UCL event response, and the UCL sport resource. It does not issue receipts,
select a provider authority, or make the candidate eligible for production.

## Evidence needed for any later decision

Before any CEO decision about real controlled-shadow use, retain a redacted
diagnostic record with: request date and path, response status, exact quota
headers, account tier/access flags, UCL event count, event identity, returned
affiliate IDs, market/participant completeness, source timestamps, delay, and
failure observations. Compare those observations with the existing provider
contracts and with any separately authorized API-Football or The Odds API
evidence. Do not infer quality, cost, or availability from this synthetic
fixture.
