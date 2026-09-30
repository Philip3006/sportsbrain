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

Manual execution adds `--execute-offline --training training.json
--input-provenance provenance.json`. Training is a local canonical record list.
Provenance is keyed by fixture ID and supplies `timeline_digest`, exact
`fixture_source_digest` and `training_cutoff` equal to `--as-of`. The merged
#227 frozen runner validates causal training and builds predictions. No network
transport exists. `--model-digest` optionally pins the expected frozen digest.

Store inspection is read-only in plan mode. Execution locks the JSONL store,
validates all due records before append, and never rewrites existing predictions
or settlements. An existing fixture/phase/frozen-model record is
ALREADY_CAPTURED. Duplicate lines, changed model, record ID or fixture binding
fail closed. A later invocation never recalculates an already captured phase.

All output remains SHADOW_ONLY / no_bet=true; no scheduler or publication path
is registered. Use an operator-selected local shadow store, not a production
ledger. Refinement may be captured even if INITIAL was missed; the summary
keeps that missed initial visible and does not claim a completed lifecycle.
