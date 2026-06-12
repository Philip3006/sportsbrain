import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

from src.config import DC_PHI, TOURNAMENT_WEIGHTS

_TAU_EPSILON = 1e-6
_MAX_GOALS = 10


@dataclass
class DixonColesParams:
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    home_adv: float = 0.0
    rho: float = -0.1
    fit_date: pd.Timestamp = field(default_factory=pd.Timestamp.now)


def _tau(x: int, y: int, lh: float, la: float, rho: float) -> float:
    if x == 0 and y == 0:
        return max(_TAU_EPSILON, 1.0 - lh * la * rho)
    if x == 1 and y == 0:
        return max(_TAU_EPSILON, 1.0 + la * rho)
    if x == 0 and y == 1:
        return max(_TAU_EPSILON, 1.0 + lh * rho)
    if x == 1 and y == 1:
        return max(_TAU_EPSILON, 1.0 - rho)
    return 1.0


def _lambdas(
    home: str,
    away: str,
    params: DixonColesParams,
    neutral: bool = False,
) -> tuple[float, float]:
    unknown = [t for t in (home, away) if t not in params.attack]
    if unknown:
        raise ValueError(
            f"Team(s) not in DC model: {unknown}. "
            "Run canonical_name() before predict, or retrain with updated data."
        )
    gamma = 0.0 if neutral else params.home_adv
    lh = np.exp(params.attack[home] + params.defence[away] + gamma)
    la = np.exp(params.attack[away] + params.defence[home])
    return lh, la


def _match_log_likelihood(
    hg: int, ag: int, lh: float, la: float, rho: float, weight: float = 1.0
) -> float:
    t = _tau(hg, ag, lh, la, rho)
    ll = (
        poisson.logpmf(hg, lh)
        + poisson.logpmf(ag, la)
        + np.log(t)
    )
    return weight * ll


def _prepare_arrays(
    matches: pd.DataFrame,
    team_idx: dict[str, int],
    phi: float,
    today: pd.Timestamp,
) -> tuple:
    """Pre-computes fixed numpy arrays for vectorized NLL. Called once before optimize."""
    known = matches["home_team"].isin(team_idx) & matches["away_team"].isin(team_idx)
    m = matches[known].reset_index(drop=True)

    home_idx = m["home_team"].map(team_idx).values.astype(np.int32)
    away_idx = m["away_team"].map(team_idx).values.astype(np.int32)
    # Cap goals at 7: prevents blowout wins (14-0 vs minnows) from dominating the NLL.
    # A 7-0 is already a very strong signal; 14-0 adds noise, not information.
    hg = np.minimum(m["home_score"].to_numpy(dtype=np.int32, na_value=0), 7)
    ag = np.minimum(m["away_score"].to_numpy(dtype=np.int32, na_value=0), 7)
    days = (today - m["date"]).dt.days.values.clip(min=0).astype(np.float64)
    weights = np.exp(-phi * days)

    # Apply tournament quality weights: final tournaments count more than qualifiers.
    # Reduces confederation bias (teams dominating weak qualifiers get inflated params otherwise).
    if "tournament" in m.columns:
        tourn_w = m["tournament"].map(TOURNAMENT_WEIGHTS).fillna(0.65).values
        weights = weights * tourn_w

    neutral = m.get("neutral", pd.Series(False, index=m.index)).fillna(False).values.astype(bool)

    # Pre-compute log(k!) for Poisson log-pmf  (gammaln(k+1) = log(k!))
    log_fac_hg = gammaln(hg + 1)
    log_fac_ag = gammaln(ag + 1)

    # Boolean masks for tau correction (low-score matches only)
    m00 = (hg == 0) & (ag == 0)
    m10 = (hg == 1) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m11 = (hg == 1) & (ag == 1)

    return (home_idx, away_idx, hg, ag, weights, neutral,
            log_fac_hg, log_fac_ag, m00, m10, m01, m11)


def _vectorized_nll(
    params_vec: np.ndarray,
    n: int,
    ref_idx: int,
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    hg: np.ndarray,
    ag: np.ndarray,
    weights: np.ndarray,
    neutral: np.ndarray,
    log_fac_hg: np.ndarray,
    log_fac_ag: np.ndarray,
    m00: np.ndarray,
    m10: np.ndarray,
    m01: np.ndarray,
    m11: np.ndarray,
) -> float:
    attack = params_vec[:n].copy()
    attack[ref_idx] = 0.0
    defence = params_vec[n : 2 * n]
    gamma = params_vec[2 * n]
    rho = params_vec[2 * n + 1]

    # Vectorized lambda computation via index arrays
    gamma_eff = np.where(neutral, 0.0, gamma)
    log_lh = attack[home_idx] + defence[away_idx] + gamma_eff
    log_la = attack[away_idx] + defence[home_idx]
    lh = np.exp(log_lh)
    la = np.exp(log_la)

    # Poisson log-pmf: k*log(lambda) - lambda - log(k!)
    log_p_hg = hg * log_lh - lh - log_fac_hg
    log_p_ag = ag * log_la - la - log_fac_ag

    # Tau correction (vectorized, only low-score cells)
    log_tau = np.zeros(len(hg))
    if m00.any():
        log_tau[m00] = np.log(np.maximum(_TAU_EPSILON, 1.0 - lh[m00] * la[m00] * rho))
    if m10.any():
        log_tau[m10] = np.log(np.maximum(_TAU_EPSILON, 1.0 + la[m10] * rho))
    if m01.any():
        log_tau[m01] = np.log(np.maximum(_TAU_EPSILON, 1.0 + lh[m01] * rho))
    if m11.any():
        log_tau[m11] = np.log(max(_TAU_EPSILON, 1.0 - rho))

    return -float((weights * (log_p_hg + log_p_ag + log_tau)).sum())


def negative_log_likelihood(
    params_vec: np.ndarray,
    matches: pd.DataFrame,
    teams: list[str],
    reference_team: str,
    phi: float = DC_PHI,
    today: pd.Timestamp | None = None,
) -> float:
    """Scalar NLL — used for tests and one-off calls. fit() uses vectorized version."""
    if today is None:
        today = matches["date"].max() + pd.Timedelta(days=1)
    team_idx = {t: i for i, t in enumerate(teams)}
    ref_idx = team_idx[reference_team]
    arrays = _prepare_arrays(matches, team_idx, phi, today)
    return _vectorized_nll(params_vec, len(teams), ref_idx, *arrays)


def fit(
    matches: pd.DataFrame,
    phi: float = DC_PHI,
    today: pd.Timestamp | None = None,
    method: str = "L-BFGS-B",
    max_iter: int = 2000,
) -> DixonColesParams:
    """Fits Dixon-Coles model. Returns DixonColesParams."""
    if today is None:
        today = matches["date"].max() + pd.Timedelta(days=1)

    teams = sorted(
        set(matches["home_team"].tolist() + matches["away_team"].tolist())
    )
    n = len(teams)
    reference_team = teams[0]
    team_idx = {t: i for i, t in enumerate(teams)}
    ref_idx = team_idx[reference_team]

    # Pre-compute fixed arrays once (not on every NLL call)
    arrays = _prepare_arrays(matches, team_idx, phi, today)

    mean_home_goals = float(matches["home_score"].mean())
    init_attack = np.full(n, np.log(max(mean_home_goals, 0.5)))
    init_defence = np.zeros(n)
    x0 = np.concatenate([init_attack, init_defence, [0.3, -0.1]])

    bounds = [(None, None)] * (2 * n) + [(None, None), (-0.5, 0.0)]

    result = minimize(
        _vectorized_nll,
        x0,
        args=(n, ref_idx, *arrays),
        method=method,
        bounds=bounds,
        options={"maxiter": max_iter, "ftol": 1e-9},
    )

    params_vec = result.x
    attack = dict(zip(teams, params_vec[:n]))
    attack[reference_team] = 0.0
    defence = dict(zip(teams, params_vec[n : 2 * n]))

    return DixonColesParams(
        attack=attack,
        defence=defence,
        home_adv=float(params_vec[2 * n]),
        rho=float(params_vec[2 * n + 1]),
        fit_date=today,
    )


def predict_scoreline(
    home: str,
    away: str,
    params: DixonColesParams,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> np.ndarray:
    """Returns (max_goals+1 x max_goals+1) matrix of P(home_goals=i, away_goals=j).
    rho_override: replaces params.rho when set (used for stage-specific calibration).
    """
    lh, la = _lambdas(home, away, params, neutral)
    rho = rho_override if rho_override is not None else params.rho
    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            t = _tau(i, j, lh, la, rho)
            matrix[i, j] = poisson.pmf(i, lh) * poisson.pmf(j, la) * t
    # Renormalize to account for truncation
    total = matrix.sum()
    if total > 0:
        matrix /= total
    return matrix


def predict_match(
    home: str,
    away: str,
    params: DixonColesParams,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict[str, float]:
    """Returns {'p_home': float, 'p_draw': float, 'p_away': float}.
    rho_override: applies stage-specific low-score correction (see predict_match_staged).
    """
    matrix = predict_scoreline(home, away, params, max_goals, neutral, rho_override)
    p_home = float(np.tril(matrix, -1).sum())
    p_draw = float(np.trace(matrix))
    p_away = float(np.triu(matrix, 1).sum())
    total = p_home + p_draw + p_away
    return {
        "p_home": p_home / total,
        "p_draw": p_draw / total,
        "p_away": p_away / total,
    }


_RHO_FACTORS_CACHE: dict | None = None


def _load_rho_factors() -> dict[str, float]:
    """Loads empirically-fit rho factors from rho_stages.json (built by
    scripts/fit_rho_stages.py via walk-forward DC snapshots).
    Falls back to hardcoded defaults if file missing.
    """
    global _RHO_FACTORS_CACHE
    if _RHO_FACTORS_CACHE is not None:
        return _RHO_FACTORS_CACHE
    import json as _json
    from src.config import MODELS_DIR
    defaults = {"group": 1.10, "r16": 0.75, "qf": 0.75, "sf": 0.75,
                "third_place": 0.75, "final": 0.75}
    path = MODELS_DIR / "dixon_coles" / "rho_stages.json"
    if path.exists():
        try:
            data = _json.loads(path.read_text())
            for stage, meta in data.items():
                if isinstance(meta, dict) and "shrunk" in meta:
                    defaults[stage] = float(meta["shrunk"])
        except Exception:
            pass
    _RHO_FACTORS_CACHE = defaults
    return defaults


def predict_match_staged(
    home: str,
    away: str,
    params: DixonColesParams,
    is_knockout: bool = False,
    neutral: bool = False,
    stage: str | None = None,
) -> dict[str, float]:
    """
    Stage-aware prediction. Rho factors are loaded from rho_stages.json
    (fit by scripts/fit_rho_stages.py with walk-forward DC snapshots; no leakage).

    If `stage` is provided ("group", "r16", "qf", "sf", "third_place", "final"),
    uses that specific factor. Otherwise falls back to binary is_knockout:
    knockout → mean of KO-stage factors weighted by sample size; else group.
    """
    factors = _load_rho_factors()
    if stage and stage in factors:
        rho_factor = factors[stage]
    elif is_knockout:
        # KO-weighted average (n: r16=192, qf=96, sf=48, 3rd=7, final=24 from rho_stages.json)
        rho_factor = (factors.get("r16", 0.0) * 192
                      + factors.get("qf", 0.0) * 96
                      + factors.get("sf", 0.0) * 48
                      + factors.get("third_place", 0.0) * 7
                      + factors.get("final", 0.0) * 24) / 367.0
    else:
        rho_factor = factors.get("group", 1.10)
    rho = params.rho * rho_factor
    return predict_match(home, away, params, neutral=neutral, rho_override=rho)


def get_stage_rho(params: DixonColesParams, stage: str | None,
                  is_knockout: bool = False) -> float:
    """Returns the rho override value the scanner should pass to other
    market-prediction functions (predict_totals, predict_btts, etc.)
    so they stay consistent with the staged 1X2 prediction.
    """
    factors = _load_rho_factors()
    if stage and stage in factors:
        rho_factor = factors[stage]
    elif is_knockout:
        rho_factor = (factors.get("r16", 0.0) * 192
                      + factors.get("qf", 0.0) * 96
                      + factors.get("sf", 0.0) * 48
                      + factors.get("third_place", 0.0) * 7
                      + factors.get("final", 0.0) * 24) / 367.0
    else:
        rho_factor = factors.get("group", 1.10)
    return params.rho * rho_factor


def predict_goals_distribution(
    home: str,
    away: str,
    params: DixonColesParams,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
) -> dict[str, np.ndarray]:
    """Returns marginal goal distributions for each team."""
    matrix = predict_scoreline(home, away, params, max_goals, neutral)
    return {
        "home_goals": matrix.sum(axis=1),
        "away_goals": matrix.sum(axis=0),
        "expected_home": float((np.arange(max_goals + 1) * matrix.sum(axis=1)).sum()),
        "expected_away": float((np.arange(max_goals + 1) * matrix.sum(axis=0)).sum()),
    }


def predict_asian_handicap(
    home: str,
    away: str,
    params: DixonColesParams,
    line: float = -0.5,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict[str, float]:
    """
    Asian Handicap prediction for line values: -0.5, -1.0, -1.5, +0.5, +1.0, +1.5.

    For half-line handicaps (±0.5, ±1.5): no push possible.
    For whole-line handicaps (±1.0): push possible when home wins by exactly |line| goals.

    Returns dict with keys p_ah_home, p_ah_away, p_push.
    p_ah_home + p_ah_away + p_push == 1.0 (within floating-point tolerance).

    line=-0.5 (home -0.5): home wins outright → AH home wins; draw/away → AH away wins.
    line=-1.0 (home -1.0): home wins by 2+ → AH home; home wins by exactly 1 → push; else → AH away.
    line=-1.5 (home -1.5): home wins by 2+ → AH home; else → AH away (no push).
    line=+0.5 (home +0.5): home wins or draws → AH home; away wins → AH away (no push).
    line=+1.0 (home +1.0): away wins by 2+ → AH away; away wins by exactly 1 → push; else → AH home.
    line=+1.5 (home +1.5): away wins by 2+ → AH away; else → AH home (no push).
    """
    _SUPPORTED_LINES = {-0.5, -1.0, -1.5, -2.0, -2.5, 0.5, 1.0, 1.5, 2.0, 2.5}
    if line not in _SUPPORTED_LINES:
        raise ValueError(f"Unsupported AH line: {line}. Supported: {sorted(_SUPPORTED_LINES)}")

    matrix = predict_scoreline(home, away, params, max_goals, neutral, rho_override)

    p_ah_home = 0.0
    p_ah_away = 0.0
    p_push = 0.0

    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            prob = matrix[i, j]
            diff = i - j  # home goals minus away goals
            if line == -0.5:
                # Home wins outright (diff > 0) → AH home; else → AH away
                if diff > 0:
                    p_ah_home += prob
                else:
                    p_ah_away += prob
            elif line == -1.0:
                # Home wins by 2+ → AH home; home wins by exactly 1 → push; else → AH away
                if diff >= 2:
                    p_ah_home += prob
                elif diff == 1:
                    p_push += prob
                else:
                    p_ah_away += prob
            elif line == -1.5:
                # Home wins by 2+ → AH home; else → AH away (no push)
                if diff >= 2:
                    p_ah_home += prob
                else:
                    p_ah_away += prob
            elif line == 0.5:
                # Home wins or draws (diff >= 0) → AH home; away wins → AH away
                if diff >= 0:
                    p_ah_home += prob
                else:
                    p_ah_away += prob
            elif line == 1.0:
                # Away wins by 2+ → AH away; away wins by exactly 1 → push; else → AH home
                if diff <= -2:
                    p_ah_away += prob
                elif diff == -1:
                    p_push += prob
                else:
                    p_ah_home += prob
            elif line == 1.5:
                # Away wins by 2+ → AH away; else → AH home (no push)
                if diff <= -2:
                    p_ah_away += prob
                else:
                    p_ah_home += prob
            elif line == -2.0:
                # Home wins by 3+ → AH home; home wins by exactly 2 → push; else → AH away
                if diff >= 3:
                    p_ah_home += prob
                elif diff == 2:
                    p_push += prob
                else:
                    p_ah_away += prob
            elif line == -2.5:
                # Home wins by 3+ → AH home; else → AH away (no push)
                if diff >= 3:
                    p_ah_home += prob
                else:
                    p_ah_away += prob
            elif line == 2.0:
                # Away wins by 3+ → AH away; away wins by exactly 2 → push; else → AH home
                if diff <= -3:
                    p_ah_away += prob
                elif diff == -2:
                    p_push += prob
                else:
                    p_ah_home += prob
            elif line == 2.5:
                # Away wins by 3+ → AH away; else → AH home (no push)
                if diff <= -3:
                    p_ah_away += prob
                else:
                    p_ah_home += prob

    return {
        "p_ah_home": float(p_ah_home),
        "p_ah_away": float(p_ah_away),
        "p_push": float(p_push),
    }


def predict_totals(
    home: str,
    away: str,
    params: DixonColesParams,
    line: float = 2.5,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict[str, float]:
    """Returns P(total goals > line), P(push), P(under) from scoreline matrix.
    Whole-ball lines (e.g., 2.0, 3.0) have p_push > 0 at the exact goal total.
    Half-ball lines (e.g., 2.5) have p_push = 0.
    """
    matrix = predict_scoreline(home, away, params, max_goals, neutral, rho_override)
    is_whole = abs(line % 1) < 0.001
    p_over = 0.0
    p_push = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            total = i + j
            if total > line:
                p_over += matrix[i, j]
            elif is_whole and abs(total - line) < 0.001:
                p_push += matrix[i, j]
    return {
        "p_over": float(p_over),
        "p_under": float(1.0 - p_over - p_push),
        "p_push": float(p_push),
        "line": line,
    }


def predict_totals_all(
    home: str,
    away: str,
    params: DixonColesParams,
    line: float,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict:
    """Predict O/U for any line type: half-ball (.5), whole-ball (.0), or quarter-ball (.25/.75).
    Quarter-ball lines return lower_probs/upper_probs for EV computation.
    """
    import math
    remainder = round((line * 4) % 2)  # 0=whole, 2=half, 1=quarter
    if remainder == 1:
        lower = math.floor(line * 2) / 2  # e.g., 2.25 → 2.0
        upper = math.ceil(line * 2) / 2   # e.g., 2.25 → 2.5
        p_lower = predict_totals(home, away, params, lower, max_goals, neutral, rho_override)
        p_upper = predict_totals(home, away, params, upper, max_goals, neutral, rho_override)
        return {
            "p_over": 0.5 * (p_lower["p_over"] + p_upper["p_over"]),
            "p_under": 0.5 * (p_lower["p_under"] + p_upper["p_under"]),
            "p_push": 0.0,
            "lower_probs": p_lower,
            "upper_probs": p_upper,
            "quarter_ball": True,
            "line": line,
        }
    return predict_totals(home, away, params, line, max_goals, neutral, rho_override)


def predict_asian_handicap_all(
    home: str,
    away: str,
    params: DixonColesParams,
    line: float,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict:
    """Predict AH for any line type, including quarter-ball (e.g., -1.25, -0.75).
    Quarter-ball lines return lower_probs/upper_probs for direct EV computation.
    """
    import math
    remainder = round((line * 4) % 2)  # 0=whole, 2=half, 1=quarter
    if remainder == 1:
        lower = math.ceil(line * 2) / 2   # less aggressive (closer to 0)
        upper = math.floor(line * 2) / 2  # more aggressive (farther from 0)
        p_lower = predict_asian_handicap(home, away, params, lower, max_goals, neutral, rho_override)
        p_upper = predict_asian_handicap(home, away, params, upper, max_goals, neutral, rho_override)
        return {
            "p_ah_home": 0.5 * (p_lower["p_ah_home"] + p_upper["p_ah_home"]),
            "p_ah_away": 0.5 * (p_lower["p_ah_away"] + p_upper["p_ah_away"]),
            "p_push": 0.0,
            "lower_probs": p_lower,
            "upper_probs": p_upper,
            "quarter_ball": True,
            "line": line,
        }
    return predict_asian_handicap(home, away, params, line, max_goals, neutral, rho_override)


def predict_btts(
    home: str,
    away: str,
    params: DixonColesParams,
    max_goals: int = _MAX_GOALS,
    neutral: bool = False,
    rho_override: float | None = None,
) -> dict[str, float]:
    """Returns P(both teams score >= 1) and P(at least one team scores 0)."""
    matrix = predict_scoreline(home, away, params, max_goals, neutral, rho_override)
    p_yes = float(matrix[1:, 1:].sum())  # all cells where home >= 1 AND away >= 1
    return {"p_btts_yes": p_yes, "p_btts_no": float(1.0 - p_yes)}


def predict_xg(
    home: str,
    away: str,
    params: DixonColesParams,
    neutral: bool = False,
) -> tuple[float, float]:
    """Returns (xg_home, xg_away) — the Poisson goal rates for each team."""
    lh, la = _lambdas(home, away, params, neutral)
    return float(lh), float(la)


def predict_first_scorer(
    home: str,
    away: str,
    params: DixonColesParams,
    neutral: bool = False,
) -> dict[str, float]:
    """P(home scores first) — exponential race: each team's rate is λ, first scorer wins."""
    lh, la = _lambdas(home, away, params, neutral=neutral)
    p_home = lh / (lh + la)
    return {"p_home_first": float(p_home), "p_away_first": float(1.0 - p_home)}


def save(params: DixonColesParams, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(params, f)


def load(path: Path) -> DixonColesParams:
    with open(path, "rb") as f:
        return pickle.load(f)
