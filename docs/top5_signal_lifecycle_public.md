# Top-5 public signal lifecycle contract

The public football read model optionally carries one `lifecycle` object on an
individual outcome signal. The object is additive: records without it retain
the legacy signal shape and remain valid. Each `lifecycle_id` identifies one
fixture/model/market signal, not all three outcomes of an entire 1X2 prediction.
For a three-outcome prediction artifact, the publisher adapter accepts
`lifecycle_by_market` keyed by the predicted market (`home`, `draw`, `away`)
and maps each value to its corresponding public signal. A single-outcome
artifact may carry `lifecycle` directly.

The schema version is `top5-lifecycle-public-v1`. Required fields are:

- `schema_version`, `lifecycle_id`, `initial_record_id`, `lifecycle_version`;
- `lifecycle_stage` (`INITIAL`, `REFINED`, or `WITHDRAWN`);
- timezone-bearing `initial_generated_at` and `current_generated_at`;
- `fixture_identity`, `model_identity`, and non-empty `provenance_binding`.

`updated_at` is accepted as an input alias for `current_generated_at` and is
emitted only in canonical form. `INITIAL` is version 1 and its two timestamps
identify the same capture. `REFINED` and `WITHDRAWN` are version 2 or later;
both retain the same lifecycle and initial-record identities, fixture, model,
market, initial timestamp/history, and provenance binding. A withdrawn record
uses classification `WITHDRAWN` and cannot have `signal_status: ACTIVE`.

Optional probability values are fractions in `[0, 1]`; optional edge values
are percentage points. Deltas are emitted only when supplied and must equal
the difference of their supplied endpoints. Missing history remains missing.
The PWA uses the enclosing public signal's current prediction as the primary
number and checks any lifecycle current value against it. Odds-snapshot time
continues to come from the signal's `odds_ts`; kickoff remains the fixture's
`kickoff`. These clocks are not interchangeable.

Only `source_sha`, `research_sha`, `model_artifact_hash`, `evidence_digest`,
and `snapshot_id` may appear in `provenance_binding`, and each must match the
enclosing signal's public provenance. Across a version chain, source,
Research, and model-artifact bindings remain identical; `snapshot_id` is
version-specific and must match that version's enclosing signal. Lifecycle data cannot carry provider,
publication, activation, account, ledger, or betting authority. Unknown nested
fields are discarded by the Python public serializer. Existing controlled
release authorization, provider, no-bet, freshness, provenance, and five-league
checks remain independently required.

If multiple versions of one lifecycle arrive together, the serializer and
browser accept them only as one consistent increasing chain beginning with
version-1 `INITIAL`, then retain the latest version. The browser also rejects
a lower version or changed lifecycle ID for a previously observed
`initial_record_id`; this local monotonicity check is additional to, and never
substitutes for, the governed public-release guard.

## Core-to-public projection

`src.football.top5_signal_lifecycle_public_adapter.project_top5_signal_lifecycles`
accepts only validated `Top5SignalLifecycle` domain objects. It requires one
object for every canonical prediction outcome (`home`, `draw`, `away` as
present), with exact fixture, league, candidate, model, provider, probability,
timestamp, current snapshot, and source/Research/model-artifact bindings. It
uses the INITIAL version digest for the stable public `initial_record_id`,
retains the selected outcome's initial/current probability history, and only
projects market/edge history already present in both core versions. The
existing `project_top5_lifecycle` remains the public schema validator.

`ControlledTop5PublicationPayload` records may provide the typed objects under
`top5_signal_lifecycles`; the publisher projects these immediately before the
existing prediction serializer. Supplying both typed objects and a prebuilt
`lifecycle_by_market` is rejected. Existing prebuilt-map callers retain their
prior behavior and are not made more permissive.
