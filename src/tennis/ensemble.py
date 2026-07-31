"""Tennis Elo + LGBM Ensemble-Prediction (Roadmap J2-K Phase 4).

Wenn `models/tennis_lgbm/` existiert und Gate im metadata.json passed, wird
im Scanner statt reinem Elo ein 50/50-Blend Elo⊕LGBM verwendet. Fallback auf
reines Elo wenn Modell fehlt oder Player unbekannt (kein Rank/Elo).

Public API (drop-in-Ersatz für tennis_elo.predict_winner):
    predict_winner_ensemble(pa, pb, ratings, surface, tournament) -> {'p_a', 'p_b', 'source'}
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.models.tennis_elo import TennisEloRatings, predict_winner
from src.models import tennis_lgbm as tlgbm
from src.tennis.features import RollingState, build_match_features
from src.tennis.name_norm import (
    is_probably_elo_format, to_elo_name_from_odds_api, to_elo_name_from_te,
)

_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "tennis_lgbm"
_CACHED: dict[str, object] = {}


def _load_model():
    if "model" in _CACHED:
        return _CACHED["model"], _CACHED.get("gate_passed", False)
    model_path = _MODEL_DIR / "model.pkl"
    meta_path = _MODEL_DIR / "metadata.json"
    if not model_path.exists() or not meta_path.exists():
        _CACHED["model"] = None
        _CACHED["gate_passed"] = False
        return None, False
    try:
        model = tlgbm.load(_MODEL_DIR)
        meta = json.loads(meta_path.read_text())
        gate_passed = bool(meta.get("gate_passed", False))
        _CACHED["model"] = model
        _CACHED["gate_passed"] = gate_passed
        return model, gate_passed
    except Exception as e:
        print(f"[tennis_ensemble] Load failed: {e}")
        _CACHED["model"] = None
        _CACHED["gate_passed"] = False
        return None, False


# Blend weight: LGBM bekommt Anteil, aber Elo bleibt Anker (Robustheit bei unbekannten Playern)
_LGBM_WEIGHT = 0.45


def _normalize_for_elo(name: str, source: str = "odds_api") -> str:
    """source ∈ {'odds_api', 'te', 'elo'}. Idempotent für elo-Format."""
    if is_probably_elo_format(name):
        return name
    if source == "te":
        return to_elo_name_from_te(name)
    return to_elo_name_from_odds_api(name)


def predict_winner_ensemble(
    player_a: str,
    player_b: str,
    ratings: TennisEloRatings,
    surface: str,
    best_of: int = 3,
    category: str = "atp250",
    round_str: str = "",
    rank_a: float | None = None,
    rank_b: float | None = None,
    name_source: str = "odds_api",
) -> dict[str, float]:
    """Returns {p_a, p_b, source}. Source ∈ {'elo', 'ensemble'}.

    name_source: Format-Hint für Elo-Name-Konvertierung. 'odds_api' für Live-
    Scanner-Namen (Vorname Nachname); 'te' für Tennisexplorer (Nachname Vorname).
    """
    # Namen für Elo-Lookup normalisieren (bewahrt originale Anzeige-Namen).
    elo_key_a = _normalize_for_elo(player_a, name_source)
    elo_key_b = _normalize_for_elo(player_b, name_source)
    elo_probs = predict_winner(elo_key_a, elo_key_b, ratings, surface)

    model, gate_passed = _load_model()
    if model is None or not gate_passed:
        return {**elo_probs, "source": "elo"}

    # LGBM-Prediction: erfordert Feature-Bau ex-ante (leerer State — kein H2H bekannt).
    # Elo-Werte kommen aus ratings; Rank fällt auf Prior wenn nicht geliefert.
    _MIN_RANK = 1500.0
    ra = rank_a if rank_a and rank_a > 0 else _MIN_RANK
    rb = rank_b if rank_b and rank_b > 0 else _MIN_RANK

    elo_a_over = ratings.get_overall(elo_key_a)
    elo_b_over = ratings.get_overall(elo_key_b)
    elo_a_surf = ratings.get_blended(elo_key_a, surface)
    elo_b_surf = ratings.get_blended(elo_key_b, surface)

    feats = build_match_features(
        player_a=player_a, player_b=player_b,
        surface=surface, best_of=best_of,
        category=category, round_str=round_str,
        rank_a=ra, rank_b=rb,
        elo_a=elo_a_over, elo_b=elo_b_over,
        elo_surface_a=elo_a_surf, elo_surface_b=elo_b_surf,
        state=RollingState(),  # Leer — kein H2H/Form für unbekannte Live-Matches
        date=None,
    )
    X = pd.DataFrame([feats])
    p_lgbm_a = float(model.predict_p_a(X)[0])

    p_a = _LGBM_WEIGHT * p_lgbm_a + (1 - _LGBM_WEIGHT) * elo_probs["p_a"]
    return {"p_a": p_a, "p_b": 1.0 - p_a, "source": "ensemble"}
