# Top-5 Qualification Sample Aggregator

`top5_qualification_sample_aggregator.py` is the independent Builder-2
receipt-only aggregation boundary for future controlled-shadow observations.
It accepts only already validated `Builder2QualificationReceiptV1` receipts.
It does not read a raw observation, qualification report, cascade artifact, or
provider response, and it never issues or recreates a receipt.

## Output

`aggregate_builder2_qualification_samples` returns a deterministic
`Builder2QualificationSampleReportV1` containing:

- total input and unique valid receipt counts;
- distinct observation and fixture counts;
- per-league counts derived only from canonical fixture-key prefixes and
  per-provider counts;
- controlled-shadow-run, qualification-session, and CEO-authorization IDs;
- duplicate receipt IDs, duplicate observation IDs/digests, and
  fixture/provider identity conflicts;
- non-secret receipt provenance, including all bound authority, event,
  request, adapter, observation, cascade, and attestation digests;
- an explicit failure taxonomy for duplicate/conflicting/unattributed input;
- explicit freshness and coverage availability.

Receipt V1 does not bind capture/source timestamps or an expected-fixture
denominator. The report therefore returns `supported=false` for freshness and
coverage and leaves their observed count, denominator, and rate null. It never
invents a denominator or treats a fixture count as a coverage rate.

Exact duplicate receipt IDs are collapsed for evidence counts. Duplicate
observations and fixture/provider conflicts remain visible and do not create
additional unique observation evidence. Multiple provider identities for one
fixture are permitted redundancy and are not themselves a conflict; a conflict
is multiple provider event/request identities for the same fixture/provider
pair.

## Sample policy

The caller may supply the canonical `MinimumSamplePolicy`. There is no default
minimum and no production threshold is invented. When supplied, the report
evaluates the policy against de-duplicated observation and fixture counts only.
`sample_sufficient=true` is a measurement result, never a production
authorization. The report is always NO-BET, unpublished, and
`production_activation_authorized=false`.

## Safety and ownership

The module has no network, provider, credential, scheduler, launchd,
publication, ledger, Cloudflare/Worker, Research, sealed-data, betting, model
binding, or activation capability. It is stateless and side-effect-free.
Builder-2 remains the receipt issuer; this module is only an aggregator.
