# Nations League forward input audit

Audited main: `afe0d1c1d3244c345d396dde43e27a8546ebcb5b` (#227 merged).

## Finding

The frozen runner accepts caller-supplied fixture/history/provenance JSON. It
enforces strict result-safe cutoffs and lifecycle windows, but its original input
digest binds only the supplied provenance dictionary. Unknown teams fall back to
the frozen Elo initial rating, and history completeness is not established.

The new offline `nations_league_forward_input` gate addresses these seams without
changing the runner, model parameters, model digest or prior shadow records.
Use `build_input_state(...)`, then `predict_from_input_state(..., phase=...)` for
verified future input. The original low-level #227 API remains available for
existing research; it is not itself proof of fresh input.

## Required input

- Canonical fixture ID, edition/evaluation block, canonical home/away names,
  competition UEFA Nations League, UTC kickoff, source provenance/source digest.
- Explicit UTC prediction cutoff; future kickoff; INITIAL 22–26h or REFINEMENT
  60–120m window remains enforced by #227.
- Result records with identities, scores, kickoff, source provenance/digest and
  `result_safe_available_at < prediction_cutoff`. Equal/future rows are rejected,
  not silently discarded. Callers must supply the causal slice.
- Historical completeness evidence: source provenance/digest,
  `results_verified_through` and `observed_at`. For READY, both timestamps must
  equal the prediction cutoff. Earlier verified-through evidence is STALE_INPUT;
  absent proof is LIVE_RESULT_REFRESH_REQUIRED. A last-match timestamp is not a
  completeness watermark. This is a trusted upstream attestation, not independent
  verification of a provider's completeness.

Missing teams are MISSING_TEAM; noncanonical aliases/case collisions are
AMBIGUOUS_IDENTITY. Ambiguous history and duplicate results fail closed.

## Exact provenance

The input snapshot digest hashes normalized causal rows, fitted Elo ratings,
target fixtures, cutoff, frozen model digest, source/coverage provenance and team
readiness. UTC result timestamps normalize before the frozen stable ordering.
Prediction construction rebuilds and verifies the complete snapshot, refuses
non-READY teams, and places `input_snapshot_digest` in #227 input provenance.
The existing #227 provenance hash therefore transitively binds the actual input.
No prediction or settlement records are rewritten.

Frozen model digest:
`f55549e7225f55deac23c7a31b757acf509ad0b4810b93ba8244301d3395a8ee`.

## Repository coverage / operational gap

The canonical timeline has 512 fixtures, 510 safe played results and two explicit
administrative exceptions. Its last kickoff is 2025-06-08T19:00:00Z. The adapter
checks dataset and per-record digests and excludes administrative exceptions.
It preserves the frozen absent-neutral default rather than inventing venue data.

This artifact has no result completeness watermark through a future prediction
cutoff. It therefore cannot alone certify READY, even for represented teams.
Untimestamped current Elo or the WM2026 snapshot is not an acceptable substitute
for the exact causal input history. No live refresh was attempted. A real future
fixture still needs an upstream canonical manifest and verified result history /
completeness attestation at its cutoff: LIVE_RESULT_REFRESH_REQUIRED.

No age-based threshold, live provider request, credentials, odds, feature changes,
publication, ledger, scheduler or production runtime changes are introduced.
