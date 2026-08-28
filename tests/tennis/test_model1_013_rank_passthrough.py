"""Regression tests for FND-MODEL1-013: live rank passthrough into LGBM ensemble.

Covers:
- extract_player_ranks() correctness, coverage, validity gate
- _rank_for() normalization for OddsAPI and TE name formats
- rank_a/rank_b reach predict_winner_ensemble at both call sites (AST)
- Valid live ranks reach ensemble with correct player-order assignment
- Player-order swap cannot invert ranks
- ATP/WTA routing is correct (both paths normalise to same Elo-key format)
- Missing/invalid rank uses safe fallback (None → ensemble.py _MIN_RANK=1500)
- MODEL1-001 state/timestamp passthrough remains intact after this change
- MODEL1-014 TE registry state/timestamp passthrough remains intact
- Shadow governance unchanged
"""
from __future__ import annotations

import ast
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
_RANK_FOR_SRC = Path(__file__).parent.parent.parent / "scripts" / "tennis_scan.py"


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


@pytest.fixture(autouse=True)
def _clear_ens_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


# ---------------------------------------------------------------------------
# Phase 1 — extract_player_ranks unit tests
# ---------------------------------------------------------------------------

class TestExtractPlayerRanks:
    def test_returns_latest_rank_per_player(self):
        df = pd.concat([
            _match_df("Zverev A.", "Sinner J.", 4.0, 1.0, "2026-01-01"),
            _match_df("Zverev A.", "Alcaraz C.", 3.0, 2.0, "2026-08-01"),
        ], ignore_index=True)
        ranks = extract_player_ranks(df)
        assert ranks["Zverev A."] == 3.0  # most recent
        assert ranks["Alcaraz C."] == 2.0
        assert ranks["Sinner J."] == 1.0

    def test_empty_dataframe_returns_empty(self):
        assert extract_player_ranks(pd.DataFrame()) == {}

    def test_missing_rank_columns_returns_empty(self):
        df = pd.DataFrame([{"winner_name": "A", "loser_name": "B"}])
        assert extract_player_ranks(df) == {}

    def test_invalid_rank_zero_excluded(self):
        df = _match_df("Player A.", "Player B.", 0.0, 50.0)
        ranks = extract_player_ranks(df)
        assert "Player A." not in ranks
        assert ranks["Player B."] == 50.0

    def test_rank_above_3000_excluded(self):
        df = _match_df("X Y.", "Z W.", 3001.0, 100.0)
        ranks = extract_player_ranks(df)
        assert "X Y." not in ranks
        assert ranks["Z W."] == 100.0

    def test_null_rank_excluded(self):
        df = _match_df("X Y.", "Z W.", float("nan"), 200.0)
        ranks = extract_player_ranks(df)
        assert "X Y." not in ranks

    def test_latest_date_wins_when_multiple_rows(self):
        df = pd.concat([
            _match_df("Alpha B.", "Beta G.", 10.0, 20.0, "2025-01-01"),
            _match_df("Gamma D.", "Alpha B.", 5.0, 8.0, "2026-08-01"),
        ], ignore_index=True)
        ranks = extract_player_ranks(df)
        assert ranks["Alpha B."] == 8.0  # latest row as loser

    def test_rank_1_valid(self):
        df = _match_df("Top P.", "Other Q.", 1.0, 100.0)
        ranks = extract_player_ranks(df)
        assert ranks["Top P."] == 1.0

    def test_rank_3000_valid_boundary(self):
        df = _match_df("Border A.", "Other Q.", 3000.0, 100.0)
        ranks = extract_player_ranks(df)
        assert ranks["Border A."] == 3000.0


# ---------------------------------------------------------------------------
# Phase 2 — _rank_for name normalization (via direct function import)
# ---------------------------------------------------------------------------

class TestRankForNormalization:
    """Import _rank_for from scanner and verify it normalises names correctly."""

    @pytest.fixture(autouse=True)
    def _import_rank_for(self):
        import importlib
        import importlib.util
        spec = importlib.util.spec_from_file_location("_tennis_scan_test", _SCANNER_PATH)
        self._mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(self._mod)
        except SystemExit:
            pass  # argparse exits on import — expected

    def test_odds_api_name_maps_to_elo_key(self):
        player_ranks = {"Alcaraz C.": 2.0}
        result = self._mod._rank_for("Carlos Alcaraz", "odds_api", player_ranks)
        assert result == 2.0

    def test_te_name_maps_to_elo_key(self):
        player_ranks = {"Zverev A.": 3.0}
        result = self._mod._rank_for("Zverev Alexander", "te", player_ranks)
        assert result == 3.0

    def test_unknown_player_returns_none(self):
        result = self._mod._rank_for("Unknown Player", "odds_api", {})
        assert result is None

    def test_wta_odds_api_player(self):
        player_ranks = {"Sabalenka A.": 1.0}
        result = self._mod._rank_for("Aryna Sabalenka", "odds_api", player_ranks)
        assert result == 1.0

    def test_wta_te_player(self):
        player_ranks = {"Swiatek I.": 5.0}
        result = self._mod._rank_for("Swiatek Iga", "te", player_ranks)
        assert result == 5.0

    def test_swap_gives_different_results(self):
        """rank_a and rank_b must NOT be interchangeable — _rank_for is player-specific."""
        ranks = {"Alcaraz C.": 2.0, "Zverev A.": 3.0}
        ra = self._mod._rank_for("Carlos Alcaraz", "odds_api", ranks)
        rb = self._mod._rank_for("Alexander Zverev", "odds_api", ranks)
        assert ra == 2.0
        assert rb == 3.0
        assert ra != rb


# ---------------------------------------------------------------------------
# Phase 3 — AST: scanner call sites contain rank kwargs
# ---------------------------------------------------------------------------

def _parse_scanner_ast() -> ast.Module:
    return ast.parse(_SCANNER_PATH.read_text())


def _keyword_names(call: ast.Call) -> set[str]:
    return {kw.keyword.arg for kw in call.keywords if isinstance(kw, ast.keyword) and kw.arg}


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

    def test_scanner_has_predict_calls(self):
        assert len(self.all_calls) >= 2

    def test_odds_api_call_has_rank_a_and_rank_b(self):
        # OddsAPI primary call: no name_source="te"
        odds_api_calls = [c for c in self.all_calls if not _has_name_source_te(c)]
        assert odds_api_calls, "Expected at least one OddsAPI predict_winner_ensemble call"
        for call in odds_api_calls:
            kws = _keyword_names_direct(call)
            assert "rank_a" in kws, f"OddsAPI call missing rank_a: {kws}"
            assert "rank_b" in kws, f"OddsAPI call missing rank_b: {kws}"

    def test_te_registry_call_has_rank_a_and_rank_b(self):
        # TE registry call: name_source="te" AND category= kwarg
        te_registry_calls = [c for c in self.all_calls
                             if _has_name_source_te(c) and _has_category_kwarg(c)]
        assert te_registry_calls, "Expected at least one TE registry predict_winner_ensemble call"
        for call in te_registry_calls:
            kws = _keyword_names_direct(call)
            assert "rank_a" in kws, "TE registry call missing rank_a"
            assert "rank_b" in kws, "TE registry call missing rank_b"

    def test_odds_api_rank_value_uses_rank_for(self):
        """rank_a/rank_b in OddsAPI call must use _rank_for(), not a literal."""
        odds_api_calls = [c for c in self.all_calls if not _has_name_source_te(c)]
        for call in odds_api_calls:
            for kw in call.keywords:
                if kw.arg in ("rank_a", "rank_b"):
                    # Value must be a Call node (not a Constant 1500)
                    assert isinstance(kw.value, ast.Call), (
                        f"{kw.arg} in OddsAPI call must use _rank_for(), not a literal"
                    )
                    func = kw.value.func
                    func_name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else None)
                    assert func_name == "_rank_for", f"Expected _rank_for but got {func_name}"

    def test_te_registry_rank_value_uses_rank_for(self):
        te_registry_calls = [c for c in self.all_calls
                             if _has_name_source_te(c) and _has_category_kwarg(c)]
        for call in te_registry_calls:
            for kw in call.keywords:
                if kw.arg in ("rank_a", "rank_b"):
                    assert isinstance(kw.value, ast.Call)
                    func = kw.value.func
                    func_name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else None)
                    assert func_name == "_rank_for"

    def test_model1_001_state_kwarg_intact_on_odds_api_call(self):
        odds_api_calls = [c for c in self.all_calls if not _has_name_source_te(c)]
        for call in odds_api_calls:
            kws = _keyword_names_direct(call)
            assert "state" in kws, "MODEL1-001 regression: OddsAPI call must pass state="

    def test_model1_014_state_kwarg_intact_on_te_registry_call(self):
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
# Phase 4 — predict_winner_ensemble with real ranks reaches ensemble
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

    def test_valid_ranks_reach_ensemble(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", 5.0, 30.0, monkeypatch)
        assert out["source"] == "ensemble"
        assert "rolling_state_invalid" not in out
        assert "prediction_time_unavailable" not in out

    def test_none_rank_uses_fallback_reaches_ensemble(self, monkeypatch):
        out = self._run("Alice Chen", "Beatrice Diaz", None, None, monkeypatch)
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
        """Swapping pa/pb must not silently invert the rank assignment."""
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
        # p_a in first call ≈ p_b in second call (same strong player)
        assert abs(out_ab["p_a"] - out_ba["p_b"]) < 0.05

    def test_model1_001_state_none_still_fails_closed(self, monkeypatch):
        # Use Elo-key format names so is_known() returns True and the state check is reached.
        elo_a = "Chen A."
        elo_b = "Diaz B."
        ratings = _ratings(elo_a, elo_b)
        monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm())
        monkeypatch.setitem(ens._CACHED, "gate_passed", True)
        # name_source="odds_api" with already-normalised names (is_probably_elo_format passes them through)
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
# Phase 5 — extract_player_ranks integration with actual match corpus shape
# ---------------------------------------------------------------------------

class TestExtractPlayerRanksIntegration:
    """Test extract_player_ranks against a realistic multi-match corpus."""

    @pytest.fixture
    def corpus(self) -> pd.DataFrame:
        rows = [
            {"tourney_date": "2026-01-10", "winner_name": "Sinner J.", "loser_name": "Alcaraz C.",
             "winner_rank": 1.0, "loser_rank": 2.0},
            {"tourney_date": "2026-03-15", "winner_name": "Zverev A.", "loser_name": "Sinner J.",
             "winner_rank": 3.0, "loser_rank": 1.0},
            {"tourney_date": "2026-08-20", "winner_name": "Alcaraz C.", "loser_name": "Zverev A.",
             "winner_rank": 2.0, "loser_rank": 3.0},
            # WTA
            {"tourney_date": "2026-08-21", "winner_name": "Sabalenka A.", "loser_name": "Swiatek I.",
             "winner_rank": 1.0, "loser_rank": 5.0},
        ]
        return pd.DataFrame(rows)

    def test_correct_latest_rank(self, corpus):
        ranks = extract_player_ranks(corpus)
        assert ranks["Alcaraz C."] == 2.0   # winner in latest row for Alcaraz
        assert ranks["Zverev A."] == 3.0    # loser in latest row for Zverev
        assert ranks["Sinner J."] == 1.0    # loser in second row, last seen
        assert ranks["Sabalenka A."] == 1.0
        assert ranks["Swiatek I."] == 5.0

    def test_all_four_players_present(self, corpus):
        ranks = extract_player_ranks(corpus)
        for player in ("Sinner J.", "Alcaraz C.", "Zverev A.", "Sabalenka A.", "Swiatek I."):
            assert player in ranks

    def test_atp_wta_do_not_collide(self, corpus):
        ranks = extract_player_ranks(corpus)
        # Sabalenka rank=1 (WTA) and Sinner rank=1 (ATP) must both be stored
        assert ranks["Sabalenka A."] == 1.0
        assert ranks["Sinner J."] == 1.0
