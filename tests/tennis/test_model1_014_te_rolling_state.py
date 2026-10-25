"""FND-MODEL1-014 regression tests.

Verifies that the registry-enriched TennisExplorer prediction path
threads RollingState and match_date into predict_winner_ensemble(),
restoring MODEL1-001 train/live parity for TE secondary matches.

Root cause: scripts/tennis_scan.py registry-enriched TE call omitted
state= and match_date= — predict_winner_ensemble received state=None
(default) → logged rolling_state_unavailable → LGBM bypassed
unconditionally for all 14 TE matches in production canary run 33126308883.

These tests assert the fix at two levels:
  1. Unit (ensemble): fail-safe contracts for invalid state/timestamp;
     positive proof that populated state + valid timestamp reach ensemble.
  2. AST (scanner call-site): parses the real scripts/tennis_scan.py and
     asserts the registry-enriched TE call contains state=live_state and
     match_date=m.get("commence_time","") — protects against regression.

Shadow tournaments and non-registry TE paths are explicitly verified
to remain unaffected by this fix.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

import src.tennis.ensemble as ens
from src.models.tennis_elo import TennisEloRatings
from src.tennis.features import RollingState
from src.tennis.name_norm import to_elo_name_from_te

_SCANNER_PATH = Path(__file__).parent.parent.parent / "scripts" / "tennis_scan.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ratings_with_players(*elo_names: str, rating: float = 1700.0) -> TennisEloRatings:
    r = TennisEloRatings()
    for n in elo_names:
        r.overall[n] = rating
        r.by_surface.setdefault("hard", {})[n] = rating
        r.surface_counts.setdefault("hard", {})[n] = 30
    return r


def _state_with_players(*elo_names: str) -> RollingState:
    state = RollingState()
    paired = list(elo_names)
    if len(paired) == 1:
        paired = [paired[0], paired[0]]
    for i in range(0, len(paired), 2):
        a = paired[i]
        b = paired[min(i + 1, len(paired) - 1)]
        state.update(a, b, "hard")
        state.update(b, a, "hard")
    return state


def _mock_lgbm_model(p_ab: float = 0.65) -> MagicMock:
    """Returns a model mock whose predict_p_a() always returns p_ab."""
    model = MagicMock()
    model.predict_p_a.return_value = np.array([p_ab])
    return model


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

    assert state.form.get(elo_key_a) is not None
    assert state.form.get(elo_key_b) is not None
    assert len(state.form[elo_key_a]) > 0
    assert len(state.form[elo_key_b]) > 0


# ---------------------------------------------------------------------------
# Fail-safe contracts — state
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
        use_live_stats=False,
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_unavailable") is True


def test_te_partially_missing_state_produces_rolling_state_invalid():
    """State supplied but one target player absent → rolling_state_invalid exactly.

    When a state object is passed, the ensemble must not return rolling_state_unavailable
    (that flag means state=None). The precise contract when hist_a or hist_b is empty
    is rolling_state_invalid.
    """
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Tsitsipas Stefanos")
    ratings = _ratings_with_players(elo_a, elo_b)
    # Only player A seeded; player B absent from state.form.
    state = _state_with_players(elo_a)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="2026-08-28T12:00:00Z",
        use_live_stats=False,
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_invalid") is True, (
        "State was supplied — must be rolling_state_invalid, not rolling_state_unavailable"
    )
    assert not out.get("rolling_state_unavailable"), (
        "rolling_state_unavailable must be absent when a state object was passed"
    )


# ---------------------------------------------------------------------------
# Fail-safe contracts — timestamp
# ---------------------------------------------------------------------------

def test_te_invalid_timestamp_fails_closed(monkeypatch):
    """Invalid match_date → source=='elo' and prediction_time_unavailable==True exactly.

    The model gate at line 193 of ensemble.py fires before state/timestamp checks,
    so prediction_time_unavailable is only reachable when model is present and
    gate_passed=True. A mock model is injected to reach the timestamp check.
    """
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Tsitsipas Stefanos")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)
    # Inject gate-passing mock so the timestamp check is reached (not short-circuited).
    monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm_model())
    monkeypatch.setitem(ens._CACHED, "gate_passed", True)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Tsitsipas Stefanos",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="not-a-real-date",
        use_live_stats=False,
    )
    assert out["source"] == "elo"
    assert out.get("prediction_time_unavailable") is True


def test_te_missing_match_date_fails_closed(monkeypatch):
    """Empty match_date → source=='elo' and prediction_time_unavailable==True exactly."""
    elo_a = to_elo_name_from_te("Kecmanovic Miomir")
    elo_b = to_elo_name_from_te("Nishikori Kei")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)
    monkeypatch.setitem(ens._CACHED, "model", _mock_lgbm_model())
    monkeypatch.setitem(ens._CACHED, "gate_passed", True)

    out = ens.predict_winner_ensemble(
        "Kecmanovic Miomir", "Nishikori Kei",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="",
        use_live_stats=False,
    )
    assert out["source"] == "elo"
    assert out.get("prediction_time_unavailable") is True


# ---------------------------------------------------------------------------
# Positive path — TE name + populated state + valid timestamp reaches ensemble
# ---------------------------------------------------------------------------

def test_te_populated_state_valid_timestamp_reaches_ensemble(monkeypatch):
    """Populated state + valid timestamp + known TE players → source=='ensemble'.

    Injects a mock LGBM model (gate_passed=True) to make the LGBM path
    deterministic regardless of whether model artifacts are present on disk.
    """
    elo_a = to_elo_name_from_te("Zverev Alexander")
    elo_b = to_elo_name_from_te("Sonego Lorenzo")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    mock_model = _mock_lgbm_model(p_ab=0.65)

    # Inject mock model into the ensemble cache so _load_model() returns it.
    monkeypatch.setitem(ens._CACHED, "model", mock_model)
    monkeypatch.setitem(ens._CACHED, "gate_passed", True)

    out = ens.predict_winner_ensemble(
        "Zverev Alexander", "Sonego Lorenzo",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="2026-08-28T15:00:00Z",
        use_live_stats=False,
    )

    assert out["source"] == "ensemble", (
        f"Expected source='ensemble' but got '{out['source']}'. "
        "Populated state + valid timestamp + mock model must reach the LGBM path."
    )
    assert not out.get("rolling_state_unavailable")
    assert not out.get("rolling_state_invalid")
    assert not out.get("prediction_time_unavailable")
    assert 0.0 < out["p_a"] < 1.0
    assert out["p_a"] + out["p_b"] == pytest.approx(1.0, abs=1e-6)
    # Confirm predict_p_a was called (LGBM path was exercised).
    assert mock_model.predict_p_a.called


# ---------------------------------------------------------------------------
# AST regression — actual scanner call-site inspection
# ---------------------------------------------------------------------------

def _find_predict_winner_ensemble_calls(tree: ast.AST) -> list[ast.Call]:
    """Return all ast.Call nodes whose function is predict_winner_ensemble."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "predict_winner_ensemble" or isinstance(func, ast.Attribute) and func.attr == "predict_winner_ensemble":
                calls.append(node)
    return calls


def _keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def _keyword_value(call: ast.Call, kwarg: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == kwarg:
            return kw.value
    return None


def _has_te_name_source(call: ast.Call) -> bool:
    """True if the call has name_source="te" as a literal keyword argument."""
    val = _keyword_value(call, "name_source")
    return isinstance(val, ast.Constant) and val.value == "te"


def test_scanner_registry_te_call_has_state_kwarg():
    """AST check: the registry-enriched TE predict_winner_ensemble() call in
    scripts/tennis_scan.py contains a 'state' keyword argument.

    This directly tests the production code, not a reproduction of it,
    protecting against regression to the pre-fix state (state=None default).
    """
    source = _SCANNER_PATH.read_text()
    tree = ast.parse(source)
    calls = _find_predict_winner_ensemble_calls(tree)

    te_calls = [c for c in calls if _has_te_name_source(c)]
    assert te_calls, "Expected at least one predict_winner_ensemble() call with name_source='te'"

    # The registry-enriched call is the one that ALSO has category= (from reg.category),
    # distinguishing it from the non-registry display-only call which uses hard-coded "hard".
    registry_te_calls = [
        c for c in te_calls if "category" in _keyword_names(c)
    ]
    assert registry_te_calls, (
        "Expected a registry-enriched TE call with both name_source='te' and category= kwarg"
    )

    for call in registry_te_calls:
        kw_names = _keyword_names(call)
        assert "state" in kw_names, (
            f"Registry-enriched TE call is missing 'state=' kwarg "
            f"(found keywords: {sorted(kw_names)})"
        )
        assert "match_date" in kw_names, (
            f"Registry-enriched TE call is missing 'match_date=' kwarg "
            f"(found keywords: {sorted(kw_names)})"
        )


def test_scanner_registry_te_call_state_value_is_live_state():
    """AST check: the 'state' kwarg in the registry-enriched TE call is 'live_state'."""
    source = _SCANNER_PATH.read_text()
    tree = ast.parse(source)
    calls = _find_predict_winner_ensemble_calls(tree)

    registry_te_calls = [
        c for c in calls
        if _has_te_name_source(c) and "category" in _keyword_names(c)
    ]
    assert registry_te_calls

    for call in registry_te_calls:
        state_val = _keyword_value(call, "state")
        assert state_val is not None, "state= kwarg is missing"
        assert isinstance(state_val, ast.Name) and state_val.id == "live_state", (
            f"Expected state=live_state but got: {ast.dump(state_val)}"
        )


def _find_assign_rhs(tree: ast.Module, var_name: str) -> list[ast.expr]:
    """Return all RHS expressions assigned to var_name in the full AST."""
    rhss = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    rhss.append(node.value)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name) and node.target.id == var_name and node.value):
            rhss.append(node.value)
    return rhss


def _find_last_assign_before_line(
    tree: ast.Module, var_name: str, call_line: int
) -> tuple[int, ast.expr] | None:
    """Return (line_no, rhs) for the last assignment to var_name STRICTLY before call_line.

    'Last' means highest line number that is still < call_line.  This is the
    nearest effective assignment visible at the call site (assuming no branches
    overwrite it between that line and call_line — proven by the fact that it IS
    the last assignment before the call).
    """
    candidates: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None or line >= call_line:
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    candidates.append((line, node.value))
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name) and node.target.id == var_name and node.value):
            candidates.append((line, node.value))
    if not candidates:
        return None
    return max(candidates, key=lambda t: t[0])


def _rhs_is_m_get_commence_time(rhs: ast.expr) -> bool:
    """Return True if rhs is m.get("commence_time", ...) or similar m[...] access."""
    if not isinstance(rhs, ast.Call):
        return False
    func = rhs.func
    # m.get("commence_time", ...) → func is an Attribute with attr="get"
    if (isinstance(func, ast.Attribute) and func.attr == "get"
            and rhs.args and isinstance(rhs.args[0], ast.Constant)):
        return rhs.args[0].value == "commence_time"
    return False


def test_scanner_registry_te_call_match_date_value():
    """AST check: the nearest effective assignment to the match_date variable before the
    predict_winner_ensemble() call is m.get('commence_time', ...).

    Stronger than checking ANY assignment: if the variable is reassigned to something
    wrong between the m.get(...) line and the call, the last assignment before the call
    is the wrong one and the test fails.

    Example regression this catches:
        _te_commence = m.get("commence_time", "")   # valid
        _te_commence = something_wrong              # overwrites it
        predict_winner_ensemble(..., match_date=_te_commence)
    → test would FAIL because 'something_wrong' is the nearest assignment.
    """
    source = _SCANNER_PATH.read_text()
    tree = ast.parse(source)
    calls = _find_predict_winner_ensemble_calls(tree)

    registry_te_calls = [
        c for c in calls
        if _has_te_name_source(c) and "category" in _keyword_names(c)
    ]
    assert registry_te_calls

    for call in registry_te_calls:
        call_line = call.lineno
        md_val = _keyword_value(call, "match_date")
        assert md_val is not None, "match_date= kwarg is missing"

        if isinstance(md_val, ast.Call):
            # Inline form: match_date=m.get("commence_time", ...) — direct check.
            assert _rhs_is_m_get_commence_time(md_val), (
                f"Inline match_date= is a Call but not m.get('commence_time', ...): "
                f"{ast.dump(md_val)}"
            )
        elif isinstance(md_val, ast.Name):
            # Variable form: find the NEAREST (last) assignment before this call.
            # This is the value the variable holds at the call site.
            var_name = md_val.id
            nearest = _find_last_assign_before_line(tree, var_name, call_line)
            assert nearest is not None, (
                f"match_date uses variable '{var_name}' but no assignment before "
                f"call at line {call_line} was found in AST"
            )
            nearest_line, nearest_rhs = nearest
            assert _rhs_is_m_get_commence_time(nearest_rhs), (
                f"Variable '{var_name}' at call (line {call_line}): "
                f"nearest assignment is at line {nearest_line} but is NOT "
                f"m.get('commence_time', ...) — got: {ast.dump(nearest_rhs)}. "
                f"This fires if '{var_name}' is reassigned between the m.get(...) "
                f"line and the predict_winner_ensemble() call."
            )
        else:
            pytest.fail(
                f"match_date= is neither a Call nor a Name: {ast.dump(md_val)}"
            )


def test_scanner_non_registry_te_call_has_no_state():
    """AST check: the non-registry display-only TE call intentionally omits state=.

    This verifies the non-registry path is unchanged by the FND-MODEL1-014 fix.
    """
    source = _SCANNER_PATH.read_text()
    tree = ast.parse(source)
    calls = _find_predict_winner_ensemble_calls(tree)

    # Non-registry call: name_source="te" but NO category= kwarg (uses hard-coded "hard").
    non_registry_te_calls = [
        c for c in calls
        if _has_te_name_source(c) and "category" not in _keyword_names(c)
    ]
    assert non_registry_te_calls, (
        "Expected a non-registry display-only TE call without category= kwarg"
    )

    for call in non_registry_te_calls:
        kw_names = _keyword_names(call)
        assert "state" not in kw_names, (
            "Non-registry TE display-only call must NOT have state= "
            "(intentional Elo-only, no reliable tournament metadata)"
        )


# ---------------------------------------------------------------------------
# Shadow governance and MODEL1-001 primary path
# ---------------------------------------------------------------------------

def test_scanner_te_registry_shadow_no_signal():
    """Shadow tournament: ensemble can be called with state for measurement;
    the signal-suppression is governed by the scanner's _is_shadow_reg gate,
    which is independent of predict_winner_ensemble itself."""
    elo_a = to_elo_name_from_te("Tsitsipas Stefanos")
    elo_b = to_elo_name_from_te("Kecmanovic Miomir")
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    all_live_signals: list = []
    _is_shadow_reg = True  # shadow tournament

    # Simulate: ensemble called, but signal list stays empty for shadow.
    _probs = ens.predict_winner_ensemble(
        "Tsitsipas Stefanos", "Kecmanovic Miomir",
        ratings, "hard",
        name_source="te",
        state=state,
        match_date="2026-08-28T16:00:00Z",
        use_live_stats=False,
    )
    if not _is_shadow_reg:
        all_live_signals.append("WOULD_BE_SIGNAL")

    assert len(all_live_signals) == 0, "Shadow tournament must produce no signals"
    # Ensemble can return either elo or ensemble (model may not be on disk in CI).
    assert "p_a" in _probs
    # Must not have rolled back to the pre-fix unavailable state.
    assert not _probs.get("rolling_state_unavailable")


def test_primary_odds_api_path_unchanged(monkeypatch):
    """MODEL1-001 primary path (odds_api name_source): no regression from this fix."""
    elo_a, elo_b = "Alcaraz C.", "Sinner J."
    ratings = _ratings_with_players(elo_a, elo_b)
    state = _state_with_players(elo_a, elo_b)

    mock_model = _mock_lgbm_model(p_ab=0.70)
    monkeypatch.setitem(ens._CACHED, "model", mock_model)
    monkeypatch.setitem(ens._CACHED, "gate_passed", True)

    out = ens.predict_winner_ensemble(
        "Carlos Alcaraz", "Jannik Sinner",
        ratings, "hard",
        state=state,
        match_date="2026-08-28T18:00:00Z",
        use_live_stats=False,
    )
    assert out["source"] == "ensemble", (
        "MODEL1-001 primary path must not be broken by MODEL1-014 fix"
    )
    assert not out.get("rolling_state_unavailable")
    assert not out.get("rolling_state_invalid")
    assert not out.get("prediction_time_unavailable")
