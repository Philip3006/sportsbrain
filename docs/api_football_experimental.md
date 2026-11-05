# APP-B4 API-Football experimental Football source

This is an evaluation-only adapter for UEFA Champions League pre-match 1X2
odds. It is not part of the active SportsBrain Football provider authority.
The active provider order remains `("the_odds_api",)`, and no live signal,
Top-5 readiness, publication, betting, or production path imports this module.

## Contract boundary

`src/football/experimental/api_football.py` reuses the canonical
`Fixture` and `NormalizedOddsObservation` contracts. The adapter adds an
immutable `ApiFootballObservation` origin boundary:

| Origin | Evidence kind | Synthetic | Allowed use |
| --- | --- | --- | --- |
| injected test fixture | `INJECTED_FIXTURE` | yes | deterministic contract tests only |
| explicit HTTP response | `REAL_NETWORK_CAPTURE` | no | experimental evaluation, still candidate-only |

An injected observation cannot be relabeled as a real capture: the canonical
metadata, provenance URI, and wrapper evidence kind must agree. The adapter
does not import or issue Builder-2 qualification receipts and has no prediction,
publication, betting, ledger, scheduler, Cloudflare, or activation API.

Successful observations are always `candidate_only=True`. API-Football is not
registered in `FOOTBALL_PROVIDER_REPERTOIRE`, `ProviderCascadeConfig`, active
readiness, or routing.

## API mapping

The adapter uses the API-Football v3 base URL
`https://v3.football.api-sports.io` and these bounded endpoints:

| Purpose | Endpoint and parameters | Request shape |
| --- | --- | --- |
| Champions League fixture discovery | `GET /fixtures?league=2&season=YYYY&date=YYYY-MM-DD` | `response[]` entries containing `fixture`, `league`, and `teams` |
| one fixture's odds | `GET /odds?fixture=ID&bet=1&page=1` | `response[]` entries containing `fixture`, `league`, and `bookmakers[]` |

The competition identity is the pair `league.id=2` and
`league.name="UEFA Champions League"`. Fixture identity uses
`fixture.id`, exact kickoff `fixture.date`, and normalized home/away team
names. The odds identity uses each bookmaker's `id`/`name`, bet `id=1` or
`name="Match Winner"`, and values `Home`, `Draw`, and `Away` (actual team names
are also accepted).

The source timestamp is taken only from an explicit provider `update` or
`last_update` field at the bookmaker, bet, entry, or response level. Kickoff
time and local capture time are never substituted for a source timestamp when
the default policy requires one. Missing, future, or stale source time rejects
the observation. A caller may explicitly choose capture-only timing for a
non-qualification experiment; the result remains candidate-only.

API-Football documents `/odds` as a pre-match bookmaker endpoint and identifies
`update` as the snapshot-production field. The provider also documents daily
and per-minute rate-limit headers; the adapter retains those values in the
canonical `QuotaSnapshot` without printing credentials or raw response bodies.

## Failure policy

The adapter fails closed for missing credentials, authentication errors, HTTP
errors, timeouts, malformed JSON/objects, quota exhaustion, rate limiting,
wrong competition, missing fixture, fixture mismatch, missing bookmaker,
missing Match Winner, invalid decimal odds, stale data, invalid source time,
and duplicate snapshots. It performs no retries and never selects an
alternate provider.

## Bounded diagnostic

The diagnostic is `scripts/api_football_diagnostic.py`. It loads
`API_FOOTBALL_KEY` from the process environment or the local ignored `.env`,
never prints it, and makes at most five requests:

1. one date-scoped `/fixtures` request;
2. at most four fixture-scoped `/odds` requests.

If no credential is present it exits with `REAL_TEST_READY` and prints the
safe command shape:

```text
API_FOOTBALL_KEY=<secret> python3 scripts/api_football_diagnostic.py
```

The report includes request count, fixture availability, bookmaker names,
Match Winner coverage, provider timestamps/freshness, quota headers, and the
per-fixture request estimate. It does not write a cache or runtime state.

## Provider references

- [API-Football getting started guide](https://www.api-football.com/news/post/how-to-get-started-with-api-football)
- [API-Football rate-limit guidance](https://www.api-football.com/news/post/how-ratelimit-works)
