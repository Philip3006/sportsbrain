# Nations League v1.1 result continuation

This is result research and an offline adapter only. No runner, queue, campaign,
model, provider, scheduler or publication code changes. No prediction is created.

The immutable #215/#233 base is 512 fixtures / 510 training results / two
administrative exceptions, digest
`2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef`.
The lower extension boundary is strictly after `2025-06-08T19:00:00Z`.
Base identities and date/participant keys cannot recur in the extension.
The frozen v1.1 digest remains
`50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626`.

## Official inventory

Sources: [UEFA 2024/25 complete results](https://www.uefa.com/uefanationsleague/news/028a-1a23b4c739a8-07fe752f503f-1000/)
and [UEFA 2026/27 complete calendar/results](https://www.uefa.com/uefanationsleague/news/02a2-1fea18abbcbc-456e846509e7-1000--2026-27-uefa-nations-league-all-the-league-phase-fixtures/).
The former includes the remaining four C/D playoff legs in March 2026; the latter
contains 26 completed fixtures on each of matchdays 1 and 2, September 24–29.
Its next matchday starts October 1. These complete competition schedules account
for the gap between June 2025 and September 2026; World Cup qualifiers and other
competitions are excluded. There are 56 expected and 56 verified played results,
zero new administrative exceptions, unresolved rows, duplicates or ambiguous
identities. All 56 source match IDs are independently retained in expected-ID
accounting so dropping a result fails closed.

Scores are extracted from official index match links. Kickoff UTC, Finished
status and reported full-time are read from each linked public UEFA match page's
match-header metadata. Only those fields are retained; no API is called or
credential read. The source extract records actual retrieval times, original
participant names, source URLs and SHA-256 hashes of the fetched public pages.
Official Republic of Ireland is normalized to the established canonical Ireland.

## Time and completeness semantics

`reported_full_time_at` is completion metadata, not proof of result publication
at that instant. Each row uses the conservative SAFE_BOUND
`max(actual result-index observation, actual match-page observation, kickoff+6h)`.
The six-hour buffer is the established #215 rule. Observations occurred September
30 around 17:04Z, so these rows are deliberately not backdated into March or
earlier September prediction cutoffs. Inclusion requires strict
`result_safe_available_at < prediction_cutoff`; equality/future fails.

The audited interval ends at `2026-09-30T17:04:37.635372Z`. Generation time is
explicit metadata supplied to the deterministic builder, distinct from observation
and verified-through. The chosen future cutoff is `2026-09-30T20:00:00Z`.
The committed completeness artifact is **STALE_INPUT**, not READY. It proves the
inventory through the observation, not completeness at a future clock. A new
causal completeness observation at the exact operational cutoff remains required.
No unresolved required row or incomplete interval can return READY.

Three immutable dated outputs accompany the source extract: result extension,
completeness at the chosen cutoff, and Elo continuation proof. Replaying with
identical generation/cutoff inputs reproduces their digests. Successor validation
rejects changes/removal of earlier evidence. Write later observations to new
artifacts rather than replacing this dated evidence or the frozen base.

## Builder 1 handoff

`build_extension(base, inventory, generated_at=...)` returns the sealed extension.
`completeness(extension, prediction_cutoff)` validates inventory/digests and gives
the operational state. `elo_continuation(base, extension, prediction_cutoff)`
adapts the retained 510 base results and the 56 safe extension rows, producing
566 causal training rows and an Elo state without calling prediction code.
It reports stale completeness explicitly: a reproducible state is not execution
approval. The state SHA-256 is
`7ccaed2c2fc237307c9f74a9adf7755bb6182fed085dffcd11cb4b56a39d47fc`.

Builder 1's v1.1 input migration must bind base digest, model digest, extension
digest, completeness digest, exact cutoff, source provenance, result rows and
team identity/readiness. `results_verified_through` must equal that cutoff and
observation must be causal. Do not transfer this snapshot's READY-at-observation
status to a later cutoff. This PR does not change #229 or any execution boundary.

## Earliest forward opportunities and timing seam

At research time 17:16Z September 30, Azerbaijan–Liechtenstein INITIAL has not
yet closed. The merged #228 manifest says October 1 kickoff 17:00Z, INITIAL
September 30 15:00–19:00Z and REFINEMENT October 1 15:00–16:00Z.
Germany–Serbia is next at manifest kickoff 19:45Z, INITIAL September 30
17:45–21:45Z and REFINEMENT October 1 17:45–18:45Z.

However, direct official match metadata contradicts those UTC conversions:
[Azerbaijan–Liechtenstein](https://www.uefa.com/uefanationsleague/match/2047955--azerbaijan-vs-liechtenstein/)
is 16:00Z, giving INITIAL September 30 14:00–18:00Z and REFINEMENT October 1
14:00–15:00Z. [Germany–Serbia](https://www.uefa.com/uefanationsleague/match/2047954--germany-vs-serbia/)
is 18:45Z, giving INITIAL September 30 16:45–20:45Z and REFINEMENT October 1
16:45–17:45Z. The manifest applies fixed CET to a daylight-saving period.
Builder 1 must resolve this source/UTC conflict before relying on those windows;
this result-only PR leaves its fixture/source-research files untouched.

If the Azerbaijan INITIAL closes before valid input is ready, classify
MISSED_INITIAL_WINDOW; do not create a backdated prediction. At the chosen 20:00Z
cutoff it is missed; Germany–Serbia remains within the official INITIAL window,
subject to fresh completeness and corrected fixture bindings. The earliest
remaining REFINEMENT is Azerbaijan–Liechtenstein on October 1 14:00–15:00Z.

v1.1 is not immediately operationally READY merely when migration lands: exact
cutoff completeness and corrected manifest UTC bindings remain prerequisites.
No paid provider calls, credentials, quota, predictions or production writes.
