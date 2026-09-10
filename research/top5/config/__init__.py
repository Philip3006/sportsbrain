"""Top-5 league configuration registry.

Each league config is a plain LeagueConfig dataclass instance. Python-native
so type-checking is trivial and no YAML parser is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LeagueConfig:
    """Per-league configuration for Top-5 flagship research.

    Fields:
        key                  short id used in directory paths
        fbdata_code          football-data.co.uk league code (D1, E0, SP1, I1, F1)
        display_name         human-readable name
        seasons              full ordered list of season codes to acquire
        dev_seasons          seasons used for DEVELOPMENT_LABELLED
        calibration_season   season used for CALIBRATION (predictions-only, sealed outcomes)
        holdout_season       season used for HOLDOUT (schema-only, sealed outcomes+closing)
        live_shadow_season   season used for LIVE SHADOW (deferred until in-season)
        calib_seed_folds     dev fold(s) that provide chronological seed for M6 alpha selection
        outer_folds          dev outer folds for walk-forward evaluation
        season_starts        approximate season kickoff dates (used by test_02)
    """
    key: str
    fbdata_code: str
    display_name: str
    seasons: tuple[str, ...]
    dev_seasons: tuple[str, ...]
    calibration_season: str
    holdout_season: str
    live_shadow_season: str
    calib_seed_folds: tuple[str, ...]
    outer_folds: tuple[str, ...]
    season_starts: dict[str, str] = field(default_factory=dict)

    def dataset_dir(self, top5_root: Path) -> Path:
        return top5_root / "leagues" / self.key / "dataset"

    def results_dir(self, top5_root: Path) -> Path:
        return top5_root / "leagues" / self.key / "results"

    def scripts_dir(self, top5_root: Path) -> Path:
        return top5_root / "leagues" / self.key / "scripts"

    def raw_pkl(self, top5_root: Path) -> Path:
        return self.dataset_dir(top5_root) / f"{self.key}_raw.pkl"

    def full_pkl(self, top5_root: Path) -> Path:
        return self.dataset_dir(top5_root) / f"{self.key}_raw_full.pkl"


# All Top-5 seasons follow the same 10-season window used for BL1 v7.
_TOP5_SEASONS = ("1617", "1718", "1819", "1920", "2021",
                 "2122", "2223", "2324", "2425", "2526")

_TOP5_DEV = ("1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324")
_TOP5_CALIB = "2425"
_TOP5_HOLDOUT = "2526"
_TOP5_LIVE_SHADOW = "2627"
_TOP5_CALIB_SEED = ("1819", "1920")
_TOP5_OUTER = ("2021", "2122", "2223", "2324")

# Season-start dates. These are approximate — used only by invariant test_02
# to prove DC snapshots are causal (fit_date < season_start).
_SEASON_STARTS = {
    "1617": "2016-08-19", "1718": "2017-08-11", "1819": "2018-08-10",
    "1920": "2019-08-09", "2021": "2020-09-11", "2122": "2021-08-13",
    "2223": "2022-08-05", "2324": "2023-08-11", "2425": "2024-08-16",
    "2526": "2025-08-15",
}


BL1 = LeagueConfig(
    key="bl1", fbdata_code="D1", display_name="Bundesliga (BL1)",
    seasons=_TOP5_SEASONS, dev_seasons=_TOP5_DEV,
    calibration_season=_TOP5_CALIB, holdout_season=_TOP5_HOLDOUT,
    live_shadow_season=_TOP5_LIVE_SHADOW,
    calib_seed_folds=_TOP5_CALIB_SEED, outer_folds=_TOP5_OUTER,
    season_starts=_SEASON_STARTS,
)

EPL = LeagueConfig(
    key="epl", fbdata_code="E0", display_name="Premier League",
    seasons=_TOP5_SEASONS, dev_seasons=_TOP5_DEV,
    calibration_season=_TOP5_CALIB, holdout_season=_TOP5_HOLDOUT,
    live_shadow_season=_TOP5_LIVE_SHADOW,
    calib_seed_folds=_TOP5_CALIB_SEED, outer_folds=_TOP5_OUTER,
    season_starts=_SEASON_STARTS,
)

LALIGA = LeagueConfig(
    key="laliga", fbdata_code="SP1", display_name="La Liga",
    seasons=_TOP5_SEASONS, dev_seasons=_TOP5_DEV,
    calibration_season=_TOP5_CALIB, holdout_season=_TOP5_HOLDOUT,
    live_shadow_season=_TOP5_LIVE_SHADOW,
    calib_seed_folds=_TOP5_CALIB_SEED, outer_folds=_TOP5_OUTER,
    season_starts=_SEASON_STARTS,
)

SERIEA = LeagueConfig(
    key="seriea", fbdata_code="I1", display_name="Serie A",
    seasons=_TOP5_SEASONS, dev_seasons=_TOP5_DEV,
    calibration_season=_TOP5_CALIB, holdout_season=_TOP5_HOLDOUT,
    live_shadow_season=_TOP5_LIVE_SHADOW,
    calib_seed_folds=_TOP5_CALIB_SEED, outer_folds=_TOP5_OUTER,
    season_starts=_SEASON_STARTS,
)

LIGUE1 = LeagueConfig(
    key="ligue1", fbdata_code="F1", display_name="Ligue 1",
    seasons=_TOP5_SEASONS, dev_seasons=_TOP5_DEV,
    calibration_season=_TOP5_CALIB, holdout_season=_TOP5_HOLDOUT,
    live_shadow_season=_TOP5_LIVE_SHADOW,
    calib_seed_folds=_TOP5_CALIB_SEED, outer_folds=_TOP5_OUTER,
    season_starts=_SEASON_STARTS,
)


ALL: dict[str, LeagueConfig] = {
    "bl1": BL1, "epl": EPL, "laliga": LALIGA, "seriea": SERIEA, "ligue1": LIGUE1,
}


def get(key: str) -> LeagueConfig:
    if key not in ALL:
        raise KeyError(f"unknown league key {key!r}. Known: {sorted(ALL)}")
    return ALL[key]
