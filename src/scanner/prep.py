"""
Model loaders, data adjusters, and feature helpers for the daily scan pipeline.
All functions here are stateless pure helpers or thin wrappers around model I/O.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from src.config import MODELS_DIR
from src.models import dixon_coles as dc
from src.data.squad_availability import SquadReport
from src.features.form import momentum_score, match_load, form_direction_label

_WM_2026_START = datetime(2026, 6, 11)
_WM_2026_END = datetime(2026, 7, 19)


def _is_wm_active(today: datetime | None = None) -> bool:
    """Returns True if WM 2026 is currently running (matches possible)."""
    today = today or datetime.now()
    return _WM_2026_START <= today <= _WM_2026_END + timedelta(days=1)


def _load_latest_dc_params() -> dc.DixonColesParams | None:
    snap_dir = MODELS_DIR / "dixon_coles"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("params_*.pkl"))
    if not files:
        return None
    return dc.load(files[-1])


def _load_lgbm_gate() -> dict:
    """Reads models/lgbm/gate.json (written by train_lgbm.py).
    Returns {'passed': bool, 'dc_weight': float, ...}. Missing file → not passed.
    """
    import json as _json
    path = MODELS_DIR / "lgbm" / "gate.json"
    if not path.exists():
        return {"passed": False, "dc_weight": 0.5, "reason": "no gate.json"}
    try:
        return _json.loads(path.read_text())
    except Exception as e:
        return {"passed": False, "dc_weight": 0.5, "reason": f"unreadable gate.json: {e}"}


def _load_latest_lgbm():
    """Loads LGBM model only if the ensemble gate passed (gate.json)."""
    gate = _load_lgbm_gate()
    if not gate.get("passed"):
        return None
    try:
        from src.models import lgbm_model
        model_path = MODELS_DIR / "lgbm" / "model.pkl"
        if model_path.exists():
            return lgbm_model.load_model(model_path)
    except ImportError:
        pass
    return None


def _load_calibrators():
    try:
        from src.ensemble.calibration import load_calibrators
        path = MODELS_DIR / "lgbm" / "calibrators.pkl"
        if path.exists():
            return load_calibrators(path)
    except Exception:
        pass
    return None


def _load_cluster_calibrators():
    try:
        from src.config import PER_CLUSTER_CALIBRATION_ENABLED
        if not PER_CLUSTER_CALIBRATION_ENABLED:
            return None
        from src.ensemble.calibration import load_cluster_calibrators
        path = MODELS_DIR / "lgbm" / "cluster_calibrators.pkl"
        if path.exists():
            return load_cluster_calibrators(path)
    except Exception:
        pass
    return None


def _load_stacker():
    try:
        from src.config import STACKER_ENABLED
        if not STACKER_ENABLED:
            return None
        from src.ensemble.stacking import Stacker
        path = MODELS_DIR / "lgbm" / "stacker.pkl"
        if path.exists():
            return Stacker.load(path)
    except Exception:
        pass
    return None


def _load_conformal():
    try:
        from src.config import CONFORMAL_ENABLED
        if not CONFORMAL_ENABLED:
            return None
        from src.ensemble.conformal import ConformalPredictor
        path = MODELS_DIR / "lgbm" / "conformal.pkl"
        if path.exists():
            return ConformalPredictor.load(path)
    except Exception:
        pass
    return None


def _squad_adjust(
    final_arr: np.ndarray,
    home_squad: SquadReport,
    away_squad: SquadReport,
    weight: float = 0.30,
) -> np.ndarray:
    """Shifts home/away win probs by squad availability difference.
    No-op only when BOTH sources are default (no real data at all).
    Covers.com, Transfermarkt, Wikipedia all count as real data.
    """
    both_default = (
        home_squad.data_source == "default"
        and away_squad.data_source == "default"
    )
    if both_default:
        return final_arr
    avail_diff = home_squad.availability_score - away_squad.availability_score
    shift = avail_diff * weight
    adjusted = final_arr.copy()
    adjusted[2] = max(0.01, adjusted[2] + shift)
    adjusted[0] = max(0.01, adjusted[0] - shift)
    adjusted[1] = max(0.01, adjusted[1])
    return adjusted / adjusted.sum()


def _rank_adjust(
    final_arr: np.ndarray,
    home: str,
    away: str,
    weight: float = 0.03,
) -> np.ndarray:
    """Small shift based on FIFA ranking difference. Complements Elo.
    Effect is capped at ±weight (3%). Applied after squad adjustment.
    Positive rank_diff = home is better ranked (lower rank number = stronger).
    """
    from src.data.fifa_rankings import get_fifa_rank_diff
    rank_diff = get_fifa_rank_diff(home, away)  # positive = home better ranked
    # Normalize: every 50 rank positions = full weight unit
    shift = float(np.clip(rank_diff / 50.0 * weight, -weight, weight))
    adjusted = final_arr.copy()
    adjusted[2] = max(0.01, adjusted[2] + shift)
    adjusted[0] = max(0.01, adjusted[0] - shift)
    adjusted[1] = max(0.01, adjusted[1])
    return adjusted / adjusted.sum()


def _form_context(team: str, scan_date: pd.Timestamp, historical: pd.DataFrame) -> dict:
    """Computes display-only form context for scan report."""
    mom = momentum_score(team, scan_date, historical)
    load = match_load(team, scan_date, historical)
    direction = form_direction_label(mom["form_trend"])
    fatigue = load["matches_30d"] >= 4
    return {
        "momentum": mom,
        "load": load,
        "direction": direction,
        "fatigue": fatigue,
    }


def _match_ts_utc(match: dict) -> pd.Timestamp:
    """Parses commence_time to UTC Timestamp, falls back to far future."""
    try:
        ts = pd.Timestamp(match.get("commence_time", ""))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts
    except Exception:
        return pd.Timestamp("2099-01-01", tz="UTC")
