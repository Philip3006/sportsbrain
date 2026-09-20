# Top-5 Controlled Shadow authorization package

This package is an offline preparation and post-run reconciliation boundary
for the candidate provider identity `therundown_experimental`. It does not
create CEO authority, enable a transport, register a scheduler, issue a
Builder-2 receipt, or alter the active Football provider repertoire. The Odds
API remains the only active production Football provider.

## Inputs and exact scope

`prepare_authorization_package()` accepts a reviewed
`TheRundownNetworkConfigurationV1` with `enabled=false`. It rejects any
provider identity other than `therundown_experimental` and requires the exact
league set and order used by the package:

`EPL`, `BL1`, `LL`, `SA`, `L1`.

The package copies, rather than invents, the reviewed PR-103 values for:

- fixture, provider-event, participant, and request scope;
- adapter version and adapter source SHA;
- maximum request count, maximum datapoints, quota cost per request, and
  maximum quota cost units;
- source-age limit, sequential pacing, and `maximum_retries=0`.

The authorization template leaves CEO identity, authorization ID, controlled
shadow run ID, qualification session ID, issue time, and expiry as `null`.
Those fields must be supplied by a caller holding the separate run-specific
CEO authorization. The package cannot self-authorize a run.

The generated package is inert: network execution, receipt issuance, active
provider authority, scheduler registration, publication, activation, betting,
and monetary spend are all false.

## Provider billing and final bounded budget

For the real TheRundown transport, `datapoint_count` means the provider-billed
`X-Datapoints` value from the HTTP response header. It is not the number of
normalized bookmaker observations. `quota_cost_units` is kept as a separate
governed field, but for this provider it must equal that billed datapoint value
and is never inferred from normalized record count. `request_quota_cost_units`
is the per-request authorization ceiling; `maximum_quota_cost_units` is the
sum ceiling across the run.

Run-006 supplied the offline billing evidence `X-Datapoints=55`,
`X-Datapoints-Used=55`, `X-Datapoints-Remaining=19945`, and
`X-Datapoints-Limit=20000`. Therefore the exact five-request package is:

| field | value |
| --- | ---: |
| `maximum_request_count` | `5` |
| `maximum_datapoints` | `275` (`5 × 55`) |
| `request_quota_cost_units` | `55` |
| `maximum_quota_cost_units` | `275` |
| `minimum_interval_seconds` | `1.1` |
| `maximum_retries` | `0` |
| `maximum_source_age_seconds` | `300` |

The observed daily free-tier headroom before the bounded run is `19945`, so
the authorized ceiling leaves `19670` units. The package derives these values
from the observed provider cost and rejects a smaller headroom; it does not
substitute an arbitrary large cap. A response with missing or contradictory
billing headers fails closed before it can become `REAL_OBSERVED` evidence.
The provider's observed `X-Rate-Limit` exposes a limit but no remaining/reset
header; the transport records those fields as explicitly unavailable and never
maps the limit into a fabricated remaining value.

The network-shadow adapter delegates the actual `events[]` response to the
reviewed `src/football/odds/therundown.py` semantics. B4 only adapts the
resulting normalized observations into the shadow response and adds the
provider billing envelope; it does not maintain a second participant, market,
price, bookmaker, freshness, or digest parser.

Freshness uses the existing TheRundown snapshot contract:
`source_timestamp = response_finished_at - X-Data-Delay-Seconds`. The raw
price-level `updated_at` values remain preserved as provenance, but are not
silently substituted for the provider's declared REST snapshot timestamp.
Missing delay, event kickoff, bookmaker mapping, or price-level timestamps
fails closed.

## Post-run reconciliation

`reconcile_controlled_shadow_run()` consumes only a completed
`COMPLETED_NETWORK` PR-103 result plus the exact configuration and
caller-supplied authorization. It validates every capture and attestation,
then emits ordered qualification evidence for all five leagues. The evidence
binds provider, league, fixture, provider event, request, participants,
bookmaker, complete pre-match 1X2 prices, source/capture/request timestamps,
freshness, adapter/configuration hashes, raw/provider/normalized/cascade
digests, and quota/rate-limit/tier/delay provenance.

### Direct B1 La Liga injection

The repaired B1 La Liga artifact is injected as the keyword argument
`b1_ll_artifact` to
`reconcile_controlled_shadow_run_with_b1_ll_artifact(result, configuration,
authorization, b1_ll_artifact, now=...)`. The value is exactly the mapping
returned by `LaLigaCaptureEvidence.as_evidence_bundle()`; the repaired
Run-006 wrapper at `/private/tmp/top5-b1-laliga-final-evidence.json` is also
accepted and its `canonical_b1_evidence_bundle` is materialized without
network access. `validate_canonical_b1_ll_artifact()` is the offline B4
preflight for this wrapper: it checks real-evidence identity, the three
bookmakers, complete 1X2, timestamps, digests, and the 55-datapoint billing
reconciliation. No B1 object is imported as an authority or receipt issuer.

The Run-006 artifact retains its own historical LL authorization/run/session
binding. It is evidence provenance, not a substitute for the new
five-league CEO authorization. The eventual five-league reconciliation must
bind the newly captured LL slot to that run's exact authorization, run ID,
session ID, expiry, configuration digest, and capture attestation.

Run-006 also retains the original provider fixture key
`therundown:LL:48e87c231045c73e2318f6b4d5327405`. B4 never rewrites that
historical value. It separately derives the canonical SportsBrain key with
`make_fixture_key(league, home_team, away_team, kickoff)`, compares that
derived value to the B4 target fixture key, and fails closed on mismatch. The
reconciled artifacts expose both identities under `b1_fixture_identity`.
Historical B1 normalized-record digests remain provider-key provenance; B4's
canonical normalized digest is independently retained and is never substituted
or falsified to make the two identity domains appear identical.

B4 accepts the bundle only when all of the following match the `LL` capture in
the completed PR-103 result byte-for-byte or by canonical timestamp
comparison:

- `therundown_experimental`, canonical `LL` identity, fixture, provider event,
  provider request, home/away participant IDs, teams, and kickoff;
- the exact controlled-shadow run ID, qualification session ID, CEO
  authorization ID, expiry, adapter version, adapter source SHA, and raw
  response digest;
- source and capture timestamps within the 300-second freshness contract;
  - a bookmaker with a complete home/draw/away decimal 1X2 record and matching
    provider-record provenance; B1's historical normalized-record digests remain
    bound to its provider fixture identity while B4 retains its canonical digest;
- quota-before/after, rate-limit, and quota evidence provenance.

The B1 bundle must remain `REAL_OBSERVED`/`CAPTURED`, pre-match, candidate-only,
no-bet, unpublished, inactive, and no-spend. Its cascade digest must remain
`null`, and it must explicitly require B4 to supply the canonical cascade
evidence and capture attestation. A synthetic/replay bundle, stale bundle,
wrong request, partial 1X2, missing bookmaker/provenance, or any identity or
digest mismatch fails closed. The final reconciled payload retains the exact
B1 bundle under `artifacts.b1_ll_artifact`; it does not turn it into a receipt.

### Expected five-league artifact set

The reconciled package is ordered and complete only when it contains exactly
these five slots:

1. `EPL`
2. `BL1`
3. `LL` — the slot bound to the repaired B1 artifact above
4. `SA`
5. `L1`

Each slot contains one PR-103 canonical capture attestation, one
`CandidateProviderEligibilityV1` payload, one qualification input, and one
Builder-2 receipt input. The five receipt inputs remain
`eligible=false`/`issuer_present=false`; the reconciliation status remains
`PENDING_BUILDER2_VALIDATION`. No slot can change production authority,
publication, scheduler registration, activation, betting, or spend.

Synthetic, replay, stale, post-kickoff, incomplete, ambiguous, duplicated,
rebinding, digest-mismatched, or otherwise malformed evidence fails closed.
The resulting artifacts contain canonical capture attestations and
`CandidateProviderEligibilityV1` payloads, but their Builder-2 receipt inputs
remain explicitly `eligible=false` and `issuer_present=false`. The
qualification status is `PENDING_BUILDER2_VALIDATION`; shadow success does
not grant authority or publication.

## Later execution gate

The exact future command/package shape is exposed for a later, separately
authorized run but is not executed by this package:

```text
python -m src.football.top5_controlled_shadow_authorization_package --execute --package <authorization-package.json> --authorization <ceo-authorization.json> --b1-ll-artifact <b1-ll-evidence-bundle.json>
```

The same command with `--preflight` is the zero-network validation mode. It
requires the package, CEO authorization, and B1 artifact, but never loads the
provider credential or constructs the HTTP transport. `--execute` is the only
mode that loads `THERUNDOWN_API_KEY`; it requires the same exact authorization,
enables the otherwise-disabled configuration only for that invocation, and
still emits candidate/shadow evidence without issuing a receipt or changing
authority.

This is the single later Controlled-Shadow command shape after the repaired
B1 LL bundle is available. It is not run by this offline package. The later
operator must provide a valid, unexpired, separately approved CEO
authorization matching the package byte-for-byte on scope, hashes, budgets,
pacing, retry policy, run/session identifiers, and safety flags. The command
keeps network disabled in preflight/default operation and only passes the
reviewed live-network gate after the explicit `--execute` flag, authorization,
credential, and all executor checks succeed. This package itself performs no provider request,
does not register a scheduler, and has no receipt-issuer or authority path.

## Compatibility assumptions

- PR-95 remains the controlled-shadow lifecycle envelope.
- PR-103 remains the sequential, zero-retry, bounded network transport and
  attestation producer.
- PR-112 remains the candidate-provider eligibility boundary and keeps
  `therundown_experimental` out of active routing.
- Builder 2 still owns qualification reporting and
  `Builder2QualificationReceiptV1` issuance. A complete canonical cascade
  evidence object/report is a downstream Builder-2 input; this package does
  not fabricate one from a digest.
- The B1 LL bundle is a validated input to the exact LL slot, not a substitute
  for the PR-103 canonical capture attestation.
- No real La Liga capture or five-league Controlled Shadow is consumed during
  offline package preparation.
