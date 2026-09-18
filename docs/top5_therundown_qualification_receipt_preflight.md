# TheRundown qualification-to-receipt preflight

This is an offline, read-only B1 compatibility seam. It is stacked on APP-B1
PR #94 at `d8b8dbae54f3705e8643835e8647614c70336151` and was developed after
inspecting the current APP-B2 PR #95 head
`9f0cc98a9e6f97376f5b373ba2b7215ec1754671`. It does not import or copy PR #95
implementation code.

`build_builder2_qualification_receipt_preflight()` recomputes the B1
qualification report, requires all five Top-5 leagues, and emits one
non-issued receipt-input projection for every accepted bookmaker observation.
Each projection retains the B1 evidence, bookmaker, 1X2 prices, timestamps,
quota/rate-limit state, adapter SHA, exact provider/event/request identity, and
the independently supplied controlled-shadow binding.

The preflight requires:

- `REAL_OBSERVED` evidence with `synthetic_reconstruction=false` and one real
  request;
- complete pre-match 1X2 prices, participant/fixture identity, bookmaker,
  source timestamp, fresh odds, digests, and provider provenance;
- exact run, qualification-session, CEO-authorization, provider, league,
  fixture, event, request, adapter, capture-time, raw/normalized-digest, and
  cascade-digest bindings;
- monotonic quota/rate-limit state with cost matching the usage delta; and
- one valid capture-attestation binding for every accepted evidence ID.

It returns `issuer_present=false`, omits the issuer-generated receipt ID and
receipt digest, and never calls `issue_builder2_qualification_receipt()`.
Qualification success alone and shadow success alone therefore cannot create a
receipt. No provider registration, authority change, model activation,
publication, betting, scheduler, ledger, Cloudflare, or network action occurs.

The current B1 identity is deliberately preserved as
`therundown_experimental`. PR #95 currently uses a different candidate label
(`therundown`), so that mismatch fails closed until an explicitly reviewed
contract alignment exists. Separately, the canonical Builder-2 issuer still
validates provider authority against its existing repertoire. Consequently,
complete five-league evidence plus a valid independent shadow attestation is
sufficient for this preflight projection, but not sufficient to issue a
canonical Builder-2 receipt until Builder-2 authority accepts the candidate.
