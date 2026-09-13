# Top-5 production verification checklist

This checklist is for a future CEO-authorized **CONTROLLED ACTIVATION RUN**.
It is not an activation command and does not authorize publication, betting,
scheduling, or ledger writes.

Record the following exact values before verification:

- production source SHA;
- frozen Research SHA;
- model artifact hash and candidate identity;
- league and fixture identity;
- signal-time configuration;
- provider fixture, odds, and result authority;
- rollback pointer and configuration snapshot.

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

## Evidence capture

Attach the preflight payload, post-run verification payload, health snapshot,
provenance records, provider-validation evidence, shadow-performance report,
and CEO decision reference. Preserve benchmark-only closing and CLV-style
comparisons separately from signal-time inference records.
