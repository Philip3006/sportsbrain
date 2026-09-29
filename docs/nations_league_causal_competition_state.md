# UEFA Nations League causal competition-state snapshot (v1)

This is a research/data deliverable. It does not change a model, production
provider, runtime, activation, publication, betting, or ledger path.

## Current status: PARTIAL

The reproducible input snapshot contains all 512 matches in the SportsBrain
evaluation block and exact final scores from the audited local results cache.
It supports deterministic edition/group assignment for all league-phase
fixtures and reconstructs point, W/D/L, goals-for/against, and goal-difference
tables using only matches whose source date is strictly earlier than the
fixture date. Same-day results are excluded. The state cutoff is set to 00:00
UTC on the day before the fixture date, a conservative date-only cutoff.

This does **not** meet the full launch contract yet. The upstream 512-row cache
has dates but no official UEFA match IDs, kickoff timestamps, matchday fields,
or a frozen complete schedule. The v1 records consequently retain an internal
deterministic fixture key while `official_fixture_id`, `kickoff`, and `matchday`
remain `null`. Schedule-derived and mathematical qualification/relegation
primitives also remain explicitly unresolved; final tables are never copied
back into historical states.

The edition contract includes group membership and distinct edition formats.
The 2022/23 B2 Russia exception is represented as an ineligible participant
automatically ranked fourth/relegated. The two March 2024 Lithuania–Gibraltar
play-out legs are a distinct phase, not group matches. The 2022/23 exact
Article 15 tie-break text has not been frozen from its regulation source. Even
where rule text is known (2020/21 and 2024/25), missing disciplinary totals can
leave exact tied ranks unresolved.

For non-group fixtures, records retain `home_league_tier`, `away_league_tier`,
`home_group`, and `away_group` separately. A cross-league fixture's
`league_tier` is the explicit home/away pairing (for example `A/B`); the
delayed C/D play-out is labeled `C/D`. This avoids assigning a tie to
whichever team happens to be at home.

Do not use this partial dataset as a substitute for exact kickoff timestamps,
official fixture IDs, matchday, or fully determined qualification/relegation
states. No prediction uplift is evaluated here.

## Reproduction

The canonical input snapshot is `data/research/nations_league/historical_results_source_v1.json`.
Its upstream cache SHA-256 is pinned to the digest recorded by the existing
historical validation audit. To re-freeze from that exact local cache (no
network is performed):

```sh
python3 scripts/freeze_nations_league_results_source.py \
  --source-pickle /path/to/international_results.pkl \
  --output data/research/nations_league/historical_results_source_v1.json \
  --committed-at 2026-09-29T16:00:59Z

python3 scripts/build_nations_league_competition_state.py
```

The generator and validator are `src/analysis/nations_league_competition_state.py`.
The source snapshot digest, edition-contract digest, per-record digest, dataset
digest, and coverage digest use sorted compact UTF-8 JSON with SHA-256. Change
the pinned source or contracts only with a corresponding documented provenance
update. The loader recomputes both dataset and coverage digests and rejects
attempts to fill unsupported official IDs, kickoff times, or matchdays without
changing the pinned evidence.

## Edition rule summary

- **2020/21:** A/B/C each have four groups of four; D has one group of four and
  one group of three. Group winners B/C/D promote; A/B bottom sides are
  relegated; the C fourth-place play-out concept and the separate delayed
  2020/21 play-outs are documented. Group tie-break Article 15 includes
  head-to-head away goals. Disciplinary data is absent from the match source.
- **2022/23:** Same broad group sizes, with Russia not participating in B2 and
  assigned automatic fourth place/relegation. A group winners advance to the
  Finals; B/C/D winners promote; A/B bottom sides relegate; C/D play-out
  mechanism exists, with the target source containing only the Lithuania–
  Gibraltar two-leg tie in March 2024. EURO 2024 play-off qualification is a
  separate downstream competition state and is not inferred here. The exact
  edition regulation text for tied-team order is an open blocker.
- **2024/25:** A/B/C each have four groups of four; D has two groups of three.
  A top two enter two-legged quarter-finals; group winners B/C/D promote;
  A/B bottom sides are directly relegated; two lowest-ranked C fourth-place
  sides are relegated and other C fourth-place sides have a later C/D play-off;
  A/B and B/C promotion/relegation ties are two-legged. The tie-break no
  longer uses head-to-head away goals. Missing cards prevent resolving every
  tied rank.

## Schema and fail-closed semantics

`uefa-nations-league-causal-competition-state-v1` emits one record per source
fixture. `fixture_id` is a deterministic SportsBrain internal key, **not** an
official UEFA ID. A record has a conservative `state_cutoff`, standings for
the involved group(s), source and record digests, and explicit missing-field
statuses. For a tied point table, the record reports `rank_min`/`rank_max` as
null and status `unresolved_points_tie`; it never uses alphabetical ordering
as a sporting tie-break. Qualification and relegation fields are null until
the official full schedule and enough edition-specific rule inputs are
available to prove the state.

## Frozen primary-source references

The edition contract JSON carries the official UEFA source URLs, retrieval
timestamp, and notes. Raw remote PDF/HTML digests are not claimed because the
source bytes were not vendored; the digest in each generated record binds the
local canonical inputs actually used. Key primary references:

- [UEFA 2020/21 group draw, format, and dates](https://www.uefa.com/uefanationsleague/news/0253-0d821c7c9ca8-3e97e8cc9b99-1000--2020-21-nations-league-who-will-play-who/)
- [UEFA 2020/21 Nations League regulations](https://editorial.uefa.com/resources/0256-0f842fa4944a-f636da185247-1000/regulations_of_the_uefa_nations_league_2020_21.pdf)
- [UEFA 2020/21 official fixture list](https://editorial.uefa.com/resources/025b-0ed7e6dacfbb-9e8567b03e90-1000/2020-21_-_unl_-_fixtures_list_league.pdf)
- [UEFA 2022/23 groups and format](https://www.uefa.com/uefanationsleague/news/026e-13698f20e8cb-8372848125f5-1000--uefa-nations-league-2022-23-calendario-partidos-ligas-forma/)
- [UEFA 2022/23 draw procedure](https://editorial.uefa.com/resources/026f-13c241515097-67a9c87ed1b2-1000/unl_2022-23_league_phase_draw_procedure_en.pdf)
- [UEFA decision on Russian teams](https://www.uefa.com/uefanationsleague/news/0275-150d0b0a13f1-90de757e6bab-1000--suspension-of-russian-teams-and-clubs/)
- [UEFA 2022/23 results](https://www.uefa.com/uefanationsleague/news/0270-13fa7d271b2d-786414f61047-1000--nations-league-fixtures-and-results/)
- [UEFA confirmation of the Lithuania–Gibraltar play-out dependency](https://editorial.uefa.com/resources/028a-1a1e18631263-95c63cec8617-1000/202425_unl_qualifying.pdf)
- [UEFA 2024/25 draw and format](https://www.uefa.com/uefanationsleague/news/028a-1a1579c12dec-5cadac35d0a0-1000/)
- [UEFA 2024/25 official fixture list](https://editorial.uefa.com/resources/028a-1a2379ee841a-226ce0a39213-1000/uefa_nations_league_-_2024-2025_-_league_phase_-_fixtures_by_league.pdf)
- [UEFA 2024/25 tie-break Article 15](https://documents.uefa.com/r/Regulations-of-the-UEFA-Nations-League-2024/25/Article-15-Equality-of-points-league-phase-Online)
- [UEFA 2024/25 format summary](https://www.uefa.com/uefanationsleague/news/0288-19a14a0f77d0-8b88a4fd8106-1000/)
