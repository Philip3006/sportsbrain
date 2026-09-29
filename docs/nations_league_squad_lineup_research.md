# Nations League squad / lineup research v1

## Result

Research status: **`NL_SQUAD_FEATURES_FORWARD_EVIDENCE_REQUIRED`**.

The local frozen Nations League validation sample contains 512 matches across
the 2020/21, 2022/23, delayed 2022/23 play-off, and 2024/25 blocks. The audit
found **zero verified fixture-bound, point-in-time squad/absence/lineup
captures** for those fixtures. Historical squad-feature coverage is therefore
0/512 (0%). No squad ablation was run and no improvement/regression claim is
supported. This is an evidence limitation, not a finding that the features
have no predictive value.

The existing frozen Dixon–Coles component is retained below only as baseline
context. It is not a matched squad-feature comparison: the source audit says
the retrospective feature reconstruction uses current static squad-value
inputs and default squad context where historical snapshots are absent.

## Source audit

| Source / seam | What it supplies today | Nations League PIT suitability |
|---|---|---|
| `src/data/squad_models.py` | `PlayerStatus`, `SquadReport`, cache helpers | Neither model has source/capture timestamps, fixture identity, or evidence kind. `default_report()` is empty, while `availability_score` for an empty report is 1.0. |
| `src/data/squad_merger.py` / `squad_availability.py` | Covers → Transfermarkt → Wikipedia → default chain; `squad_impact_features()` | Current/future retrieval, not historical replay. Covers-only absences are padded with generated fit placeholders in the existing merger. The research path does not call that merger; it invokes its impact calculation only after independently validating a complete PIT matchday roster. |
| Covers / `src/data/injury_data.py` | Current injury/doubtful rows; static current fallback | Current team-level state, not queried as of an old fixture. No complete roster or per-player historic observation timestamp. |
| Transfermarkt | Current/previous-season roster, injury/suspension and values | Scrapes a live page and caches by filesystem mtime for 24 hours. It does not retrieve a preserved historical as-of page for each Nations League fixture. |
| Wikipedia | Current/future 2026 World Cup squad lists | Not historical Nations League call-ups or fixture-bound availability. |
| SofaScore | Current squad/player-value detail via existing key convention | No verified historical PIT feed in this repository. No request or credential read was made. |
| `data/suspensions.json` | 48-team current overlay, last updated 2026-08-14 | No match/player observation timestamps; current World Cup context, not historical Nations League evidence. Read only. |
| `docs/data/squads.json` | 48 current/future team snapshots, updated 2026-09-29, source `wikipedia` | No fixture binding or archived PIT observations. Read only. |
| `src/data/market_values.py` | Static national-team values, approximate June 2026 | Not a historical as-of source for the 2020–2025 evaluation fixtures. Existing builder consumes these values; this task does not change that behavior. |
| FotMob | Lineups and player ratings for finished match pages | The target-match lineup/rating is post-match and is excluded from pre-match prediction inputs. |
| `src/features/player_rating.py` | Prior player xG/shot-quality when covered | Existing open-data coverage is other tournaments, not a verified Nations League lineup/status archive. |
| `src/features/builder.py` | Optional squad reports; defaults to `default_report()` when omitted | Missing squad input becomes neutral-looking numeric availability. The offline Nations League validation path does not supply real PIT squad reports. This production behavior is documented, not changed. |
| `src/scanner/scoring.py` | Current scan can call `squad_report()` for present/future matches | Not a historical evidence archive; this path is not invoked by this research task. |

The inspected local snapshot candidates are reported in
`results/audits/nations_league_squad_lineup_research_v1.json`. The current
48-team files are explicitly classified as nonhistorical, not as zero-absence
or fit evidence, and do not establish coverage of every Nations League
participant or edition.

## Feature contract

Version: `nl-squad-lineup-research-feature-v1` from input snapshots using
`nl-squad-lineup-snapshot-v1`.

`src/analysis/nations_league_squad_research.py` is offline-only and is not
imported by scanner, signal detector, runtime, publication, activation, or
betting code. It makes no requests. A snapshot must bind competition, exact
fixture ID, canonical home/away participants, kickoff, capture timestamp,
source records, each source's native as-of timestamp and SHA-256, complete
team scope, stable player IDs, positions, and timestamped player status
claims. All effective/source/capture timestamps must be at or before the
prediction cutoff, and prediction cutoff must precede kickoff.

Availability and absence features require an exact, complete `matchday_squad`
with a status claim for every player. A source conflict makes the affected
team's aggregate unavailable; no source-priority guess resolves it. A
`national_roster`, partial squad, unknown status, or stale status snapshot is
not interpreted as a complete matchday squad. Missing numeric features are
`null`, not 1.0, 0.0, or a generated fit player.

The layer covers:

- availability, unavailable counts and goalkeeper/defender/midfielder/forward
  absences;
- unavailable starters only from a timestamped 11-player confirmed lineup;
- key-player risk only when the key-player flag itself has timestamped source
  evidence;
- weighted impact using existing SportsBrain `squad_impact_features()` after
  the completeness/time gates. All position groups must be known. Its existing
  per-player-value weighting is used only when every squad value has valid
  source provenance; otherwise the existing position-weighted method applies.
- squad market value and ratio/log-ratio only from complete, source-timestamped
  matchday-squad values;
- starting-XI/bench strength only from source-provided scores with a named,
  versioned methodology and cutoff-safe timestamps. The research layer does
  not invent player-strength weights or fit a player model.
- returners, missing usual starters, turnover, previous starting-XI minutes,
  7/14-day international minutes, days since previous international match,
  and match counts only when the prior-match archive declares a complete
  14-day window and each event has a completed result and timestamped player
  minutes available before the target prediction time.

Quality is deterministic: HIGH requires complete matchday coverage, no status
conflicts, at least two provenance sources, and status evidence no older than
six hours; MEDIUM requires complete, conflict-free coverage and evidence no
older than 24 hours; LOW marks partial/conflicting evidence; UNAVAILABLE marks
missing or unusably stale status evidence. Confirmed lineup evidence is limited
to six hours. These are research eligibility thresholds, not production
policy. `fallback_usage=false` means the research calculation used no fallback.

`TEST_FIXTURE` snapshots are accepted by feature derivation only with an
explicit test-only argument, retain that evidence kind in output, and are
rejected by causal ablation evaluation and the forward capture writer.

## Evaluation and current metrics

The evaluator requires identical fixture IDs, outcomes, prediction cutoffs,
real feature rows, and snapshot digests across all variants. It computes
multiclass Brier score and log loss, one-vs-rest Home/Draw/Away calibration,
10-bin ECE, accuracy, coverage, mean maximum probability, and paired
fixture/date-cluster bootstrap differences with a fixed seed.

| Ablation | Historical rows | Result |
|---|---:|---|
| Baseline (context only) | 512 | Existing frozen DC component: Brier 0.619594; 10-bin mean one-vs-rest ECE 0.080737; argmax accuracy 0.496094. This is not comparable to a squad variant. |
| + availability | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| + positional absences | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| + weighted player impact | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| + squad market value | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| + lineup strength | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| + rotation / load | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |
| Full candidate | 0 | `HISTORICAL_POINT_IN_TIME_UNAVAILABLE` |

The existing audit has no authentic historical pre-match 1X2 market join
either; it reports the canonical market stacker as not evaluated. Tier A/B/C/D
is unavailable in the result source and is not inferred. All requested
absence/favorite/strength/lineup/data-quality strata are consequently
`not_evaluated`, not zero-effect findings.

## Forward evidence option

If historical PIT evidence cannot be obtained from an approved archive, the
append-only `append_forward_capture()` API can store *future* real captures at
`T24H`, `T6H`, `T90M`, and `CONFIRMED_LINEUP`. It requires an already-created
private parent directory, a regular non-symlink JSONL file with mode 0600,
timestamped source records, and `REAL_OBSERVED` input. It locks and appends a
single record, rejects duplicate fixture/slot keys, and never overwrites an
existing capture. This task did not start capture: count remains zero. No
provider connector or credential handling is implemented here.

The declared time slots are bounded: `T24H` is 22–26 hours before kickoff,
`T6H` is 5–7 hours before kickoff, and `T90M` is 1–2 hours before kickoff.
`CONFIRMED_LINEUP` requires both lineups to be confirmed and no older than six
hours. Match-load minute totals are summed player-minutes for all captured
players in the team archive, not an inferred per-player fatigue score.

Reproduce the report from the repository root:

```sh
python3 scripts/run_nations_league_squad_research.py \
  --source-main-sha 0ab36238451b80d0478fd130ca6209e562a9bd98
```

Run contract tests:

```sh
python3 -m pytest tests/analysis/test_nations_league_squad_research.py -q
```

The output is a research audit under `results/audits/`; it is not a production
snapshot and cannot be read as qualification, activation, publication, betting,
or provider authority.
