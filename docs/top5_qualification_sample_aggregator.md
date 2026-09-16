# Top-5 Qualification Sample Aggregator

`top5_qualification_sample_aggregator.py` is the independent Builder-2
receipt-only aggregation boundary for future controlled-shadow observations.
It accepts only already validated `Builder2QualificationReceiptV1` receipts.
It does not read raw observations, qualification reports, cascade artifacts,
provider responses, or secrets, and it never issues or recreates a receipt.

## Deterministic report

`aggregate_builder2_qualification_samples` returns a deterministic
`Builder2QualificationSampleReportV1` with:

- input receipt count and retained valid receipt-variant count;
- raw distinct observation/fixture counts and separate eligible counts;
- raw and eligible per-league/per-provider counts;
- controlled-shadow-run, qualification-session, and CEO-authorization IDs;
- receipt/observation duplicate diagnostics and explicit identity conflicts;
- fixture/provider conflicts and unattributed fixture diagnostics;
- non-secret receipt provenance, including bound authority, event, request,
  adapter, observation, cascade, attestation, and receipt digests;
- an explicit failure taxonomy; and
- explicit freshness and observation-coverage availability.

Provenance retains one row per unique receipt ID/digest variant and an
`input_occurrence_count`. Exact duplicate rows with the same receipt ID and
semantic digest collapse to one evidence row and may count once. Divergent
content under one receipt ID is never selected arbitrarily: every variant is
retained, `DIVERGENT_RECEIPT_ID_CONFLICT` is emitted, and those variants are
ineligible evidence.

## Eligibility and conflicts

Raw counts describe retained canonical receipt variants. Eligible counts are
derived only from non-conflicting, attributed evidence. An eligible row must
not belong to a divergent receipt ID, an observation ID/digest identity
conflict, or a fixture/provider event/request conflict, and its fixture must
have a canonical Top-5 league prefix. Duplicate observations do not create
additional eligible observations or fixtures. Same observation ID with a
different digest and same digest with a different observation ID are explicit
identity conflicts and exclude the affected evidence. Multiple provider
identities for one fixture are permitted redundancy; conflicting event or
request identities for the same fixture/provider pair are excluded.

Unattributed fixtures remain visible in raw diagnostics and
`UNATTRIBUTED_LEAGUE`, but do not create league coverage or eligible fixture or
observation evidence. No coverage denominator is inferred.

## Sample policy and validation

The caller may supply the canonical `MinimumSamplePolicy`. There is no default
minimum and no production threshold is invented. `sample_sufficient` is
computed only from eligible distinct observations and eligible distinct
fixtures, and is a measurement result rather than authorization. It remains
false whenever the supplied evidence cannot meet the caller policy; ambiguous
variants can never make it true. The report is always NO-BET, unpublished, and
`production_activation_authorized=false`.

`Builder2QualificationSampleReportV1.validate()` re-derives all counts,
identity sets, duplicate/conflict diagnostics, provenance multiplicity,
failure taxonomy, eligible counts, and policy sufficiency from retained
provenance. Recomputing an unkeyed report digest cannot make tampered counters
valid. The digest provides integrity for the serialized report only; it does
not establish cryptographic authenticity of the underlying receipts.

Receipt V1 does not bind capture/source timestamps or an expected-fixture
denominator. Freshness and observation coverage therefore remain
`supported=false` with null observed counts, denominators, and rates.

## Safety and ownership

The module is stateless and side-effect-free. It has no network, provider,
credential, scheduler, launchd, publication, ledger, Cloudflare/Worker,
Research, sealed-data, betting, model-binding, deployment, or activation
capability. Builder-2 remains the receipt issuer; this module only aggregates
already issued canonical receipts. A sufficient sample report never authorizes
production.
