"""FND-MODEL1-001 parity tests: train-style vs live-style RollingState.

Validates that build_live_rolling_state() produces feature values identical to
those extracted during walk-forward training (tennis_train.py semantics).

Core assertion: for any match at time T, the feature values extracted
  (a) immediately before state.update() in a full walk-forward pass, and
  (b) by building state from all matches before T and querying at T
must be identical (or within documented numerical tolerance = 0.0 for integer
counts, float equality for rate features).

Also covers the fail-safe requirement: state=None → Elo-only, no LGBM.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.tennis.features import RollingState, build_match_features, FEATURE_COLUMNS
from src.tennis.elo_source import build_live_rolling_state

# State-derived features in FEATURE_COLUMNS that this fix targets.
_ROLLING_FEATURES = [
    "form_a_wr", "form_b_wr", "form_diff",
    "form_a_wr_surface", "form_b_wr_surface", "form_diff_surface",
    "h2h_a_wr", "h2h_n", "h2h_surface_a_wr", "h2h_surface_n",
    "rest_a", "rest_b", "rest_diff",
    "form_hot_diff", "form_stable_diff",
    "form_quality_a", "form_quality_b", "form_quality_diff",
    "sets_dropped_rate_a", "sets_dropped_rate_b",
    "tb_wr_a", "tb_wr_b", "tb_wr_diff",
    "sets_last7d_a", "sets_last7d_b",
]


def _make_match_row(
    winner: str, loser: str, surface: str, date: str,
    winner_rank: float | None = None, loser_rank: float | None = None,
    winner_sets: int | None = None, loser_sets: int | None = None,
    w1: int | None = None, l1: int | None = None,
    w2: int | None = None, l2: int | None = None,
    w3: int | None = None, l3: int | None = None,
) -> dict:
    return {
        "winner_name": winner, "loser_name": loser,
        "surface": surface.capitalize(),  # Sackmann Title case
        "tourney_date": pd.Timestamp(date),
        "winner_rank": winner_rank, "loser_rank": loser_rank,
        "winner_sets": winner_sets, "loser_sets": loser_sets,
        "w1": w1, "l1": l1, "w2": w2, "l2": l2, "w3": w3, "l3": l3,
        "score": "", "tourney_level": "A",
    }


def _build_features(player_a: str, player_b: str, surface: str,
                    state: RollingState, date: pd.Timestamp | None) -> dict:
    return build_match_features(
        player_a=player_a, player_b=player_b,
        surface=surface, best_of=3,
        category="atp250", round_str="2nd Round",
        rank_a=100.0, rank_b=100.0,
        elo_a=1500.0, elo_b=1500.0,
        elo_surface_a=1500.0, elo_surface_b=1500.0,
        state=state, date=date,
    )


def _extract_train_style(matches: list[dict], target_idx: int) -> dict:
    """Walk-forward: build features for matches[target_idx] using state of all prior matches."""
    state = RollingState(window=10)
    for i, m in enumerate(matches):
        winner, loser = m["winner_name"], m["loser_name"]
        surface = m["surface"].lower()
        date = m["tourney_date"]
        winner_rank = m.get("winner_rank")
        loser_rank = m.get("loser_rank")
        sets_w = m.get("winner_sets")
        sets_l = m.get("loser_sets")
        tb_w = tb_l = 0
        for j in range(1, 4):
            gw = m.get(f"w{j}"); gl = m.get(f"l{j}")
            if gw is not None and gl is not None:
                if gw == 7 and gl == 6:
                    tb_w += 1
                elif gl == 7 and gw == 6:
                    tb_l += 1
        if i == target_idx:
            # Extract features BEFORE updating state (walk-forward rule)
            feats = _build_features(winner, loser, surface, state, date)
        state.update(
            winner, loser, surface, date=date,
            winner_rank=winner_rank, loser_rank=loser_rank,
            sets_w=sets_w, sets_l=sets_l,
            tiebreaks_won_by_winner=tb_w, tiebreaks_won_by_loser=tb_l,
        )
    return feats


def _extract_live_style(matches: list[dict], target_idx: int) -> dict:
    """Live-style: build state from all matches BEFORE target, then extract features."""
    prior = matches[:target_idx]
    df = pd.DataFrame(prior) if prior else pd.DataFrame(columns=list(matches[0].keys()))
    state = build_live_rolling_state(df)
    m = matches[target_idx]
    winner, loser = m["winner_name"], m["loser_name"]
    surface = m["surface"].lower()
    date = m["tourney_date"]
    return _build_features(winner, loser, surface, state, date)


def _assert_features_equal(train: dict, live: dict, label: str = "") -> None:
    for feat in _ROLLING_FEATURES:
        t_val = train.get(feat, 0.0)
        l_val = live.get(feat, 0.0)
        assert abs(t_val - l_val) < 1e-9, (
            f"[{label}] {feat}: train={t_val:.6f} live={l_val:.6f}"
        )


# ---------------------------------------------------------------------------
# Scenario 1: First match (sparse history — both players unknown)
# ---------------------------------------------------------------------------

def test_parity_first_match():
    matches = [
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-15"),
        _make_match_row("Alcaraz C.", "Djokovic N.", "hard", "2024-01-20"),
    ]
    train = _extract_train_style(matches, target_idx=0)
    live = _extract_live_style(matches, target_idx=0)
    _assert_features_equal(train, live, "first_match")
    # Both should be neutral priors (no history)
    assert train["form_a_wr"] == pytest.approx(0.5)
    assert train["h2h_n"] == pytest.approx(0.0)
    assert train["rest_a"] == pytest.approx(14.0)


# ---------------------------------------------------------------------------
# Scenario 2: Established player with several matches
# ---------------------------------------------------------------------------

def test_parity_established_player():
    matches = [
        _make_match_row("Alcaraz C.", "Opponent A.", "hard", "2024-01-01",
                        winner_sets=2, loser_sets=0),
        _make_match_row("Alcaraz C.", "Opponent B.", "hard", "2024-01-05",
                        winner_sets=2, loser_sets=1),
        _make_match_row("Opponent C.", "Alcaraz C.", "hard", "2024-01-10",
                        winner_sets=2, loser_sets=0),
        _make_match_row("Alcaraz C.", "Opponent D.", "hard", "2024-01-15"),
    ]
    train = _extract_train_style(matches, target_idx=3)
    live = _extract_live_style(matches, target_idx=3)
    _assert_features_equal(train, live, "established_player")
    # 2W + 1L across 3 matches → form = 2/3
    assert train["form_a_wr"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Scenario 3: Prior H2H exists
# ---------------------------------------------------------------------------

def test_parity_prior_h2h():
    matches = [
        _make_match_row("Alcaraz C.", "Sinner J.", "clay", "2023-05-01"),
        _make_match_row("Sinner J.", "Alcaraz C.", "clay", "2023-09-01"),
        _make_match_row("Alcaraz C.", "Sinner J.", "clay", "2024-05-01"),
    ]
    train = _extract_train_style(matches, target_idx=2)
    live = _extract_live_style(matches, target_idx=2)
    _assert_features_equal(train, live, "prior_h2h")
    # 1W 1L from A's perspective before third match
    assert train["h2h_a_wr"] == pytest.approx(0.5)
    assert train["h2h_n"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Scenario 4: Surface H2H split (clay vs hard)
# ---------------------------------------------------------------------------

def test_parity_surface_h2h():
    matches = [
        _make_match_row("Alcaraz C.", "Sinner J.", "clay", "2023-04-01"),
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2023-08-01"),
        _make_match_row("Alcaraz C.", "Sinner J.", "clay", "2024-04-01"),
    ]
    train = _extract_train_style(matches, target_idx=2)
    live = _extract_live_style(matches, target_idx=2)
    _assert_features_equal(train, live, "surface_h2h")
    # Only the clay H2H (1 match) is relevant to the third clay match
    assert train["h2h_surface_a_wr"] == pytest.approx(1.0)
    assert train["h2h_surface_n"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Scenario 5: Player with match in last 7 days (fatigue)
# ---------------------------------------------------------------------------

def test_parity_recent_fatigue():
    matches = [
        _make_match_row("Alcaraz C.", "Opponent A.", "hard", "2024-01-08",
                        winner_sets=2, loser_sets=1),
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-10"),  # target
    ]
    train = _extract_train_style(matches, target_idx=1)
    live = _extract_live_style(matches, target_idx=1)
    _assert_features_equal(train, live, "recent_fatigue")
    # Alcaraz played 3 sets 2 days ago → sets_last7d_a = 3
    assert train["sets_last7d_a"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Scenario 6: Long rest (14-day prior cap)
# ---------------------------------------------------------------------------

def test_parity_long_rest():
    matches = [
        _make_match_row("Alcaraz C.", "Opponent A.", "hard", "2023-11-01"),
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-15"),  # 75d rest → capped at 60
    ]
    train = _extract_train_style(matches, target_idx=1)
    live = _extract_live_style(matches, target_idx=1)
    _assert_features_equal(train, live, "long_rest")
    assert train["rest_a"] == pytest.approx(60.0)  # capped


# ---------------------------------------------------------------------------
# Scenario 7: Cross-surface form (hard form ≠ clay form)
# ---------------------------------------------------------------------------

def test_parity_cross_surface():
    matches = [
        _make_match_row("Alcaraz C.", "Opp A.", "clay", "2024-01-01"),
        _make_match_row("Opp B.", "Alcaraz C.", "clay", "2024-01-05"),
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-10"),  # hard match, no clay H2H
    ]
    train = _extract_train_style(matches, target_idx=2)
    live = _extract_live_style(matches, target_idx=2)
    _assert_features_equal(train, live, "cross_surface")
    # Hard surface form = no data → 0.5 prior
    assert train["form_a_wr_surface"] == pytest.approx(0.5)
    # Overall form = 1W 1L → 0.5
    assert train["form_a_wr"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Scenario 8: Symmetric A/B and B/A calls return complementary values
# ---------------------------------------------------------------------------

def test_parity_symmetric_ab_ba():
    matches = [
        _make_match_row("Player X.", "Player Y.", "hard", "2024-01-01"),
        _make_match_row("Player X.", "Player Y.", "hard", "2024-01-10"),
        _make_match_row("Player Y.", "Player X.", "hard", "2024-01-20"),  # target
    ]
    state = build_live_rolling_state(pd.DataFrame(matches[:2]))
    date = pd.Timestamp("2024-01-20")

    feats_ab = _build_features("Player X.", "Player Y.", "hard", state, date)
    feats_ba = _build_features("Player Y.", "Player X.", "hard", state, date)

    # h2h_a_wr from X's view should be complement of h2h_a_wr from Y's view
    assert feats_ab["h2h_a_wr"] + feats_ba["h2h_a_wr"] == pytest.approx(1.0)
    assert feats_ab["h2h_n"] == feats_ba["h2h_n"]  # same total count
    assert feats_ab["form_a_wr"] == feats_ba["form_b_wr"]  # form_a in AB = form_b in BA


# ---------------------------------------------------------------------------
# Scenario 9: Temporal leakage proof — target match cannot update state
# ---------------------------------------------------------------------------

def test_no_future_leakage():
    """State built from prior matches must NOT contain the target match result."""
    matches = [
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-01"),
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-10"),  # prior
        # target match (Sinner wins)
        _make_match_row("Sinner J.", "Alcaraz C.", "hard", "2024-01-20"),
    ]
    # State from only the first two matches
    state_prior = build_live_rolling_state(pd.DataFrame(matches[:2]))

    # Alcaraz: 2W/0L before target → form = 1.0
    assert state_prior.wr("Alcaraz C.") == pytest.approx(1.0)

    # State from all three matches (INCLUDING the target's result)
    state_incl = build_live_rolling_state(pd.DataFrame(matches))
    # Now Alcaraz has 2W/1L → form = 2/3
    assert state_incl.wr("Alcaraz C.") == pytest.approx(2 / 3)

    # The live-style extraction for target_idx=2 must use state_prior, not state_incl.
    # For the third match: winner=Sinner (player_a), loser=Alcaraz (player_b).
    train = _extract_train_style(matches, target_idx=2)
    live = _extract_live_style(matches, target_idx=2)
    _assert_features_equal(train, live, "no_future_leakage")
    # player_a = Sinner (winner of target). From B's perspective (Alcaraz), form_b_wr = 1.0.
    # Verify Alcaraz's pre-target form is preserved (as player_b in this orientation).
    assert live["form_b_wr"] == pytest.approx(1.0)  # Alcaraz 2W/0L before target
    assert live["form_a_wr"] == pytest.approx(0.0)  # Sinner 0W/2L before target


# ---------------------------------------------------------------------------
# Scenario 10: build_live_rolling_state handles empty DataFrame
# ---------------------------------------------------------------------------

def test_build_live_state_empty():
    state = build_live_rolling_state(pd.DataFrame())
    assert state.wr("Anyone") == pytest.approx(0.5)
    assert state.h2h_wr("A", "B") == (0.5, 0)


# ---------------------------------------------------------------------------
# Scenario 11: Tiebreak detection from per-set scores
# ---------------------------------------------------------------------------

def test_tiebreak_parity():
    matches = [
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-01",
                        winner_sets=2, loser_sets=1,
                        w1=7, l1=6,   # Alcaraz wins TB
                        w2=4, l2=6,   # Sinner wins normal set
                        w3=6, l3=4),  # Alcaraz wins normal set
        _make_match_row("Alcaraz C.", "Sinner J.", "hard", "2024-01-10"),  # target
    ]
    train = _extract_train_style(matches, target_idx=1)
    live = _extract_live_style(matches, target_idx=1)
    _assert_features_equal(train, live, "tiebreak")
    # 1 TB won → tb_wr_a should be 1.0 (1 won / 1 played)
    assert train["tb_wr_a"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Fail-safe: state=None → Elo-only (tested via ensemble, not just features)
# ---------------------------------------------------------------------------

def test_fail_safe_none_state_bypasses_lgbm(monkeypatch):
    """When state=None, predict_winner_ensemble must return source='elo'."""
    from src.tennis.ensemble import predict_winner_ensemble, _CACHED
    from src.models.tennis_elo import TennisEloRatings

    _CACHED.clear()
    ratings = TennisEloRatings()
    ratings.overall["Alcaraz C."] = 1800
    ratings.overall["Sinner J."] = 1750
    ratings.by_surface["hard"] = {"Alcaraz C.": 1820, "Sinner J.": 1740}
    ratings.surface_counts["hard"] = {"Alcaraz C.": 30, "Sinner J.": 25}

    out = predict_winner_ensemble(
        "Carlos Alcaraz", "Jannik Sinner", ratings, "hard",
        state=None,
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_unavailable") is True
    _CACHED.clear()


def test_fail_safe_populated_state_enables_lgbm():
    """When state is a non-empty RollingState, LGBM path is attempted."""
    from src.tennis.ensemble import predict_winner_ensemble, _CACHED
    from src.models.tennis_elo import TennisEloRatings

    _CACHED.clear()
    ratings = TennisEloRatings()
    ratings.overall["Alcaraz C."] = 1900
    ratings.overall["Michelsen A."] = 1500
    ratings.by_surface["hard"] = {"Alcaraz C.": 1920, "Michelsen A.": 1480}
    ratings.surface_counts["hard"] = {"Alcaraz C.": 30, "Michelsen A.": 25}

    populated_state = RollingState()
    populated_state.update("Alcaraz C.", "Michelsen A.", "hard")

    out = predict_winner_ensemble(
        "Carlos Alcaraz", "Alex Michelsen", ratings, "hard",
        state=populated_state,
    )
    # With valid model: ensemble; if gate fails for some reason: elo (not rolling_state_unavailable)
    assert out["source"] in ("ensemble", "elo")
    assert "rolling_state_unavailable" not in out
    _CACHED.clear()
