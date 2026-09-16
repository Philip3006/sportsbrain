# Builder-2 controlled-shadow qualification intake

`top5_b2_qualify` is the operational, no-network entry point for one already
captured controlled-shadow observation.  It consumes a
`Builder2QualificationIntakeManifestV1` containing canonical
`RealProviderObservation`, `ProviderQualificationSession`, `CEOAuthorization`,
`CascadeEvidence`, `ControlledShadowCaptureAttestation`, and caller-supplied
timing/readiness context.

The command never executes a provider, creates an attestation, chooses a
provider or signal time, binds a model, publishes, bets, mutates the ledger,
or activates production.  The external controlled-shadow runner must have
completed the run and obtained the explicit CEO authorization before this
intake is called.  A successful receipt is evidence of validation only.

## Commands

```text
top5-b2-qualify validate <manifest.json>
top5-b2-qualify run <manifest.json> --evidence-dir /absolute/external/evidence
top5-b2-qualify inspect <artifact.json>
top5-b2-qualify aggregate /absolute/external/receipts
```

`run` may receive an existing receipt collection and a caller-owned sample
policy:

```text
top5-b2-qualify run <manifest.json> \
  --evidence-dir /absolute/external/evidence \
  --receipt-dir /absolute/external/receipts \
  --minimum-real-observations 10 \
  --minimum-distinct-fixtures 10
```

The two sample-policy values must be supplied together.  There is no default
or production minimum threshold.  `aggregate` has the same optional policy
flags and accepts only canonical `Builder2QualificationReceiptV1` JSON
artifacts.

Every command reports these explicit safety banners:

```text
NO NETWORK EXECUTION
NO BET
NO PUBLICATION
NO PRODUCTION ACTIVATION
```

## Manifest and output boundary

The manifest is deterministic and binds the intake identity, controlled-shadow
run, qualification session, CEO authorization, fixture, provider event and
request, observation, normalized record, cascade and capture-attestation
digests, adapter version/source SHA, caller timing/readiness references, and
non-secret source artifact paths/digests.  Unknown manifest or top-level input
fields are rejected.

Successful `run` output is installed as one atomic external directory:

```text
<evidence-dir>/<intake-id>/
  manifest.json
  qualification_report.json
  receipt.json
  result.json
  sample_report.json        # only when aggregation was requested
```

All validation happens before publication.  A failed validation or failed
staged write leaves the last-good intake directory unchanged.  Repeating the
same intake is idempotent; a conflicting manifest or receipt for the same
intake identity is rejected rather than overwritten.
