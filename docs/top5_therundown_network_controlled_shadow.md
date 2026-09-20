# TheRundown network-capable controlled shadow

This change is the separately reviewable network transport seam after the
offline canary and lifecycle proof in PR #95. It is disabled by default and
does not register with a scheduler, activate a provider, issue a Builder-2
receipt, change Football authority, publish, bet, deploy, or mutate ledger,
Cloudflare, Research, or sealed data.

## Authorization and execution contract

`TheRundownNetworkAuthorizationV1` is caller-supplied. This module does not
create or infer authorization. The envelope binds:

- CEO authorization ID and authorization identity;
- controlled-shadow run ID and qualification-session ID;
- provider `therundown_experimental`;
- exactly one target for each `EPL`, `BL1`, `LL`, `SA`, and `L1` league;
- exact canonical fixture key, provider event ID, participant IDs, and request
  identity for every target;
- explicit issue and expiry timestamps;
- adapter version, adapter source SHA, and configuration digest;
- maximum request count, provider-billed datapoints, per-request quota cost,
  and total quota-cost budget; and
- sequential execution, explicit pacing (minimum `1.1` seconds), zero retries,
  and immutable
  `no_bet=true`, `publication=false`, `production_activation=false`, and
  `monetary_spend_authorized=false` flags.

For the current five-request scope, the authorization package is bounded by
the real La Liga Run-006 event response: `X-Datapoints=55` per request, a
maximum of `5` requests, `request_quota_cost_units=55`, and total
`maximum_datapoints=maximum_quota_cost_units=275`. In this contract,
`datapoint_count` is exactly the provider-billed `X-Datapoints` value, not a
normalized-row or bookmaker count. A response above `55` datapoints is
rejected and requires a new authorization package.

The executor refuses an expired, mismatched, disabled, over-budget, non-
sequential, retried, or unsafe request before it can reach the transport. It
executes at most one request per league, in configured order, and stops on a
request, datapoint, or quota overrun.

## Transport boundary

`TheRundownHttpNetworkTransportV1` is the `NETWORK_CAPABLE` implementation.
It has no retry loop and requires a caller-supplied endpoint, adapter, HTTP
client, API key, and the executor's explicit `allow_live_network=True` gate.
The default is false. The only concrete HTTP client uses HTTPS, except for
loopback endpoints intended for local tests.

`TheRundownReplayTransportV1` is deterministic and test-only. It forces
`TEST_FIXTURE` and `network_execution=false`; a replay response claiming
`REAL_OBSERVED` or network execution is rejected. All acceptance tests use
in-memory transports and do not open sockets.

The generic canonical payload adapter binds `X-Datapoints` and its used,
remaining, and limit counters to the internal response object, but it does
not itself normalize the provider's raw `/events/{date}` JSON. The reviewed
candidate adapter remains the provider-specific normalization boundary. A
live run must supply that reviewed adapter and its cascade-evidence binding;
the generic seam fails closed for a raw provider payload that has not been
normalized.

The normalized response requires complete pre-match regulation 1X2 data,
bookmaker and source identity, exact participant/event/request identity,
provider source and capture/request timestamps, freshness, provider timestamp
provenance, adapter/source/raw/provider-record/normalized/cascade digests,
quota-before/after, rate-limit reset/remaining, account tier, provider delay,
and zero retries. Missing or malformed evidence fails closed.

## Downstream evidence boundary

Each successful capture contains:

- the canonical `ControlledShadowCaptureAttestation` field shape;
- the canonical real-observation field shape, including bookmaker, 1X2,
  provenance, timestamps, quota evidence, digests, and participant binding;
- a qualification-input projection; and
- a `Builder2QualificationReceiptV1`-shaped binding projection with
  `issuer_present=false`, `eligible=false`, and no qualification result.

The transport never constructs `ControlledShadowCaptureAttestation`, a
qualification report, or `Builder2QualificationReceiptV1`; it only emits
candidate evidence inputs. All capture and run outputs remain candidate-only
with immutable no-bet/publication/activation/spend safety flags.

The current B1/B2 contracts intentionally accept only the active Football
provider repertoire (`the_odds_api`). TheRundown remains outside that active
repertoire, and this change does not alter it. Consequently, a network-shaped
TheRundown output is ready for external validation but cannot currently pass
canonical provider qualification or become a Builder-2 receipt. A separately
reviewed provider-repertoire/cascade contract and canonical cascade-evidence
binding remain prerequisites for that later gate.

## Verification and next gate

The deterministic suite covers successful five-league sequential replay and
network-shaped stub output, expiry and scope failures, request/datapoint/quota
caps, stale/incomplete/mismatched observations, missing bookmaker/provenance,
transport and rate-limit failures, zero retries, and the proof that shadow
success does not imply qualification, receipt, authority, activation, or
publication.

No real TheRundown request is part of this change. After independent CEO
review, the transport itself can be used only by a new, run-specific CEO
authorization. That run still cannot bypass the active-provider and canonical
cascade prerequisites described above.

**NO MERGE.**
