# Top-5 production verification checklist and read-only harness

This checklist is for a future CEO-authorized **CONTROLLED ACTIVATION RUN**.
It is not an activation command and does not authorize publication, betting,
scheduling, or ledger writes.

The deterministic verifier is `scripts/top5_production_verification.py`.  It
only consumes captured JSON evidence; it does not contact a provider, inspect
credentials, write runtime state, or enable a scheduler/publisher.

Record the following exact values before verification:

- production source SHA;
- frozen Research SHA;
- model artifact hash and candidate identity;
- league and fixture identity;
- signal-time configuration;
- provider fixture, odds, and result authority;
- rollback pointer and configuration snapshot.

The baseline must additionally record worker and PWA health, football
scheduler state, active provider order, launchd and GitHub workflow
expectations, runtime and financial-writer state, publication/betting state,
current Football health artifacts, fixture identities, and request/quota/spend
accounting.  The baseline fails closed unless The Odds API is in the active
order and TheRundown is absent from it.

## Checklist

- [ ] Source SHA matches the approved configuration snapshot.
- [ ] Research SHA matches the approved frozen research record.
- [ ] Model artifact hash and candidate identity match the approval.
- [ ] The selected league and fixture universe are correct.
- [ ] The event-relative signal-time contract is the approved contract.
- [ ] Provider authority and request routes match the approved source matrix.
- [ ] No closing odds entered signal-time inference.
- [ ] PWA data is available, backward-compatible, and provenance-complete.
- [ ] Publisher health is healthy and the strict Top-5 namespace is intact.
- [ ] The financial ledger is untouched and settlement compatibility is
      verified without creating a bet.
- [ ] Rollback is ready and restores disabled/no-bet/unpublished state.
- [ ] Health output records coverage, freshness, latency, retries, fallback,
      stale rate, duplicate suppression, quota/cost, and activation state.

## Pass/fail rule

Every checklist item must pass. A missing or mismatched value fails closed and
invokes the rollback procedure. Passing this checklist is evidence for the
production-verified rollout stage; it is not permission to expand league
scope, change the model, change provider authority, or enable publication.

The harness returns exactly one status:

- `PRODUCTION_VERIFIED` — every routing, data, runtime, safety, and resource
  check passed;
- `ROLLBACK_REQUIRED` — a hard rollback trigger was observed;
- `VERIFICATION_BLOCKED` — required evidence is missing or malformed and no
  safe production conclusion can be made.

Hard rollback triggers include routing or activation-identity mismatch,
identity/provenance mismatch, stale or post-kickoff data, incomplete
regulation 1X2, unexpected scheduler activity, health failure, uncontrolled
retries, unexpected quota/spend, publication leakage, financial/ledger or
sealed-partition mutation, and unapproved signal-time behavior.

## Exact future checks

These commands are read-only and expect operator-captured JSON files.  They do
not capture evidence themselves and therefore cannot silently make a provider
request.

```text
python3 scripts/top5_production_verification.py baseline \
  --input evidence/top5-pre-activation.json

python3 scripts/top5_production_verification.py verify \
  --input evidence/top5-post-activation.json

python3 scripts/top5_production_verification.py rollback \
  --input evidence/top5-verification-report.json

python3 scripts/top5_production_verification.py publication-preflight \
  --input evidence/top5-publication-preflight.json
```

The post-activation evidence must contain the exact activation identity and
digest, selected/observed routing, pre-match signal-time observations with
complete home/draw/away odds, worker/PWA/scheduler/writer health, retry and
publication flags, mutation counters, and bounded request/quota/spend
accounting.  A successful report is still not publication authorization.

## Publication boundary

The current shadow publication switch is deliberately disabled in
`src/football/top5_publisher.py`: `Top5PublisherContract.publish()` always
raises `ProductionContractError("Top-5 publication is disabled")`, and the
PWA/readiness contracts require `publication_enabled=False`.  The file also
contains a separate future-only controlled path,
`Top5PublisherContract.publish_controlled()` →
`InMemoryTop5PublicationStore.publish()`.  That path requires a controlled
publication payload, `Top5PublicationAuthorization`, exact active-activation
bindings, and the capability/attestation contract; this verifier never calls
it and does not issue any of those capabilities.

The general runtime publisher in `scripts/publish_runtime_artifacts.sh` is a
separate allowlisted sibling checkout for existing non-Top-5 artifacts; its
allowlist contains no Top-5 publication path and must not be reused as a Top-5
activation switch.

`publication-preflight` only reports eligibility.  Eligibility requires
`PRODUCTION_VERIFIED`, an exact activation identity/digest match, no active
rollback trigger, and a separate nonblank CEO publication authorization.  A
later publication executor must additionally pass the controlled publisher's
authorization, active-activation binding, and capability/attestation checks.
This verifier never changes `publication_enabled`, betting, scheduler, ledger,
capabilities, or runtime state.

## Safe decision sequence

1. Capture and validate the pre-activation baseline.
2. After the separately authorized bounded activation, capture immediate and
   short-follow-up evidence with no extra scope.
3. Run `verify`; retain the report and its deterministic report ID.
4. If the result is `ROLLBACK_REQUIRED`, leave The Odds API active, disable
   the candidate, preserve evidence, and do not publish.  If it is
   `VERIFICATION_BLOCKED`, collect the missing evidence without widening scope.
5. Only after `PRODUCTION_VERIFIED` may the separate CEO publication gate be
   evaluated.  The publication implementation remains disabled until a later,
   independent authorization and implementation change.

## Evidence capture

Attach the preflight payload, post-run verification payload, health snapshot,
provenance records, provider-validation evidence, shadow-performance report,
and CEO decision reference. Preserve benchmark-only closing and CLV-style
comparisons separately from signal-time inference records.
