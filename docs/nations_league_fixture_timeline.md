# UEFA Nations League fixture timeline

This offline timeline crosswalks the frozen 512-match evaluation source to
official UEFA schedules. It is a fixture identity and timing artifact; it does
not assert complete qualification/relegation mathematics or prediction uplift.

Build and validate it with:

```bash
python scripts/build_nations_league_fixture_timeline.py
```

The builder emits:

- `results/research/nations_league_fixture_timeline_v1.json`
- `results/audits/nations_league_fixture_timeline_coverage_v1.json`

Each result fixture gets a deterministic canonical `fixture_id`. The frozen
source does not provide official UEFA match IDs, so `source_fixture_id` is
explicitly null. Kickoff is populated only by a unique exact crosswalk on
edition, normalized home/away participants, and the result-source date. Pair
matches with different dates are retained as unresolved candidates, never used
to infer a postponed fixture's date. An ambiguous exact crosswalk also remains
unresolved.

## Source and time semantics

The schedule extract records SHA-256 hashes of the downloaded public schedule
PDFs, source URLs, extraction version, and normalized row provenance. The
2020/21 group fixture PDF is superseded for the actual group dates and is kept
only as audit evidence; its kickoff times are not used. The 2020/21 and 2022/23
final-tournament match times are separately sourced from UEFA schedule
announcements. Schedule times are interpreted in `Europe/Paris`, using IANA
daylight-saving rules; no fixed CET offset is assumed.

For a normal played fixture with verified kickoff, the timeline's
`result_safe_available_at` is kickoff plus a conservative six-hour completion
buffer, covering regulation, extra time/penalties and additional delay margin.
Administrative awards without a played match have no such bound and
remain null. No outcome may be used at or after kickoff: Builder 1 must enforce
the strict order
`result_safe_available_at < training_cutoff < prediction_cutoff < target_kickoff`.
The exported `validate_causal_cutoff_order(...)` helper enforces this ordering
and rejects equality or naive timestamps. The schedule timestamp alone does not
establish a target lifecycle or qualify a feature row.

## B3 lifecycle projection

For each verified kickoff the row provides the exact T−24h initial point and
T−90m refinement point. It records the closing-odds kickoff boundary but keeps
`closing_benchmark_capture_at` null because this dataset has no observed close
capture. Every row has `closing_odds_prediction_input=false`. These are
deterministic schedule projections, not evidence that a snapshot was actually
captured. The coverage audit classifies every result dated 2022-06-11 relative
to `2022-06-11T00:25:00Z`; unresolved kickoff remains `unknown`.

## Current coverage

The committed coverage audit is deliberately partial: all 512 input result
rows have unique canonical fixture identities, while only uniquely
crosswalked official schedule rows have verified UTC kickoff. Each unresolved
fixture carries its own reason and any dated participant-pair schedule
candidates. It must not be consumed as complete 512/512 timeline evidence.

Administrative award references:

- Switzerland–Ukraine, 2020-11-17: UEFA declared the unplayed fixture forfeited;
  therefore the result-safe availability timestamp is unresolved.
- Romania–Kosovo, 2024-11-15: UEFA lists Romania's 3-0 forfeit award; therefore
  the result-safe availability timestamp is unresolved.

No provider, credential, quota, production, activation, publication, betting,
or ledger interfaces are used by this data builder.
