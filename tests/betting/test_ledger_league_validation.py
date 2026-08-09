"""
O1-4 Regression tests — BetSignal.league semantic validation in ledger.

Root cause (incident): ledger.py:314 used `or "wm2026"` as default when
BetSignal.league was unset. Tennis signals created by tennis_detector.py
never set league → all tennis bets received league="wm2026" (football league).

Fix:
  - BetSignal.league must be set before append_bets()
  - Invalid / empty league → logged ERROR, row SKIPPED (fail-safe, not crash)
  - Tennis detector sets league=tour on every returned signal
  - VALID_LEDGER_LEAGUES constant enforces domain constraint

These tests prove the bug cannot recur through the known code path.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from src.betting.value_detector import BetSignal
from src.config import VALID_LEDGER_LEAGUES


def _minimal_signal(market: str = "home", league: str = "") -> BetSignal:
    return BetSignal(
        match_id="test_match_001",
        home="Player A",
        away="Player B",
        market=market,
        model_prob=0.55,
        fair_prob=0.50,
        decimal_odds=1.95,
        ev=0.07,
        kelly_f=0.10,
        stake_pct=0.02,
        stake_eur=2.0,
        confidence="MEDIUM",
        league=league,
    )


def _run_append(signal: BetSignal, bankroll: float = 100.0) -> tuple[int, pd.DataFrame]:
    """Append signal to a temp ledger (non-existent path → _load starts empty)."""
    from src.betting.ledger import append_bets
    import uuid
    tmp = Path(tempfile.gettempdir()) / f"ledger_test_{uuid.uuid4().hex}.csv"
    try:
        n = append_bets([signal], bankroll, path=tmp)
        df = pd.read_csv(tmp, dtype=str) if tmp.exists() and tmp.stat().st_size > 0 \
            else pd.DataFrame(columns=["league"])
        return n, df
    finally:
        tmp.unlink(missing_ok=True)


# ── 1. Valid ATP tennis bet ──────────────────────────────────────────────────

class TestValidATPBet:
    def test_atp_bet_appended_with_correct_league(self):
        sig = _minimal_signal(league="atp")
        n, df = _run_append(sig)
        assert n == 1, "Valid ATP bet must be appended"
        assert df.iloc[0]["league"] == "atp"

    def test_atp_in_valid_leagues_constant(self):
        assert "atp" in VALID_LEDGER_LEAGUES


# ── 2. Valid WTA tennis bet ──────────────────────────────────────────────────

class TestValidWTABet:
    def test_wta_bet_appended_with_correct_league(self):
        sig = _minimal_signal(league="wta")
        n, df = _run_append(sig)
        assert n == 1, "Valid WTA bet must be appended"
        assert df.iloc[0]["league"] == "wta"

    def test_wta_in_valid_leagues_constant(self):
        assert "wta" in VALID_LEDGER_LEAGUES


# ── 3. Valid football league bet ─────────────────────────────────────────────

class TestValidFootballLeagueBet:
    @pytest.mark.parametrize("league", ["wm2026", "bl2"])
    def test_football_league_appended(self, league):
        sig = _minimal_signal(market="home", league=league)
        sig.home = "Germany"
        sig.away = "Brazil"
        n, df = _run_append(sig)
        assert n == 1, f"Valid football bet ({league}) must be appended"
        assert df.iloc[0]["league"] == league

    def test_football_leagues_in_valid_constant(self):
        assert "wm2026" in VALID_LEDGER_LEAGUES
        assert "bl2" in VALID_LEDGER_LEAGUES


# ── 4. Missing tennis league (the O1-4 bug reproduction) ────────────────────

class TestMissingTennisLeague:
    def test_empty_league_skipped_not_corrupted(self, caplog):
        """BetSignal with empty league must be SKIPPED, not written with wm2026."""
        sig = _minimal_signal(league="")  # empty — the old bug
        with caplog.at_level(logging.ERROR, logger="sportsbrain.betting.ledger"):
            n, df = _run_append(sig)
        assert n == 0, "Signal with empty league must be skipped"
        assert len(df) == 0 or "wm2026" not in df.get("league", pd.Series([])).values

    def test_error_logged_for_missing_league(self, caplog):
        sig = _minimal_signal(league="")
        with caplog.at_level(logging.ERROR, logger="sportsbrain.betting.ledger"):
            _run_append(sig)
        assert any("invalid league" in r.message.lower() for r in caplog.records), \
            "Missing league must produce an ERROR log entry"

    def test_old_default_wm2026_cannot_corrupt_tennis_bet(self):
        """Regression: the old code path `or 'wm2026'` must not exist anymore."""
        import ast, pathlib
        src = pathlib.Path(__file__).resolve().parents[2] / "src" / "betting" / "ledger.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                for val in node.values:
                    if isinstance(val, ast.Constant) and val.value == "wm2026":
                        pytest.fail(
                            "O1-4 regression: `or 'wm2026'` default found in ledger.py. "
                            "This was the root cause of tennis bets receiving wm2026 league."
                        )


# ── 5. Incompatible sport/league combination ─────────────────────────────────

class TestIncompatibleSportLeague:
    def test_unknown_league_code_is_rejected(self, caplog):
        """A signal with a made-up or wrong league must be rejected."""
        sig = _minimal_signal(league="soccer_fifa_world_cup")  # full key, not short code
        with caplog.at_level(logging.ERROR, logger="sportsbrain.betting.ledger"):
            n, df = _run_append(sig)
        assert n == 0, "Signal with invalid league code must be skipped"

    def test_none_league_rejected(self, caplog):
        sig = _minimal_signal(league="")
        sig.league = None  # type: ignore[assignment]
        with caplog.at_level(logging.ERROR, logger="sportsbrain.betting.ledger"):
            n, _ = _run_append(sig)
        assert n == 0


# ── 6. Legacy/default behavior that previously produced the corruption ───────

class TestLegacyDefaultRegression:
    def test_tennis_detector_signal_sets_league_atp(self):
        """detect_value_tennis() must set league='atp' on returned signals."""
        from src.betting.tennis_detector import detect_value_tennis
        signals = detect_value_tennis(
            player_a="Novak Djokovic",
            player_b="Carlos Alcaraz",
            probs={"p_a": 0.60, "p_b": 0.40},
            odds_a=1.60,
            odds_b=2.50,
            bankroll=100.0,
            match_id="test_djokovic_alcaraz",
            tour="atp",
        )
        for s in signals:
            assert s.league == "atp", (
                f"detect_value_tennis(tour='atp') must set league='atp', got {s.league!r}"
            )

    def test_tennis_detector_signal_sets_league_wta(self):
        """detect_value_tennis() must set league='wta' on returned signals."""
        from src.betting.tennis_detector import detect_value_tennis
        signals = detect_value_tennis(
            player_a="Iga Swiatek",
            player_b="Aryna Sabalenka",
            probs={"p_a": 0.55, "p_b": 0.45},
            odds_a=1.75,
            odds_b=2.20,
            bankroll=100.0,
            match_id="test_swiatek_sabalenka",
            tour="wta",
        )
        for s in signals:
            assert s.league == "wta", (
                f"detect_value_tennis(tour='wta') must set league='wta', got {s.league!r}"
            )

    def test_detect_total_games_sets_league(self):
        """detect_total_games() must propagate tour to league."""
        from src.betting.tennis_detector import detect_total_games
        sigs = detect_total_games(
            player_a="Djokovic",
            player_b="Alcaraz",
            p_match_a=0.55,
            odds_over=1.90,
            odds_under=2.00,
            line=22.5,
            best_of=3,
            bankroll=100.0,
            match_id="test_tg",
            tour="atp",
        )
        for s in sigs:
            assert s.league == "atp"
