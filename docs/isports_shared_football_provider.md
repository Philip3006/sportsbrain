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
Current Top-5 final acceptance and the existing B4 real-evidence dossier are
still bound to TheRundown-specific quota-proof, Discovery, and Controlled
Shadow schemas. The CL model/runtime contract also currently requires the
The Odds API as its model-input source and remains disabled. These product
acceptance seams must be changed in their own reviewed integration before
iSports output can satisfy those final gates. This adapter does not imitate
legacy provider schemas to bypass them.

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
did not pass the implemented documented-response parser. Only its digest was
retained (`6d23d58891142dd1746f25fcc6f37d1e81c1922896b4bcee50fcaee6ffe9e1dc`),
so the exact body shape and total schedule counts cannot be recovered from this
run. In keeping with the one-shot/no-retry boundary, the European Odds request
was not made. No real bookmaker coverage is therefore claimed. A future
provider request requires a fresh authorization and should first capture only
safe response-shape metadata (not raw payload or credentials) if parser
diagnosis is still needed.
