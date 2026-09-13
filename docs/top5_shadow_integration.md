# Top-5 Shadow Integration

This document describes a disabled, offline-only integration of the frozen
Top-5 research package with the merged shadow-readiness contracts. It is not a
model approval, a provider approval, a scheduler change, or a betting path.

## Frozen input and dependency

- Frozen Research SHA: `6eaabbec7d0182103d815c72fae4976e261b40aa`
- Frozen Research branch: `feat/top5-framework`
- Production/shadow dependency: PR #55 production contracts and PR #56 shadow
  readiness/signal-time harness, both present on the base used for this work.
- Integration source SHA: supplied as an exact value when the reproducibility
  manifest is built; `latest`, `current`, and `best` are rejected.
- 2425 calibration outcomes: sealed.
- 2526 holdout outcomes: sealed.

The integration branch does not copy, rewrite, score, or unlock the frozen
research data. The binding inventory records Git blob identities from the
frozen commit and distinguishes historical evidence from inference artifacts.

## Candidate readiness

Every league has an explicit M1-M7 inventory. Every candidate remains disabled
for production. Only M5 has a legitimate offline adapter in this branch.

| Candidate | Definition and required inputs | Frozen artifact finding | Readiness |
|---|---|---|---|
| M1_DC | Dixon-Coles snapshot, fixture teams, kickoff, causal future-season state | `dc_2425.pkl` exists, but no inference-safe shadow-season snapshot exists | `UNSUPPORTED_INPUT` |
| M2_Elo | Current causal Elo state, fixture teams, kickoff | `elo_series_dev.pkl` is historical state and not a current shadow-state contract | `UNSUPPORTED_INPUT` |
| M3_LGBM_dmwd | Trained LightGBM model and causal feature vector | OOF predictions exist; no trained model artifact exists | `NOT_REPRODUCIBLE` |
| M4_LGBM | Trained LightGBM model and causal feature vector | OOF predictions exist; no trained model artifact exists | `NOT_REPRODUCIBLE` |
| M5_market_preclose | Signal-time home/draw/away odds | Deterministic canonical inverse-odds formula; no learned artifact required | `AVAILABLE_FOR_SHADOW` |
| M6_market_elo_blend | Signal-time market, causal Elo state, fixed alpha | Historical Elo and alpha-sweep evidence exist; no fixed inference alpha exists | `NOT_REPRODUCIBLE` |
| M7_market_residual | Trained residual model and causal market/feature vector | OOF predictions exist; no trained model artifact exists | `NOT_REPRODUCIBLE` |

OOF CSVs are never loaded as models. The unavailable candidates are rejected
before any shadow run can create an artifact. No production winner is inferred
from DEV Brier or edge results.

## Frozen artifact inventory

All hashes below are Git blob SHA-1 identities in the frozen Research commit,
not claims that the file is a deployable model.

| League | DC 2425 snapshot | Elo series | M6 alpha evidence | M3 OOF only | M4 OOF only | M7 OOF only |
|---|---|---|---|---|---|---|
| BL1 | `7b001566218584375ccb339e4a974ec75861b1d2` | `1e499ebec98dcd4a07cc7026c3f4492a1c534c48` | `bb7d7730e3e3f3eba4736c3afc8f71e94f8beda0` | `4ea19351169be7d78c920ca217470ce53d3bd863` | `f5d7cf9294ad6cd359c18154dba68cfd717a7b20` | `761beae5c6b0c8dc39c10f6fe6369a69bd23936e` |
| EPL | `d5e183fec8f9da0da4ec7b10efe3bd38aa8ca33b` | `c43335e7f5bacd2857751a734a7783a0baa70ce5` | `b4003e75ca099847c7a3c2f4beb34658ccc7be2c` | `560c84a1ce1a3e24cb1c772660d0cf820a4bc263` | `ccd1b1d037298a156711d643265c14278922e2b0` | `b56b8e509985c877c2e31ae3ab0eed44accc37e2` |
| La Liga | `1c9755706ab2df6371193b92f3ba299864f04384` | `5106bae83c105c31df4989f62940abfa155b402d` | `ded877c39472d7ee2b3d7b000561edd843bf49eb` | `4b151af7ea9cdb1a7a578dd36fa418e4db58c58e` | `f312cdd64a22a8603a80e3197f686acd718958b0` | `a4618e12398f742b82349d539989becfb65bce29` |
| Serie A | `e98d6779a3810916fa904513fc764e7d70dd9658` | `2c45a3b12dcd92b596c0caf6f92e5488fdc48bed` | `b20b9d8ea35a6eff778a30ebb39b55441da48641` | `0a73ae1de2c50aba58aacebb93f53784e9b66c6c` | `1cc92e44b1853e9f9459a45fe9a26be039f59e49` | `c78c7c7173064521407248535852d807f1a9bc51` |
| Ligue 1 | `eddbd601091716d41ca2353e239f8f4b129c47da` | `e8f713937787fde021f0eaead9175794708259c5` | `0f1e0bf5c0b2c8e87db89a2a6245fb93f38de280` | `a3c8213ada4e6b29537088d9b2308f979e35414d` | `72ef740bdc229881e4a8c270c8ad4a4dd249fddc` | `cc5cd9763ad234987d827698dd2d9449bd40b925` |

Frozen implementation source identities:

- M1: `research/top5/core/models_dc.py` -> `cf8d50dfaeaba69a4d8954494c1bbbcbd77bf484`
- M2: `research/top5/core/elo.py` -> `e6cbc050816bcbf58f3b50e3a72e8af6a618d7c5`
- M3/M4/M7: `research/top5/core/models_lgbm.py` -> `26e33033130b65d6c35d9945e1b7f11282bfcdbb`
- M5: `research/top5/core/canonical_market.py` -> `7d31e8857cdd48296a85484153756c718113883f`
- M6: `research/top5/core/models.py` -> `ae28d1acad3b2c00ce72b2bc94754e0e4fd9b5df`

## Binding contract

`top5_research_binding.py` provides immutable `Top5ResearchBinding` objects.
Each binding requires the exact Research SHA, league, candidate, model formula
or artifact identity/hash, feature schema version/hash, inference implementation
version, provider mapping, signal-time contract ID, snapshot generation,
fixture key, and `no_bet=true`.

The M5 adapter is deliberately explicit:

- feature schema: `market_home`, `market_draw`, `market_away`;
- schema hash: generated from that ordered field tuple;
- implementation: `top5-shadow-m5-market-v1`;
- formula: inverse odds normalized over home/draw/away;
- closing snapshots and odds at or below 1.0 are rejected.

## Five-league readiness matrix

| League | Provider/sport mapping | Candidate available | Signal-time | Shadow inference | Provenance | NO-BET | Health |
|---|---|---|---|---|---|---|---|
| BL1 | The Odds API / `soccer_germany_bundesliga` | M5 formula only | Test contract only | Offline PASS | Full binding | YES | In-memory |
| EPL | The Odds API / `soccer_epl` | M5 formula only | Test contract only | Offline PASS | Full binding | YES | In-memory |
| La Liga | The Odds API / `soccer_spain_la_liga` | M5 formula only | Test contract only | Offline PASS | Full binding | YES | In-memory |
| Serie A | The Odds API / `soccer_italy_serie_a` | M5 formula only | Test contract only | Offline PASS | Full binding | YES | In-memory |
| Ligue 1 | The Odds API / `soccer_france_ligue_1` | M5 formula only | Test contract only | Offline PASS | Full binding | YES | In-memory |

The provider/source matrix records football-data CSV as the current result
source and the existing The Odds API mapping as descriptive metadata. Shadow
authority is static injected input only. Live provider authority remains
undecided and is not changed here.

## Offline end-to-end path

The integration runs this path independently for each league:

`static fixture -> static signal-time market snapshot -> causal feature boundary -> M5 formula -> prediction artifact -> NO-BET shadow signal -> in-memory archive -> health record`

The implementation uses the merged injected shadow pipeline and static test
providers. It makes no network request, spends no provider credits, writes no
archive/public file, calls no scheduler, and has no ledger or publisher
dependency. Staged public paths are validated references only and publication
remains false.

## Causality and data safety

- The signal contract requires timezone-aware timestamps and normalizes aware
  values to UTC; naive values fail closed.
- `MarketSnapshotKind.CLOSING` is rejected by the feature, prediction, and
  signal boundaries.
- M5 reads only the supplied signal-time odds and has no historical outcome
  feature.
- Missing snapshots produce a deterministic skip with an explicit health reason.
- Missing features, malformed probabilities, NaN values, invalid sums, foreign
  fixtures, wrong leagues, and unavailable candidates fail closed.
- The integration never reads 2425/2526 outcomes for evaluation or selection.
- No research methodology, feature, threshold, hyperparameter, or DEV result
  was changed.

## Deterministic identity and lifecycle

Prediction and signal IDs include league, fixture, frozen Research SHA,
candidate, model hash, feature schema hash, signal-time contract, and snapshot
generation through the binding identity. The in-memory archive rejects identity
collisions and suppresses an identical duplicate. A changed snapshot generation
creates a distinct binding and prediction identity. Different leagues remain
isolated even when fixture strings collide.

The reproducibility manifest is deterministic and contains the exact
integration SHA supplied by the caller, frozen Research SHA, candidate IDs,
artifact hashes, schema hashes, provider mapping, contract identity, snapshot
generation, fixture identity, and dependency versions. It has no generation
timestamp.

## Health and failure matrix

Health records expose league, fixture, candidate, Research SHA, artifact hash,
schema hash, contract ID, snapshot generation/age, inference status/latency,
skip reason, stale rejection, provider failure, fallback state, duplicate
suppression, `no_bet`, `registered`, and `publication`.

The focused integration tests cover missing artifacts/unavailable candidates,
wrong hashes and Research SHA, missing features, closing snapshots, stale or
invalid input boundaries, malformed/NaN/out-of-range/non-unit probabilities,
namespace references, duplicate identity, changed snapshot generation,
cross-league fixture collisions, and deterministic manifests. The merged PR #56
tests cover bounded event-relative timing, retries, bulk reuse, fallback
scenarios, and rollout predecessor gates.

## Explicitly disabled

- no model or strategy is production-approved;
- no Top-5 registry entry is enabled;
- no live signal-time values are approved;
- no provider authority is selected for live use;
- no scheduler, launchd job, GitHub schedule, publisher, Worker, Cloudflare
  binding, or ledger path is wired;
- no public/staged artifact is written;
- no bet can be placed by this code.

The next CEO gate is separate approval for any shadow environment execution,
provider validation, or eventual production activation. That approval must not
unlock sealed data or infer a production winner from this offline package.
