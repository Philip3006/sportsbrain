# Top-5 Builder-2 Qualification Receipt

Status: canonical validation-only contract for Builder 1 and Builder 4;
CEO review required before any future production decision.

## Purpose and ownership

`Builder2QualificationReceiptV1` is the single shared authority artifact for
one already accepted PR-#66 qualification result. It is a deterministic
projection, not an execution result and not an authorization. Builder 2 is
the only issuer. Builder 1 and Builder 4 may validate and consume it, but
neither may issue or recreate it.

The receipt module has no provider client, network, credential, scheduler,
launchd, publisher, ledger, Cloudflare/Worker, Research, sealed-data, model
binding, betting, or deployment capability. It does not create CEO
authorization and performs no state write.

## Issuance boundary

`issue_builder2_qualification_receipt(report, observation, result)` accepts
only the exact accepted tuple from the PR-#66 qualification boundary:

- report status is `REAL_OBSERVATION_VALIDATED`;
- the supplied immutable result is present in the report, is accepted and
  real-observed, has the same observation/provider/league identity, and has
  zero qualification and cascade failure codes;
- the observation is `REAL_OBSERVED` and structurally valid;
- its serialized `ControlledShadowCaptureAttestation` is valid and exactly
  binds session, controlled run, CEO authorization, fixture, provider,
  event/request IDs, adapter provenance, capture time, raw/normalized
  digests, and the serialized cascade digest;
- the serialized cascade is structurally valid and has
  `prediction_input_allowed=true`.

`TEST_FIXTURE`, `MOCK`, `OFFLINE_REPLAY`, rejected observations, and
`OBSERVED_VALID_CONTRACT` results cannot issue a receipt. A real marker alone
cannot issue one, and the receipt layer never turns candidate data into real
evidence.

## Bound fields and digests

The receipt binds:

- schema/version and deterministic receipt ID;
- report identity/digest, result digest, session ID, controlled-shadow-run ID,
  and CEO authorization ID;
- exact fixture, provider, event ID, request ID, observation ID,
  observation/normalized-record digest, cascade-evidence digest, and
  capture-attestation digest;
- adapter version and adapter source SHA;
- `REAL_OBSERVATION_VALIDATED`, `accepted=true`,
  `prediction_input_allowed=true`, zero failure codes;
- `no_bet=true`, `publication=false`, `production_activation=false`, and
  `monetary_spend_authorized=false`.

`semantic_digest` uses canonical JSON with sorted mapping keys and stable
separators. Mapping serialization order therefore does not change a digest.
Any bound-field change changes the semantic receipt digest, and the receipt's
own digest is checked on every deserialization. The receipt contains digests,
IDs, and contract fields only; it contains no raw provider payload, secret,
API key, credential, or authentication header. A digest is an integrity
binding only; no cryptographic authenticity or signed attestation is claimed.

## Consumer seams

`validate_builder1_qualification_receipt` is the narrow Builder-1 seam. It
accepts an already-issued receipt and can be given the exact expected
observation, report, accepted result, and CEO authorization. It rejects a
receipt copied from observation A when the expected observation is B.

`validate_builder4_qualification_receipt` is the narrow Builder-4 seam. It
can require the exact expected cascade evidence and observation context.
Builder 4 remains responsible only for cascade execution/evidence and never
issues this receipt.

Both seams are pure validation functions. They do not execute, authorize,
select, publish, bet, or activate anything.

## Safety state

Receipt validity does not authorize Controlled Activation. It does not select
a provider authority, signal-time winner, research model, or production
model. The wider system remains disabled-by-default, NO-BET, unpublished,
model-unbound, and without live provider registration.

## Verification

The no-network tests cover valid PR-#66 issuance, fake markers, non-real and
rejected evidence, report/result mismatch, A-to-B copying, all authority and
evidence digest bindings, unsafe state, deterministic serialization, no
secrets, and both consumer seams. The existing PR-#66 qualification and
PR-#63 cascade regression suites remain part of verification.
