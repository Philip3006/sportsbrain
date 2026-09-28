# Shared iSports Football provider

The `src.football.odds.isports` module provides one catalog, schedule, and
prematch 1X2 normalization path for EPL, BL1, LL, SA, L1, and UCL. It resolves
competition IDs from the authenticated provider catalog; IDs are not inferred
from league names or hard-coded guesses. Provider match IDs are preserved as
the external fixture identity. Domestic canonical fixture keys use the
existing SportsBrain fixture-key function; UCL keys use `UCL:<matchId>`.

`ISportsClient` is explicitly network-capable but is not registered in the
active provider router, production merger, scheduler, or activation path. Its
`normalized_observation(...)` output uses the provider-neutral
`NormalizedOddsObservation` contract and is marked `candidate_only=True`.
The Odds API remains the active production Football provider. No production
authority, publication, activation, betting, or ledger capability is granted
by an iSports observation.

## Calls and market selection

The endpoint allowlist is the league catalog, schedule/basic, odds/main, and
odds/european/all. The capability diagnostic performs at most one catalog
call, one schedule call per competition, and one bulk call to each odds
endpoint. It uses no retry, polling, or per-fixture requests. The client
enforces the published endpoint intervals: catalog 1,800 seconds, schedule
60 seconds, Main Odds 10 seconds, and European Odds 60 seconds.

The Main and European endpoints both provide current 1X2 prices as Hong Kong
odds; conversion to decimal uses `HK + 1`. Main and European bookmaker IDs
remain separate namespaces. Valid European bookmaker rows are preferred; Main
Odds is used only when no fresh, valid European rows exist in the already
captured bulk results. The existing Shin margin-removal function is applied
per bookmaker, then normalized coordinate medians form the deterministic
provider-neutral market snapshot. Only current prematch prices enter a
signal-time snapshot; opening prices remain provenance and closing prices are
not prediction inputs. Snapshot age is capped at 900 seconds.

Safe request evidence contains the provider, operation, endpoint path, safe
non-secret parameters, request-shape digest, HTTP status/timestamps, safe
rate/quota headers, and a canonical digest of the JSON response payload. The
`ISPORTS_API_KEY` is sent only in the transient authenticated request and is
not serialized into evidence.

## Current product boundary

This module establishes provider capability and typed fixture/market output;
it does not claim an approved Top-5 or CL production provider migration.
The canonical B4 evidence contract is provider-neutral (PR #190). This adapter
normalizes iSports market observations but does not itself manufacture quota
proof, Discovery, or Controlled Shadow evidence. The B1/final-acceptance
consumer still needs separate provider-neutral work owned by Builder 1; this
PR does not implement that consumer. The Champions League model/runtime remains
disabled pending its separately approved model-input contract.

## Strict target binding and parser failure policy

Main Odds parsing is bound to the exact validated `matchId` set sent by the
client. Every returned container and odds-row ID must belong to that set; when
both an outer match ID and an inner `europeOdds` row ID exist, they must agree.
European Odds requests take the exact scheduled fixture mapping, derive the
outbound IDs from it, and validate returned native match ID, league, home and
away participants, and kickoff against those fixtures. Neither network path
can omit its target identity binding.

Malformed or conflicting evidence for a requested target fails closed, even
when another bookmaker row in that same response is valid. Duplicate target
containers and duplicate bookmaker identities are rejected. A well-formed
target with an empty/missing market list is instead represented as missing
market coverage, not parser corruption. Such a target cannot produce a market
snapshot without fresh complete 1X2 quotes.

The documented Main Odds `europeOdds` CSV row is parsed in its exact 11-field
order: `matchId`, `companyId`, initial home/draw/away, instant home/draw/away,
`changeTime`, `close`, and `oddsType`. The parser also supports the documented
mapping/object form when supplied by an injected transport. European Odds
`oddsDetail` CSV uses exactly eight fields: company ID/name, initial
home/draw/away, and instant home/draw/away; its update time comes from the
containing odds entry. Unexpected extra or missing CSV fields fail closed.

The previous HTTP-200 parser failure cannot be attributed conclusively because
only the digest was retained. Offline documented-shape regressions now cover
the parser contract; a later bounded real capability run is still required.

## Authorized capability sample (2026-09-27 UTC)

One bounded live sample resolved these IDs from the authenticated league
catalog; they were not guessed:

| League | Catalog ID | Schedule request | Selected sample match ID |
| --- | ---: | --- | --- |
| EPL | `1639` | HTTP 200 | `399830032` |
| BL1 | `188` | HTTP 200 | `232881035` |
| LL | `1134` | HTTP 200 | `271731032` |
| SA | `1437` | HTTP 200 | `380839929` |
| L1 | `1112` | HTTP 200 | `345359927` |
| UCL | `13014` | HTTP 200 | `257137036` |

The catalog request and all six schedule requests returned HTTP 200. At that
time, one eligible future fixture per league was selected for the single bulk
odds request. The Main Odds request also returned HTTP 200, but the live body
did not pass the then-current parser. Only its digest was retained
(`6d23d58891142dd1746f25fcc6f37d1e81c1922896b4bcee50fcaee6ffe9e1dc`), so the
exact body shape and total schedule counts cannot be recovered from this run.
In keeping with the one-shot/no-retry boundary, the European Odds request was
not made. No real bookmaker coverage is therefore claimed. The previous
HTTP-200 parser failure cannot be attributed conclusively because only the
digest was retained. Offline documented-shape regressions now cover the parser
contract; a later bounded real capability run is still required. Any later
request requires fresh authorization; this PR performs no live capability
request.
