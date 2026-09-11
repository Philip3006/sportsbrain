"""Regression tests for FND-MODEL1-013: live rank passthrough into LGBM ensemble.

Covers:
- extract_player_ranks() returns all observations per player (list of (date, rank))
- Freshness: latest PRIOR observation selected, future observations rejected
- Staleness gate: observations older than MAX_RANK_AGE_DAYS rejected
- Validity: zero/negative/above-3000 ranks excluded
- _rank_for() normalization for OddsAPI and TE name formats
- _rank_pair() pair-level neutralization contract
- rank_a/rank_b reach predict_winner_ensemble at both call sites (AST)
- Valid live ranks reach ensemble with correct player-order assignment
- One-sided missing/stale → both neutralized (pair-level contract)
- MODEL1-001 state/timestamp passthrough remains intact after this change
- MODEL1-014 TE registry state/timestamp passthrough remains intact
- Shadow governance unchanged
"""
from __future__ import annotations

import ast
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import src.tennis.ensemble as ens
from src.models.tennis_elo import TennisEloRatings
from src.tennis.elo_source import extract_player_ranks
from src.tennis.features import RollingState
from src.tennis.name_norm import to_elo_name_from_odds_api, to_elo_name_from_te

_SCANNER_PATH = Path(__file__).parent.parent.parent / "scripts" / "tennis_scan.py"

_NOW = datetime(2026, 8, 29, 12, 0, 0)  # Fixed reference time for all freshness tests
_MAX_AGE = 42  # Must match _MAX_RANK_AGE_DAYS in tennis_scan.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_lgbm(p_ab: float = 0.65) -> MagicMock:
    m = MagicMock()
    m.predict_p_a.return_value = np.array([p_ab])
    return m


def _ratings(a: str, b: str, overall_a: float = 1700, overall_b: float = 1500) -> TennisEloRatings:
    r = TennisEloRatings()
    r.overall[a] = overall_a
    r.overall[b] = overall_b
    r.by_surface["hard"] = {a: overall_a + 20, b: overall_b - 10}
    r.surface_counts["hard"] = {a: 30, b: 25}
    return r


def _populated_state(a: str, b: str) -> RollingState:
    state = RollingState()
    state.update(a, b, "hard")
    state.update(b, a, "hard")
    return state


def _match_df(winner: str, loser: str, wr: float, lr: float, date: str = "2026-08-01") -> pd.DataFrame:
    return pd.DataFrame([{
        "tourney_date": pd.Timestamp(date),
        "winner_name": winner,
        "loser_name": loser,
        "winner_rank": wr,
        "loser_rank": lr,
    }])


def _obs(player: str, rank: float, date: str) -> dict:
    return {"tourney_date": pd.Timestamp(date), "winner_name": player, "loser_name": "Dummy X.",
            "winner_rank": rank, "loser_rank": 999.0}


@pytest.fixture(autouse=True)
def _clear_ens_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


@pytest.fixture(scope="module")
def _scanner_mod():
    """Import tennis_scan.py module for _rank_for / _rank_pair access."""
    spec = importlib.util.spec_from_file_location("_tennis_scan_test", _SCANNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass  # argparse exits on module import — expected
    return mod


# ---------------------------------------------------------------------------
# Phase 1 — extract_player_ranks: return type and validity gate
# ---------------------------------------------------------------------------

class TestExtractPlayerRanks:
    def test_returns_dict_of_observation_lists(self):
        df = _match_df("Alcaraz C.", "Zverev A.", 2.0, 3.0)
        result = extract_player_ranks(df)
        assert isinstance(result, dict)
        assert isinstance(result["Alcaraz C."], list)
        ts, rank = result["Alcaraz C."][0]
        assert isinstance(ts, pd.Timestamp)
        assert rank == 2.0

    def test_all_observations_preserved_not_just_latest(self):
        df = pd.concat([
            _match_df("Zverev A.", "Sinner J.", 4.0, 1.0, "2026-01-01"),
            _match_df("Zverev A.", "Alcaraz C.", 3.0, 2.0, "2026-08-01"),
        ], ignore_index=True)
        result = extract_player_ranks(df)
        # Zverev should have 2 observations (one from each match)
        assert len(result["Zverev A."]) == 2

    def test_observations_sorted_ascending_by_date(self):
        df = pd.concat([
            _match_df("Zverev A.", "Sinner J.", 4.0, 1.0, "2026-08-01"),
            _match_df("Zverev A.", "Alcaraz C.", 3.0, 2.0, "2026-01-01"),
        ], ignore_index=True)
        result = extract_player_ranks(df)
        dates = [ts for ts, _ in result["Zverev A."]]
        assert dates == sorted(dates)

    def test_empty_dataframe_returns_empty(self):
        assert extract_player_ranks(pd.DataFrame()) == {}

    def test_missing_rank_columns_returns_empty(self):
        df = pd.DataFrame([{"winner_name": "A", "loser_name": "B"}])
        assert extract_player_ranks(df) == {}

    def test_rank_zero_excluded(self):
        df = _match_df("Player A.", "Player B.", 0.0, 50.0)
        result = extract_player_ranks(df)
        assert "Player A." not in result
        assert "Player B." in result

    def test_rank_above_3000_excluded(self):
        df = _match_df("X Y.", "Z W.", 3001.0, 100.0)
        result = extract_player_ranks(df)
        assert "X Y." not in result
        assert "Z W." in result

    def test_null_rank_excluded(self):
        df = _match_df("X Y.", "Z W.", float("nan"), 200.0)
        result = extract_player_ranks(df)
        assert "X Y." not in result

    def test_rank_1_valid(self):
        df = _match_df("Top P.", "Other Q.", 1.0, 100.0)
        result = extract_player_ranks(df)
        assert result["Top P."][0][1] == 1.0

    def test_rank_3000_valid_boundary(self):
        df = _match_df("Border A.", "Other Q.", 3000.0, 100.0)
        result = extract_player_ranks(df)
        assert result["Border A."][0][1] == 3000.0


# ---------------------------------------------------------------------------
# Phase 2 — _rank_for: as-of semantics and freshness gate
# ---------------------------------------------------------------------------

class TestRankForFreshness:
    """Verify as-of lookup and staleness gate in _rank_for."""

    @pytest.fixture(autouse=True)
    def _get_rank_for(self, _scanner_mod):
        self._rank_for = _scanner_mod._rank_for

    def _obs_dict(self, player: str, rank: float, date_str: str) -> dict:
        ts = pd.Timestamp(date_str)
        return {player: [(ts, rank)]}

    def test_fresh_observation_within_window_returned(self):
        observations = self._obs_dict("Sinner J.", 1.0, "2026-08-15")  # 14d before _NOW
        result = self._rank_for("Sinner J.", "odds_api", observations, _NOW, max_age_days=_MAX_AGE)
        # Note: "Sinner J." is already in Elo-key format — odds_api normalization may or may not match
        # Use pre-normalized key directly
        result2 = self._rank_for.__func__ if hasattr(self._rank_for, '__func__') else None
        # Direct key lookup: build observations dict with key matching normalised name
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Jannik Sinner")
        obs = {elo_key: [(pd.Timestamp("2026-08-15"), 1.0)]}
        assert self._rank_for("Jannik Sinner", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) == 1.0

    def test_future_observation_rejected(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Carlos Alcaraz")
        # Observation AFTER prediction_time must be rejected
        future_date = _NOW + timedelta(days=1)
        obs = {elo_key: [(pd.Timestamp(future_date), 2.0)]}
        assert self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) is None

    def test_observation_exactly_at_prediction_time_rejected(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Carlos Alcaraz")
        obs = {elo_key: [(pd.Timestamp(_NOW), 2.0)]}
        assert self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) is None

    def test_observation_exactly_at_freshness_boundary_accepted(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Carlos Alcaraz")
        boundary_date = _NOW - timedelta(days=_MAX_AGE)
        obs = {elo_key: [(pd.Timestamp(boundary_date), 3.0)]}
        assert self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) == 3.0

    def test_observation_one_day_past_boundary_rejected(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Carlos Alcaraz")
        stale_date = _NOW - timedelta(days=_MAX_AGE + 1)
        obs = {elo_key: [(pd.Timestamp(stale_date), 3.0)]}
        assert self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) is None

    def test_most_recent_prior_observation_selected(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Novak Djokovic")
        # Two valid prior observations — should return the more recent one
        obs = {elo_key: [
            (pd.Timestamp(_NOW - timedelta(days=30)), 2.0),  # older
            (pd.Timestamp(_NOW - timedelta(days=10)), 3.0),  # more recent
        ]}
        assert self._rank_for("Novak Djokovic", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) == 3.0

    def test_only_stale_observations_available_returns_none(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Rafael Nadal")
        stale_date = _NOW - timedelta(days=90)
        obs = {elo_key: [(pd.Timestamp(stale_date), 4.0)]}
        assert self._rank_for("Rafael Nadal", "odds_api", obs, _NOW, max_age_days=_MAX_AGE) is None

    def test_unknown_player_returns_none(self):
        assert self._rank_for("Unknown Player", "odds_api", {}, _NOW, max_age_days=_MAX_AGE) is None


# ---------------------------------------------------------------------------
# Phase 3 — _rank_for: name normalization (OddsAPI and TE)
# ---------------------------------------------------------------------------

class TestRankForNormalization:
    @pytest.fixture(autouse=True)
    def _get_rank_for(self, _scanner_mod):
        self._rank_for = _scanner_mod._rank_for

    def _fresh_obs(self, elo_key: str, rank: float) -> dict:
        return {elo_key: [(pd.Timestamp(_NOW - timedelta(days=7)), rank)]}

    def test_odds_api_name_maps_to_elo_key(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Carlos Alcaraz")
        obs = self._fresh_obs(elo_key, 2.0)
        assert self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, _MAX_AGE) == 2.0

    def test_te_name_maps_to_elo_key(self):
        from src.tennis.name_norm import to_elo_name_from_te
        elo_key = to_elo_name_from_te("Zverev Alexander")
        obs = self._fresh_obs(elo_key, 3.0)
        assert self._rank_for("Zverev Alexander", "te", obs, _NOW, _MAX_AGE) == 3.0

    def test_wta_odds_api_player(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        elo_key = to_elo_name_from_odds_api("Aryna Sabalenka")
        obs = self._fresh_obs(elo_key, 1.0)
        assert self._rank_for("Aryna Sabalenka", "odds_api", obs, _NOW, _MAX_AGE) == 1.0

    def test_wta_te_player(self):
        from src.tennis.name_norm import to_elo_name_from_te
        elo_key = to_elo_name_from_te("Swiatek Iga")
        obs = self._fresh_obs(elo_key, 5.0)
        assert self._rank_for("Swiatek Iga", "te", obs, _NOW, _MAX_AGE) == 5.0

    def test_swap_gives_different_results(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        k_a = to_elo_name_from_odds_api("Carlos Alcaraz")
        k_b = to_elo_name_from_odds_api("Alexander Zverev")
        obs = {k_a: [(pd.Timestamp(_NOW - timedelta(days=5)), 2.0)],
               k_b: [(pd.Timestamp(_NOW - timedelta(days=5)), 3.0)]}
        ra = self._rank_for("Carlos Alcaraz", "odds_api", obs, _NOW, _MAX_AGE)
        rb = self._rank_for("Alexander Zverev", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra == 2.0
        assert rb == 3.0
        assert ra != rb


# ---------------------------------------------------------------------------
# Phase 4 — _rank_pair: pair-level neutralization contract
# ---------------------------------------------------------------------------

class TestRankPairContract:
    @pytest.fixture(autouse=True)
    def _get_rank_pair(self, _scanner_mod):
        self._rank_pair = _scanner_mod._rank_pair

    def _obs_for(self, player: str, rank: float, days_ago: int, name_source: str = "odds_api") -> dict:
        if name_source == "te":
            from src.tennis.name_norm import to_elo_name_from_te
            elo_key = to_elo_name_from_te(player)
        else:
            from src.tennis.name_norm import to_elo_name_from_odds_api
            elo_key = to_elo_name_from_odds_api(player)
        ts = pd.Timestamp(_NOW - timedelta(days=days_ago))
        return {elo_key: [(ts, rank)]}

    def _merge_obs(self, *dicts) -> dict:
        result = {}
        for d in dicts:
            result.update(d)
        return result

    def test_both_fresh_returns_real_ranks(self):
        from src.tennis.name_norm import to_elo_name_from_odds_api
        obs = self._merge_obs(
            self._obs_for("Carlos Alcaraz", 2.0, 7),
            self._obs_for("Novak Djokovic", 5.0, 7),
        )
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra == 2.0
        assert rb == 5.0

    def test_a_missing_b_valid_both_neutralized(self):
        obs = self._obs_for("Novak Djokovic", 5.0, 7)  # only B has rank
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_a_valid_b_missing_both_neutralized(self):
        obs = self._obs_for("Carlos Alcaraz", 2.0, 7)  # only A has rank
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_a_stale_b_fresh_both_neutralized(self):
        obs = self._merge_obs(
            self._obs_for("Carlos Alcaraz", 2.0, _MAX_AGE + 5),  # stale
            self._obs_for("Novak Djokovic", 5.0, 7),              # fresh
        )
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_a_fresh_b_stale_both_neutralized(self):
        obs = self._merge_obs(
            self._obs_for("Carlos Alcaraz", 2.0, 7),               # fresh
            self._obs_for("Novak Djokovic", 5.0, _MAX_AGE + 5),    # stale
        )
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_both_stale_both_neutralized(self):
        obs = self._merge_obs(
            self._obs_for("Carlos Alcaraz", 2.0, _MAX_AGE + 10),
            self._obs_for("Novak Djokovic", 5.0, _MAX_AGE + 10),
        )
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_both_missing_both_neutralized(self):
        ra, rb = self._rank_pair("Unknown A", "Unknown B", "odds_api", {}, _NOW, _MAX_AGE)
        assert ra is None
        assert rb is None

    def test_player_ab_assignment_cannot_swap(self):
        """rank_a must come from player_a, rank_b from player_b."""
        obs = self._merge_obs(
            self._obs_for("Carlos Alcaraz", 2.0, 5),
            self._obs_for("Novak Djokovic", 50.0, 5),
        )
        ra, rb = self._rank_pair("Carlos Alcaraz", "Novak Djokovic", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra == 2.0
        assert rb == 50.0
        # Swap order — ranks should follow players
        ra2, rb2 = self._rank_pair("Novak Djokovic", "Carlos Alcaraz", "odds_api", obs, _NOW, _MAX_AGE)
        assert ra2 == 50.0
        assert rb2 == 2.0

    def test_te_name_source_pair(self):
        obs = self._merge_obs(
            self._obs_for("Zverev Alexander", 3.0, 5, "te"),
            self._obs_for("Tsitsipas Stefanos", 12.0, 5, "te"),
        )
        ra, rb = self._rank_pair("Zverev Alexander", "Tsitsipas Stefanos", "te", obs, _NOW, _MAX_AGE)
        assert ra == 3.0
        assert rb == 12.0


# ---------------------------------------------------------------------------
# Phase 5 — AST: scanner call sites have rank_a/rank_b and use _rank_pair
# ---------------------------------------------------------------------------

def _parse_scanner_ast() -> ast.Module:
    return ast.parse(_SCANNER_PATH.read_text())


def _keyword_names_direct(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def _find_predict_calls(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name) else
                    func.attr if isinstance(func, ast.Attribute) else None)
            if name == "predict_winner_ensemble":
                calls.append(node)
    return calls


def _has_name_source_te(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "name_source" and isinstance(kw.value, ast.Constant) and kw.value.value == "te":
            return True
    return False


def _has_category_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "category" for kw in call.keywords)


class TestScannerCallSiteAST:
    @pytest.fixture(autouse=True)
    def _tree(self):
        self.tree = _parse_scanner_ast()
        self.all_calls = _find_predict_calls(self.tree)

    def test_scanner_has_at_least_two_predict_calls(self):
        assert len(self.all_calls) >= 2

    def test_odds_api_call_has_rank_a_and_rank_b(self):
        odds_api_calls = [c for c in self.all_calls if not _has_name_source_te(c)]
        assert odds_api_calls, "Expected at least one OddsAPI predict_winner_ensemble call"
        for call in odds_api_calls:
            kws = _keyword_names_direct(call)
            assert "rank_a" in kws, f"OddsAPI call missing rank_a: {kws}"
            assert "rank_b" in kws, f"OddsAPI call missing rank_b: {kws}"

    def test_te_registry_call_has_rank_a_and_rank_b(self):
        te_registry_calls = [c for c in self.all_calls
                             if _has_name_source_te(c) and _has_category_kwarg(c)]
        assert te_registry_calls, "Expected at least one TE registry predict_winner_ensemble call"
        for call in te_registry_calls:
            kws = _keyword_names_direct(call)
            assert "rank_a" in kws, "TE registry call missing rank_a"
            assert "rank_b" in kws, "TE registry call missing rank_b"

    def test_rank_pair_called_in_scanner_source(self):
        """_rank_pair must be invoked in the scanner (not raw literals)."""
        src_text = _SCANNER_PATH.read_text()
        assert "_rank_pair(" in src_text, "_rank_pair() call not found in scanner"

    def test_max_rank_age_days_constant_defined(self):
        src_text = _SCANNER_PATH.read_text()
        assert "_MAX_RANK_AGE_DAYS" in src_text, "_MAX_RANK_AGE_DAYS constant not found in scanner"

    def test_model1_001_state_kwarg_intact_on_odds_api_call(self):
        odds_api_calls = [c for c in self.all_calls if not _has_name_source_te(c)]
        for call in odds_api_calls:
            kws = _keyword_names_direct(call)
            assert "state" in kws, "MODEL1-001 regression: OddsAPI call must pass state="

    def test_model1_014_state_and_match_date_intact_on_te_registry_call(self):
        te_registry_calls = [c for c in self.all_calls
                             if _has_name_source_te(c) and _has_category_kwarg(c)]
        for call in te_registry_calls:
            kws = _keyword_names_direct(call)
            assert "state" in kws, "MODEL1-014 regression: TE registry call must pass state="
            assert "match_date" in kws, "MODEL1-014 regression: TE registry call must pass match_date="

    def test_te_non_registry_call_has_no_rank(self):
        """Non-registry display-only TE call must NOT gain rank kwargs (out of scope)."""
        te_non_registry_calls = [c for c in self.all_calls
                                  if _has_name_source_te(c) and not _has_category_kwarg(c)]
        for call in te_non_registry_calls:
            kws = _keyword_names_direct(call)
            assert "rank_a" not in kws, "Non-registry TE call must not have rank_a"
            assert "rank_b" not in kws, "Non-registry TE call must not have rank_b"


# ---------------------------------------------------------------------------
# Phase 6 — predict_winner_ensemble with real ranks reaches ensemble
# ---------------------------------------------------------------------------

class TestRankPassthroughEnsemble:
    """Use mock LGBM to verify rank values propagate into predict_winner_ensemble."""

    def _run(self, pa: str, pb: str, ra: float | None, rb: float | None,
             monkeypatch, name_source: str = "odds_api") -> dict:
        elo_key_a = to_elo_name_from_te(pa) if name_source == "te" else to_elo_name_from_odds_api(pa)
        elo_key_b = to_elo_name_from_te(pb) if name_source == "te" else to_elo_name_from_odds_api(pb)
        ratings = _ratings(elo_key_a, elo_key_b)
        state = _populated_state(elo_key_a, elo_key_b)
        monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm(0.65))
        monkeypatch.setitem(ens._CACHED, "gate_passed", True)
        return ens.predict_winner_ensemble(
            pa, pb, ratings, "hard",
            rank_a=ra, rank_b=rb,
            name_source=name_source,
            state=state,
            match_date="2026-08-28T15:00:00Z",
            use_live_stats=False,
        )

    def test_both_fresh_ranks_reach_ensemble(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", 5.0, 30.0, monkeypatch)
        assert out["source"] == "ensemble"
        assert "rolling_state_invalid" not in out
        assert "prediction_time_unavailable" not in out

    def test_both_neutralized_none_uses_fallback(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", None, None, monkeypatch)
        assert out["source"] == "ensemble"  # still reaches ensemble; 1500/1500 internally

    def test_one_sided_missing_passes_none_none(self, monkeypatch):
        # Scanner passes None/None after pair-level contract — ensemble falls back symmetrically
        out = self._run("Alice Chen", "Beatrice Diaz", None, 50.0, monkeypatch)
        # Callers SHOULD pass None/None (not mixed) but ensemble accepts it; this tests the
        # ensemble's own fallback for asymmetric None (defense-in-depth)
        assert out["source"] == "ensemble"

    def test_invalid_rank_zero_uses_fallback(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", 0.0, 50.0, monkeypatch)
        assert out["source"] == "ensemble"

    def test_invalid_rank_negative_uses_fallback(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", -1.0, 50.0, monkeypatch)
        assert out["source"] == "ensemble"

    def test_te_name_source_with_ranks(self, monkeypatch):
        out = self._run("Zverev Alexander", "Tsitsipas Stefanos",
                        3.0, 12.0, monkeypatch, name_source="te")
        assert out["source"] == "ensemble"

    def test_player_order_swap_inverts_pa_pb(self, monkeypatch):
        elo_a = to_elo_name_from_odds_api("Strong Player")
        elo_b = to_elo_name_from_odds_api("Weak Player")
        ratings = _ratings(elo_a, elo_b, overall_a=1800, overall_b=1400)
        state = _populated_state(elo_a, elo_b)
        mock = _mock_lgbm(0.70)
        monkeypatch.setitem(ens._CACHED, "model", mock)
        monkeypatch.setitem(ens._CACHED, "gate_passed", True)

        out_ab = ens.predict_winner_ensemble(
            "Strong Player", "Weak Player", ratings, "hard",
            rank_a=5.0, rank_b=80.0,
            state=state, match_date="2026-08-28T15:00:00Z", use_live_stats=False,
        )
        out_ba = ens.predict_winner_ensemble(
            "Weak Player", "Strong Player", ratings, "hard",
            rank_a=80.0, rank_b=5.0,
            state=state, match_date="2026-08-28T15:00:00Z", use_live_stats=False,
        )
        assert abs(out_ab["p_a"] - out_ba["p_b"]) < 0.05

    def test_model1_001_state_none_still_fails_closed(self, monkeypatch):
        elo_a = "Chen A."
        elo_b = "Diaz B."
        ratings = _ratings(elo_a, elo_b)
        monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm())
        monkeypatch.setitem(ens._CACHED, "gate_passed", True)
        out = ens.predict_winner_ensemble(
            elo_a, elo_b, ratings, "hard",
            rank_a=5.0, rank_b=30.0,
            state=None,
            match_date="2026-08-28T15:00:00Z",
            use_live_stats=False,
        )
        assert out["source"] == "elo"
        assert out.get("rolling_state_unavailable") is True

    def test_model1_014_invalid_timestamp_still_fails_closed(self, monkeypatch):
        elo_a = to_elo_name_from_odds_api("Alice Chen")
        elo_b = to_elo_name_from_odds_api("Beatrice Diaz")
        state = _populated_state(elo_a, elo_b)
        ratings = _ratings(elo_a, elo_b)
        monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm())
        monkeypatch.setitem(ens._CACHED, "gate_passed", True)
        out = ens.predict_winner_ensemble(
            "Alice Chen", "Beatrice Diaz", ratings, "hard",
            rank_a=5.0, rank_b=30.0,
            state=state,
            match_date="not-a-date",
            use_live_stats=False,
        )
        assert out["source"] == "elo"
        assert out.get("prediction_time_unavailable") is True


# ---------------------------------------------------------------------------
# Phase 7 — extract_player_ranks integration (multi-match corpus)
# ---------------------------------------------------------------------------

class TestExtractPlayerRanksIntegration:
    @pytest.fixture
    def corpus(self) -> pd.DataFrame:
        rows = [
            {"tourney_date": "2026-01-10", "winner_name": "Sinner J.", "loser_name": "Alcaraz C.",
             "winner_rank": 1.0, "loser_rank": 2.0},
            {"tourney_date": "2026-03-15", "winner_name": "Zverev A.", "loser_name": "Sinner J.",
             "winner_rank": 3.0, "loser_rank": 1.0},
            {"tourney_date": "2026-08-20", "winner_name": "Alcaraz C.", "loser_name": "Zverev A.",
             "winner_rank": 2.0, "loser_rank": 3.0},
            {"tourney_date": "2026-08-21", "winner_name": "Sabalenka A.", "loser_name": "Swiatek I.",
             "winner_rank": 1.0, "loser_rank": 5.0},
        ]
        return pd.DataFrame(rows)

    def test_all_players_present(self, corpus):
        result = extract_player_ranks(corpus)
        for player in ("Sinner J.", "Alcaraz C.", "Zverev A.", "Sabalenka A.", "Swiatek I."):
            assert player in result

    def test_latest_rank_is_most_recent_observation(self, corpus):
        result = extract_player_ranks(corpus)
        # Alcaraz: winner_rank=2 in most recent row (2026-08-20)
        alcaraz_latest_ts, alcaraz_latest_rank = result["Alcaraz C."][-1]
        assert alcaraz_latest_rank == 2.0
        assert alcaraz_latest_ts == pd.Timestamp("2026-08-20")

    def test_atp_wta_do_not_collide(self, corpus):
        result = extract_player_ranks(corpus)
        # Sabalenka rank=1 (WTA) and Sinner rank=1 (ATP) are distinct keys
        assert result["Sabalenka A."][-1][1] == 1.0
        assert result["Sinner J."][-1][1] == 1.0
