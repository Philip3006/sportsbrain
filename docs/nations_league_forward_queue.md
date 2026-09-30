# Nations League manual forward queue

Consumes Builder 1's `nations-league-future-fixture-manifest-v1` interface
(PR #228), using its verified fixture identity, kickoff and source provenance.
No source research or model changes are included. Synthetic examples in tests
are offline test data, not verified live fixtures.

```sh
python3 scripts/nations_league_forward_queue.py --manifest fixtures.json \
  --as-of 2026-10-01T20:00:00Z --plan --store /external/shadow/nl.jsonl
```

Default invocation is also plan-only. Explicit UTC is mandatory. INITIAL is
inclusive 26–22 hours before kickoff; REFINEMENT is inclusive 120–60 minutes.
The remaining intervals are TOO_EARLY, BETWEEN_WINDOWS, TOO_LATE and STARTED.
Output includes both windows, next pending window starts, due/missed/captured
phase identities and exact prediction cutoff. Past missed phases remain visible
even when the refinement phase is due.

Manual execution adds `--execute-offline --input-state input-state.json`.
The snapshot must be produced by merged #229 `build_input_state(...)` using
canonical causal history and completeness proof. Its cutoff must equal `--as-of`
(equivalent UTC spellings accepted). The queue rebuilds and verifies the entire
snapshot, requires every team READY, binds its fixture objects including source
digests to the supplied #228 manifest, then calls `predict_from_input_state(...)`
for each due uncaptured phase. LIVE_RESULT_REFRESH_REQUIRED, STALE_INPUT,
MISSING_TEAM and AMBIGUOUS_IDENTITY fail before any prediction append. All due
fixtures must be present in the snapshot. Raw `--training` / `--input-provenance`
flags are removed. No network transport exists. `--model-digest` optionally pins
the expected frozen digest. A fixture manifest alone is insufficient to execute.

Store inspection is read-only in plan mode. Execution locks the JSONL store,
validates all due records before append, and never rewrites existing predictions
or settlements. An existing fixture/phase/frozen-model record is
ALREADY_CAPTURED. Duplicate lines, changed model, record ID or fixture binding
fail closed. A later invocation never recalculates an already captured phase.

All output remains SHADOW_ONLY / no_bet=true; no scheduler or publication path
is registered. Use an operator-selected local shadow store, not a production
ledger. Refinement may be captured even if INITIAL was missed; the summary
keeps that missed initial visible and does not claim a completed lifecycle.
