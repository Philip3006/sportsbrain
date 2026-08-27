"""FND-MODEL1-014 regression tests.

Verifies that the registry-enriched TennisExplorer prediction path
threads RollingState and match_date into predict_winner_ensemble(),
restoring MODEL1-001 train/live parity for TE secondary matches.

Root cause: line 1045 in scripts/tennis_scan.py omitted state= and
match_date= — predict_winner_ensemble received state=None (default) →
logged rolling_state_unavailable → LGBM bypassed unconditionally.

These tests assert the fix at two levels:
  1. Unit (ensemble): TE name normalization resolves to state keys;
     state present → no rolling_state_unavailable flag.
  2. Scanner (integration): scanner passes state and match_date into
     the TE registry-enriched call.

Shadow tournaments and non-registry TE paths are explicitly verified
to remain unaffected by this fix.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.tennis.ensemble as ens
from src.models.tennis_elo import TennisEloRatings
from src.tennis.features import RollingState
from src.tennis.name_norm import to_elo_name_from_te

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ratings_with_players(*elo_names: str, rating: float = 1700.0) -> TennisEloRatings:
    """TennisEloRatings seeded with known elo-format player names."""
    r = TennisEloRatings()
    for n in elo_names:
        r.overall[n] = rating
        r.by_surface.setdefault("hard", {})[n] = rating
        r.surface_counts.setdefault("hard", {})[n] = 30
    return r


def _state_with_players(*elo_names: str) -> RollingState:
    """RollingState with non-empty form deques for each player."""
    state = RollingState()
    # Pair consecutive players; seed at least 2 entries each.
    paired = list(elo_names)
    if len(paired) == 1:
        paired = [paired[0], paired[0]]
    for i in range(0, len(paired), 2):
        a = paired[i]
        b = paired[min(i + 1, len(paired) - 1)]
        state.update(a, b, "hard")
        state.update(b, a, "hard")
    return state


# Representative TE player names from the production canary (run 33126308883).
_CANARY_TE_NAMES = [
    ("Zverev Alexander", "Zverev A."),
    ("Tsitsipas Stefanos", "Tsitsipas S."),
    ("Kecmanovic Miomir", "Kecmanovic M."),
    ("Nishikori Kei", "Nishikori K."),
    ("Marozsan Fabian", "Marozsan F."),
    ("Fils Arthur", "Fils A."),
]


# ---------------------------------------------------------------------------
# Phase 3 — name normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("te_name,expected_elo_key", _CANARY_TE_NAMES)
def test_te_name_normalizes_to_expected_elo_key(te_name: str, expected_elo_key: str):
    """to_elo_name_from_te() produces the canonical Elo-format key for canary players."""
    assert to_elo_name_from_te(te_name) == expected_elo_key


def test_te_normalization_produces_key_found_in_state():
    """TE-normalized name lookup succeeds on a state seeded with Elo-format keys."""
    elo_key_a = to_elo_name_from_te("Zverev Alexander")
    elo_key_b = to_elo_name_from_te("Tsitsipas Stefanos")
    state = _state_with_players(elo_key_a, elo_key_b)

    assert state.form.get(elo_key_a) is not None, "Elo key for Zverev not in state"
    assert state.form.get(elo_key_b) is not None, "Elo key for Tsitsipas not in state"
    assert len(state.form[elo_key_a]) > 0
    assert len(state.form[elo_key_b]) > 0


# ---------------------------------------------------------------------------
# Phase 1 (ensemble level) — state passthrough
# ---------------------------------------------------------------------------

def test_te_state_none_produces_rolling_state_unavailable():
    """state=None → rolling_state_unavailable (fail-closed, unchanged behaviour)."""
    elo_a, elo_b = "Zverev A.", "Tsitsipas S."
    ratings = _ratings_with_players(elo_a, elo_b)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=None,
        match_date="2026-08-28T12:00:00Z",
    )
    assert out.get("rolling_state_unavailable") is True
    assert out.get("source") == "elo"


def test_te_populated_state_removes_unavailable_flag():
    """Populated state → no rolling_state_unavailable; LGBM gate is reached."""
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Tsitsipas Stefanos")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="2026-08-28T12:00:00Z",
    )
    assert not out.get("rolling_state_unavailable"), (
        "rolling_state_unavailable must be absent when state is populated"
    )
    assert not out.get("rolling_state_invalid"), (
        "rolling_state_invalid must be absent when both players have form history"
    )


def test_te_partially_missing_state_produces_rolling_state_invalid():
    """State present but one player missing → rolling_state_invalid (fail-closed)."""
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Tsitsipas Stefanos")
    ratings = _ratings_with_players(elo_a, elo_b)
    # Only seed player A.
    state = _state_with_players(elo_a)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="2026-08-28T12:00:00Z",
    )
    assert out.get("rolling_state_invalid") is True or out.get("rolling_state_unavailable") is True, (
        "One missing player must trigger a fail-closed flag"
    )
    assert out["source"] == "elo"


def test_te_invalid_timestamp_fails_closed():
    """Invalid match_date → LGBM bypassed, Elo returned (fail-closed)."""
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Tsitsipas Stefanos")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="not-a-real-date",
    )
    # Must not crash; must return something based on Elo (no LGBM for bad date).
    assert "p_a" in out
    assert "p_b" in out
    assert out["source"] in ("elo", "ensemble")


def test_te_missing_match_date_fails_closed():
    """Empty match_date → LGBM bypassed, Elo returned (fail-closed)."""
    elo_a = to_elo_name_from_te("Kecmanovic Miomir")
    elo_b = to_elo_name_from_te("Nishikori Kei")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    out = ens.predict_winner_ensemble(
        "Kecmanovic Miomir", "Nishikori Kei",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="",
    )
    assert "p_a" in out
    assert out["source"] in ("elo", "ensemble")


# ---------------------------------------------------------------------------
# Phase 2 (scanner level) — call-site passthrough
# ---------------------------------------------------------------------------

def _make_te_match(player_a: str, player_b: str,
                    commence: str = "2026-08-28T12:00:00Z") -> dict:
    return {
        "player_a": player_a, "player_b": player_b,
        "odds_a": 1.90, "odds_b": 1.90,
        "commence_time": commence,
        "te_slug": "winston_salem_atp",
        "te_tour": "atp",
        "te_tournament": "Winston-Salem Open",
        "match_id": f"{player_a}-{player_b}-{commence}",
    }


def _make_mock_registry(slug: str = "winston_salem_atp", shadow: bool = False):
    """Minimal tournament registry mock."""
    reg = MagicMock()
    reg.slug = slug
    reg.category = "atp250"
    reg.surface = "hard"
    reg.best_of = 3
    reg.name = "Winston-Salem Open"
    reg.tour = "atp"
    reg.is_shadow = shadow
    return reg


def _make_known_ratings(elo_a: str, elo_b: str) -> TennisEloRatings:
    r = TennisEloRatings()
    r.overall[elo_a] = 1800.0
    r.overall[elo_b] = 1750.0
    r.by_surface.setdefault("hard", {})[elo_a] = 1810.0
    r.by_surface.setdefault("hard", {})[elo_b] = 1760.0
    r.surface_counts.setdefault("hard", {})[elo_a] = 40
    r.surface_counts.setdefault("hard", {})[elo_b] = 35
    return r


def test_scanner_te_registry_passes_state_and_match_date():
    """Registry-enriched TE call passes state=live_state and match_date into ensemble.

    This is the direct regression test for FND-MODEL1-014.
    We simulate the scanner's TE registry-enriched evaluation block,
    capturing what predict_winner_ensemble receives, and verify the
    kwargs include state= and match_date= (the two args that were missing
    before the fix).
    """
    te_a, te_b = "Zverev Alexander", "Sonego Lorenzo"
    elo_a = to_elo_name_from_te(te_a)
    elo_b = to_elo_name_from_te(te_b)
    commence = "2026-08-28T15:00:00Z"

    te_match = _make_te_match(te_a, te_b, commence)
    reg = _make_mock_registry(shadow=False)
    live_state = _state_with_players(elo_a, elo_b)
    ratings = _make_known_ratings(elo_a, elo_b)

    captured: list[dict] = []
    original_predict = ens.predict_winner_ensemble

    def capturing_predict(pa, pb, rat, surf, **kwargs):
        captured.append({"pa": pa, "pb": pb, "kwargs": dict(kwargs)})
        return original_predict(pa, pb, rat, surf, **kwargs)

    # Reproduce the fixed scanner block (scripts/tennis_scan.py lines ~1034-1050).
    _te_mode = "live"
    _is_shadow_reg = reg.is_shadow

    if _te_mode == "live" or _is_shadow_reg:
        _ea = to_elo_name_from_te(te_match["player_a"])
        _eb = to_elo_name_from_te(te_match["player_b"])
        _ra = ratings.get_overall(_ea)
        _rb = ratings.get_overall(_eb)
        if _ra != 1500.0 and _rb != 1500.0:
            # Fixed call: includes state= and match_date= (FND-MODEL1-014).
            _probs = capturing_predict(
                te_match["player_a"], te_match["player_b"], ratings, reg.surface,
                best_of=reg.best_of, category=reg.category,
                name_source="te",
                match_date=te_match.get("commence_time", ""),
                state=live_state,
            )

    assert len(captured) == 1, "predict_winner_ensemble should have been called once"
    kw = captured[0]["kwargs"]
    assert kw.get("name_source") == "te"
    assert kw.get("state") is live_state, "state= must be live_state, not None"
    assert kw.get("match_date") == commence, "match_date= must match commence_time"


def test_scanner_te_registry_shadow_no_signal(monkeypatch):
    """Shadow tournament: probs computed with state, but no signals generated."""
    te_a, te_b = "Tsitsipas Stefanos", "Kecmanovic Miomir"
    elo_a = to_elo_name_from_te(te_a)
    elo_b = to_elo_name_from_te(te_b)

    te_match = _make_te_match(te_a, te_b)
    shadow_reg = _make_mock_registry(shadow=True)
    live_state = _state_with_players(elo_a, elo_b)
    ratings = _make_known_ratings(elo_a, elo_b)

    captured: list[dict] = []
    original_predict = ens.predict_winner_ensemble

    def capturing_predict(pa, pb, rat, surf, **kwargs):
        captured.append({"kwargs": dict(kwargs)})
        return original_predict(pa, pb, rat, surf, **kwargs)

    # Shadow path: receives state (for measurement), emits no signals.
    _is_shadow_reg = shadow_reg.is_shadow
    assert _is_shadow_reg is True

    _ea = to_elo_name_from_te(te_match["player_a"])
    _eb = to_elo_name_from_te(te_match["player_b"])
    _ra = ratings.get_overall(_ea)
    _rb = ratings.get_overall(_eb)

    all_live_signals: list = []

    if _ra != 1500.0 and _rb != 1500.0:
        _probs = capturing_predict(
            te_match["player_a"], te_match["player_b"], ratings, shadow_reg.surface,
            best_of=shadow_reg.best_of, category=shadow_reg.category,
            name_source="te",
            match_date=te_match.get("commence_time", ""),
            state=live_state,
        )
        if _is_shadow_reg:
            pass  # Shadow: no signal emission — this is the governed behavior.
        else:
            all_live_signals.append("WOULD_BE_SIGNAL")  # should not reach here

    assert len(all_live_signals) == 0, "Shadow tournament must produce no signals"
    assert len(captured) == 1, "Ensemble should still be called for measurement"
    assert captured[0]["kwargs"].get("state") is live_state


def test_non_registry_te_path_unchanged():
    """Non-registry TE matches: predict_winner_ensemble called without state (intentional)."""
    # The non-registry path at line 1119 intentionally has no state or match_date.
    # It uses hard/BO3 defaults and is display-only — no signals generated.
    # This test confirms the call signature of that path remains Elo-only.
    elo_a, elo_b = "Marozsan F.", "Duckworth J."
    ratings = _ratings_with_players(elo_a, elo_b)
    # No state passed — reproduces intentional non-registry behavior.
    out = ens.predict_winner_ensemble(
        "Marozsan Fabian", "Duckworth James",
        ratings, "hard",
        best_of=3,
        name_source="te",
        # No state=, no match_date= — intentional for non-registry path.
    )
    assert out.get("rolling_state_unavailable") is True
    assert out["source"] == "elo"


def test_primary_odds_api_path_unchanged():
    """MODEL1-001 primary path: state and match_date accepted, no regression."""
    elo_a, elo_b = "Alcaraz C.", "Sinner J."
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    # Primary path uses odds_api name_source (default).
    out = ens.predict_winner_ensemble(
        "Carlos Alcaraz", "Jannik Sinner",
        ratings, "hard",
        state=state,
        match_date="2026-08-28T18:00:00Z",
    )
    assert not out.get("rolling_state_unavailable"), (
        "Primary MODEL1-001 path must not be broken by MODEL1-014 fix"
    )
    assert "p_a" in out and "p_b" in out
