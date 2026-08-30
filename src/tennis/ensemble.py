"""Tennis Elo + LGBM Ensemble-Prediction (Roadmap J2-K Phase 4).

Wenn `models/tennis_lgbm/` existiert und Gate im metadata.json passed, wird
im Scanner statt reinem Elo ein 50/50-Blend Elo⊕LGBM verwendet. Fallback auf
reines Elo wenn Modell fehlt oder Player unbekannt (kein Rank/Elo).

Public API (drop-in-Ersatz für tennis_elo.predict_winner):
    predict_winner_ensemble(pa, pb, ratings, surface, tournament) -> {'p_a', 'p_b', 'source'}
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pandas as pd

_log = logging.getLogger("sportsbrain.tennis.ensemble")

from src.config import TENNIS_USE_LIVE_STATS
from src.data.tennis_stats import fetch_aggregate, save_serve_snapshot
from src.models.tennis_elo import TennisEloRatings, predict_winner
from src.models import tennis_lgbm as tlgbm
from src.tennis.features import RollingState, build_match_features
from src.tennis.name_norm import (
    is_probably_elo_format, to_elo_name_from_odds_api, to_elo_name_from_te,
)
from src.tennis.elo_hebels import bayesian_dampen, altitude_adjust
from src.tennis.style_cluster import classify_style, style_matchup_edge

# Kategorien in denen Bayesian-Dämpfung nachweislich schadet (Ablation 2026-08-02).
_BAYESIAN_SKIP_CATEGORIES = frozenset({"wta500"})

_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "tennis_lgbm"
_CAL_DIR = Path(__file__).parent.parent.parent / "models" / "tennis_calibrators"
_CACHED: dict[str, object] = {}
_CACHED_LOCK = threading.Lock()

# Mindestanzahl settled Signale bevor Meta-Calibrator angewendet wird
_META_CAL_MIN_SAMPLES = 50


def _load_surface_calibrator(surface: str):
    """Lädt Surface-spezifischen Isotonic-Calibrator (J8-I10).
    Erstellt von scripts/tennis_recalibrate.py — hard/clay/grass separat."""
    cache_key = f"surf_cal_{surface}"
    with _CACHED_LOCK:
        if cache_key in _CACHED:
            return _CACHED[cache_key]
    path = _CAL_DIR / f"{surface}.pkl"
    if not path.exists():
        with _CACHED_LOCK:
            _CACHED[cache_key] = None
        return None
    try:
        import pickle
        cal = pickle.loads(path.read_bytes())
        with _CACHED_LOCK:
            _CACHED[cache_key] = cal
        _log.info("tennis-ensemble: surface_calibrator[%s] geladen", surface)
    except Exception as e:
        _log.warning("tennis-ensemble: surface_calibrator[%s] load failed (%s)", surface, e)
        with _CACHED_LOCK:
            _CACHED[cache_key] = None
    with _CACHED_LOCK:
        return _CACHED[cache_key]


def _load_meta_calibrator():
    """Lädt Meta-Calibrator aus settled Signalen (tennis_signal_recalibrate.py).
    Nur aktiv wenn >= _META_CAL_MIN_SAMPLES Samples im Fit-Zeitpunkt vorhanden waren."""
    with _CACHED_LOCK:
        if "meta_cal" in _CACHED:
            return _CACHED["meta_cal"]
    path = _MODEL_DIR / "meta_calibrator.pkl"
    if not path.exists():
        with _CACHED_LOCK:
            _CACHED["meta_cal"] = None
        return None
    try:
        import pickle
        data = pickle.loads(path.read_bytes())
        if data.get("n_samples", 0) < _META_CAL_MIN_SAMPLES:
            with _CACHED_LOCK:
                _CACHED["meta_cal"] = None
            return None
        with _CACHED_LOCK:
            _CACHED["meta_cal"] = data["calibrator"]
        _log.info("tennis-ensemble: meta_calibrator geladen (%d samples)", data["n_samples"])
    except Exception as e:
        _log.warning("tennis-ensemble: meta_calibrator load failed (%s)", e)
        with _CACHED_LOCK:
            _CACHED["meta_cal"] = None
    with _CACHED_LOCK:
        return _CACHED["meta_cal"]


def _load_model():
    with _CACHED_LOCK:
        if "model" in _CACHED:
            return _CACHED["model"], _CACHED.get("gate_passed", False)
    model_path = _MODEL_DIR / "model.pkl"
    meta_path = _MODEL_DIR / "metadata.json"
    if not model_path.exists() or not meta_path.exists():
        _log.info("tennis-ensemble: model files missing (%s / %s) → Fallback Elo",
                  model_path.name, meta_path.name)
        with _CACHED_LOCK:
            _CACHED["model"] = None
            _CACHED["gate_passed"] = False
        return None, False
    try:
        model = tlgbm.load(_MODEL_DIR)
        meta = json.loads(meta_path.read_text())
        gate_passed = bool(meta.get("gate_passed", False))
        with _CACHED_LOCK:
            _CACHED["model"] = model
            _CACHED["gate_passed"] = gate_passed
        return model, gate_passed
    except Exception as e:
        _log.warning("tennis-ensemble: model load FAILED (%s) → Fallback Elo", e)
        with _CACHED_LOCK:
            _CACHED["model"] = None
            _CACHED["gate_passed"] = False
        return None, False


# Surface-spezifische Blend-Gewichte (Rationale: Grass → Serve-Dominanz → Elo präziser).
# Validiert via tennis_hebel_backtest.py bevor Anpassung. Default für unbekannte Surfaces.
_LGBM_WEIGHT: dict[str, float] = {
    "hard":    0.45,
    "clay":    0.40,
    "grass":   0.35,
    "carpet":  0.42,
    "default": 0.45,
}


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
    use_live_stats: bool = True,
    match_date: str | None = None,
    tournament_slug: str | None = None,
    state: RollingState | None = None,
) -> dict[str, float]:
    """Returns {p_a, p_b, source}. Source ∈ {'elo', 'ensemble'}.

    name_source: Format-Hint für Elo-Name-Konvertierung. 'odds_api' für Live-
    Scanner-Namen (Vorname Nachname); 'te' für Tennisexplorer (Nachname Vorname).

    state: Pre-built RollingState populated from the full historical match corpus
    (FND-MODEL1-001 fix). The scanner builds this once via build_live_rolling_state()
    and passes it here. Missing or target-player-empty state fails closed to Elo-only.
    A valid match timestamp is also required because rest/fatigue features are
    prediction-time dependent.
    """
    # Namen für Elo-Lookup normalisieren (bewahrt originale Anzeige-Namen).
    elo_key_a = _normalize_for_elo(player_a, name_source)
    elo_key_b = _normalize_for_elo(player_b, name_source)
    elo_probs = predict_winner(elo_key_a, elo_key_b, ratings, surface)

    # Unknown-Player-Gate: Spieler ohne ausreichende Elo-Historie erzeugen p≈0.5
    # (Default-Rating 1500) → keine verlässliche EV-Berechnung möglich.
    # Mindestens 5 Elo-Matches insgesamt bevor Wett-Signal erlaubt.
    _known_a = ratings.is_known(elo_key_a, min_matches=5)
    _known_b = ratings.is_known(elo_key_b, min_matches=5)
    if not _known_a or not _known_b:
        _log.info(
            "tennis-ensemble: unknown player(s) — %s (known=%s), %s (known=%s) → low_confidence",
            elo_key_a, _known_a, elo_key_b, _known_b,
        )
        return {**elo_probs, "source": "elo", "low_confidence": True,
                "unknown_player_a": not _known_a, "unknown_player_b": not _known_b}

    model, gate_passed = _load_model()
    if model is None or not gate_passed:
        return {**elo_probs, "source": "elo"}

    # FND-MODEL1-001: production LGBM requires a real, target-player-populated
    # RollingState. This catches both construction failure and empty/partial corpora.
    if state is None:
        _log.warning(
            "tennis-ensemble: no RollingState for %s vs %s → LGBM bypassed "
            "(rolling_state_unavailable)", player_a, player_b,
        )
        return {**elo_probs, "source": "elo", "rolling_state_unavailable": True}
    hist_a = state.form.get(elo_key_a)
    hist_b = state.form.get(elo_key_b)
    if not hist_a or not hist_b:
        _log.warning(
            "tennis-ensemble: invalid RollingState for %s vs %s → LGBM bypassed "
            "(rolling_state_invalid)", player_a, player_b,
        )
        return {**elo_probs, "source": "elo", "rolling_state_invalid": True}

    # Prediction timestamp is mandatory: rest/fatigue features are invalid without it.
    if not match_date:
        _log.warning(
            "tennis-ensemble: missing prediction timestamp for %s vs %s → LGBM bypassed",
            player_a, player_b,
        )
        return {**elo_probs, "source": "elo", "prediction_time_unavailable": True}
    try:
        ts = pd.Timestamp(match_date)
        if pd.isna(ts):
            raise ValueError("NaT timestamp")
        _pred_date = ts.tz_localize(None) if ts.tzinfo is None else ts.tz_convert(None)
    except (ValueError, TypeError, AttributeError, OverflowError):
        _log.warning(
            "tennis-ensemble: invalid prediction timestamp %r for %s vs %s → LGBM bypassed",
            match_date, player_a, player_b,
        )
        return {**elo_probs, "source": "elo", "prediction_time_unavailable": True}

    # LGBM-Prediction: Features ex-ante mit historischem RollingState.
    # Elo-Werte kommen aus ratings; Rank fällt auf Prior wenn nicht geliefert.
    _MIN_RANK = 1500.0
    ra = rank_a if rank_a and rank_a > 0 else _MIN_RANK
    rb = rank_b if rank_b and rank_b > 0 else _MIN_RANK

    elo_a_over = ratings.get_overall(elo_key_a)
    elo_b_over = ratings.get_overall(elo_key_b)
    elo_a_surf = ratings.get_blended(elo_key_a, surface)
    elo_b_surf = ratings.get_blended(elo_key_b, surface)

    # J2-M: Live-Stats-Fetch (cached 24h — max 2 HTTP-Calls pro Match).
    stats_a = stats_b = None
    if use_live_stats and TENNIS_USE_LIVE_STATS:
        # WTA-Kategorien routen zu TA-wplayer.cgi statt player-classic.cgi.
        tour = "wta" if category.startswith("wta") else "atp"
        # J8-B10: before_date passt Live-Fetch an WF-Konvention an (nur Historie < match_date).
        try:
            stats_a = fetch_aggregate(player_a, last_n=20, surface=surface, tour=tour,
                                      before_date=match_date)
            stats_b = fetch_aggregate(player_b, last_n=20, surface=surface, tour=tour,
                                      before_date=match_date)
            # J8-I7: Asof-Snapshot für Walk-Forward-Training (J2-N Prerequisite).
            if stats_a and stats_a.n_matches > 0:
                try:
                    save_serve_snapshot(player_a, surface, match_date, stats_a, tour=tour)
                except Exception:
                    pass
            if stats_b and stats_b.n_matches > 0:
                try:
                    save_serve_snapshot(player_b, surface, match_date, stats_b, tour=tour)
                except Exception:
                    pass
        except Exception as e:
            _log.debug("tennis-ensemble: serve-stats fetch failed (%s) → neutral prior", e)
            stats_a = stats_b = None

    # Phase 3c: Biometrie + TZ-Feature für LGBM (gecacht, low latency nach erstem Call).
    try:
        from src.data.tennis_bios import lookup_bio
        _tour_bio = "wta" if category.startswith("wta") else "atp"
        bio_a = lookup_bio(player_a, _tour_bio)
        bio_b = lookup_bio(player_b, _tour_bio)
    except Exception:
        bio_a = bio_b = None

    # FND-MODEL1-001: state lookups use the canonical Elo-format key (elo_key_a/b),
    # which matches the winner_name/loser_name format in the historical corpus used
    # to build the state. Both AB and BA symmetry calls share the same pre-match state
    # and the same prediction timestamp — no state mutation occurs here.
    feats = build_match_features(
        player_a=elo_key_a, player_b=elo_key_b,
        surface=surface, best_of=best_of,
        category=category, round_str=round_str,
        rank_a=ra, rank_b=rb,
        elo_a=elo_a_over, elo_b=elo_b_over,
        elo_surface_a=elo_a_surf, elo_surface_b=elo_b_surf,
        state=state,
        date=_pred_date,
        serve_stats_a=stats_a,
        serve_stats_b=stats_b,
        bio_a=bio_a,
        bio_b=bio_b,
        tournament_slug=tournament_slug,
    )
    # Symmetric prediction: average P(A wins | A first) and 1 − P(B wins | B first).
    # Eliminates ~22pp positional bias from training-data ordering correlation.
    feats_ba = build_match_features(
        player_a=elo_key_b, player_b=elo_key_a,  # swapped elo keys
        surface=surface, best_of=best_of,
        category=category, round_str=round_str,
        rank_a=rb, rank_b=ra,
        elo_a=elo_b_over, elo_b=elo_a_over,
        elo_surface_a=elo_b_surf, elo_surface_b=elo_a_surf,
        state=state,
        date=_pred_date,
        serve_stats_a=stats_b, serve_stats_b=stats_a,
        bio_a=bio_b, bio_b=bio_a,
        tournament_slug=tournament_slug,
    )
    X = pd.DataFrame([feats])
    X_ba = pd.DataFrame([feats_ba])
    p_ab = float(model.predict_p_a(X)[0])
    p_ba = float(model.predict_p_a(X_ba)[0])  # P(B wins when listed first)
    p_lgbm_a = (p_ab + (1.0 - p_ba)) / 2.0

    _w = _LGBM_WEIGHT.get(surface.lower(), _LGBM_WEIGHT["default"])
    p_a = _w * p_lgbm_a + (1 - _w) * elo_probs["p_a"]

    # J2-M Rule-based Adjustment: dominance_rate-Diff (Serve+Return-Punkte-Anteil letzte 20)
    # ist starker Indikator und wird vom aktuellen LGBM nicht genutzt (trainiert vor J2-M).
    # Konservative Bias: max ±3pp Verschiebung, nur wenn beide Spieler ausreichend
    # Sample-Size haben (n≥10). Nach Retrain (Phase 4) auf 0 setzbar.
    if stats_a and stats_b and stats_a.n_matches >= 10 and stats_b.n_matches >= 10:
        dom_diff = stats_a.dominance_rate - stats_b.dominance_rate
        adjustment = max(-0.03, min(0.03, dom_diff * 0.30))
        p_a = max(0.02, min(0.98, p_a + adjustment))

    # Hebel 3 — Bayesian-Uncertainty-Dämpfung (Rollout 2026-08-02).
    # Ablation-Backtest zeigte +5.26pp ROI overall; wta500 skip weil dort Regression.
    if category not in _BAYESIAN_SKIP_CATEGORIES:
        n_a = ratings.get_surface_count(elo_key_a, surface)
        n_b = ratings.get_surface_count(elo_key_b, surface)
        p_a = bayesian_dampen(p_a, n_a, n_b)

    # Hebel 1 — Altitude-Boost auf Aufschläger.
    # Greift nur wenn (a) Höhen-Venue UND (b) Serve-Bias-Delta bekannt.
    # Serve-Bias aus dominance_rate der letzten 20 (TA-live-Stats).
    if tournament_slug and stats_a and stats_b \
            and stats_a.n_matches >= 10 and stats_b.n_matches >= 10:
        serve_bias_a = (stats_a.dominance_rate - 0.5) * 2  # in [-1, +1]
        serve_bias_b = (stats_b.dominance_rate - 0.5) * 2
        p_a = altitude_adjust(
            p_a, tournament_slug,
            serve_bias_a=serve_bias_a, serve_bias_b=serve_bias_b,
        )

    # Hebel 2 — Style-Cluster Matchup-Bias (max ±3pp).
    # Backtest zeigte marginal negativ (−0.22pp), aber conditional-on-good-classification
    # potenziell besser. Verwendet echte TA-Live-Serve-Stats (nicht Proxy).
    if stats_a and stats_b:
        style_a = classify_style(stats_a)
        style_b = classify_style(stats_b)
        style_bias = style_matchup_edge(style_a, style_b)
        if style_bias != 0.0:
            p_a = max(0.02, min(0.98, p_a + style_bias))

    # Surface-Calibrator (J8-I10): per-Surface Isotonic aus tennis_recalibrate.py.
    # Hat Vorrang über globalen Meta-Calibrator (spezifischer → bevorzugt).
    # Symmetric post-calibration: f_sym(x) = (f(x) + (1 - f(1-x))) / 2 guarantees
    # P(A>B) + P(B>A) = 1 regardless of which player was listed first in the API call.
    # An asymmetric isotonic calibrator (trained with ordering bias) violates this
    # property and can shift the actionable side by up to 13.8pp — Option A fix.
    surf_cal = _load_surface_calibrator(surface.lower())
    if surf_cal is not None:
        try:
            p_fwd = float(surf_cal.predict([p_a])[0])
            p_rev = float(surf_cal.predict([1.0 - p_a])[0])
            p_a = (p_fwd + (1.0 - p_rev)) / 2.0
            p_a = max(0.02, min(0.98, p_a))
        except Exception:
            pass

    # Meta-Calibrator: globaler Korrektiv-Layer (ab 50 Samples, nur wenn kein Surface-Cal).
    # FND-MODEL1-016: complement-symmetric semantics — same pattern as surface calibrator.
    # Raw F(p) breaks P(A>B) + P(B>A) = 1 for clay/grass (no surface calibrator exists).
    # p_sym = (F(p) + 1 - F(1-p)) / 2 guarantees complement symmetry.
    if surf_cal is None:
        meta_cal = _load_meta_calibrator()
        if meta_cal is not None:
            try:
                raw_p = p_a
                cal_ab = float(meta_cal.predict([raw_p])[0])
                cal_ba = float(meta_cal.predict([1.0 - raw_p])[0])
                p_a = (cal_ab + 1.0 - cal_ba) / 2.0
                p_a = max(0.02, min(0.98, p_a))
            except Exception:  # noqa: BLE001, S110
                pass

    return {"p_a": p_a, "p_b": 1.0 - p_a, "source": "ensemble"}
