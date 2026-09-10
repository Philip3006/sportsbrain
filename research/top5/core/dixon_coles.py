"""Research Dixon-Coles implementation for Top-5 generic framework.

Clean research-only version — no WC2026, altitude, turf, tournament weights,
or Elo-scale adjustment inside fitting. Matches the exact algorithm invoked by
BL1 v7 (frozen reference 569741b4ad571a38492e4d7cfd014cf82daec396):

  dixon_coles.fit(df, phi=phi, today=today, regularization=reg, max_iter=N)
  dixon_coles.predict_match(home, away, params)

Results are bit-identical to the production module when called with the same
simplified arguments (no elo_series, no cluster_map, no wc2026_boost_override).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

_TAU_EPSILON = 1e-6
_MAX_GOALS = 10
_MAX_LAMBDA = 4.5

# Optimizer bounds — identical to production version used by BL1 v7.
_FIT_BOUNDS_ATTACK = (-3.0, 2.5)
_FIT_BOUNDS_DEFENCE = (-3.5, 2.0)
_FIT_BOUNDS_HOME_ADV = (0.0, 0.6)
_FIT_BOUNDS_RHO = (-0.50, 0.10)


@dataclass
class DixonColesParams:
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    home_adv: float = 0.0
    rho: float = -0.1
    fit_date: pd.Timestamp = field(default_factory=pd.Timestamp.now)


def _tau(lh: np.ndarray, la: np.ndarray, rho: float,
         m00: np.ndarray, m10: np.ndarray, m01: np.ndarray, m11: np.ndarray) -> np.ndarray:
    """Vectorized Dixon-Coles tau correction."""
    log_tau = np.zeros(len(lh))
    if m00.any():
        log_tau[m00] = np.log(np.maximum(_TAU_EPSILON, 1.0 - lh[m00] * la[m00] * rho))
    if m10.any():
        log_tau[m10] = np.log(np.maximum(_TAU_EPSILON, 1.0 + la[m10] * rho))
    if m01.any():
        log_tau[m01] = np.log(np.maximum(_TAU_EPSILON, 1.0 + lh[m01] * rho))
    if m11.any():
        log_tau[m11] = np.log(max(_TAU_EPSILON, 1.0 - rho))
    return log_tau


def _nll(params_vec: np.ndarray, n: int, ref_idx: int,
         home_idx: np.ndarray, away_idx: np.ndarray,
         hg: np.ndarray, ag: np.ndarray, weights: np.ndarray,
         log_fac_hg: np.ndarray, log_fac_ag: np.ndarray,
         m00: np.ndarray, m10: np.ndarray, m01: np.ndarray, m11: np.ndarray,
         regularization: float) -> float:
    attack = params_vec[:n].copy()
    attack[ref_idx] = 0.0
    defence = params_vec[n: 2 * n]
    gamma = params_vec[2 * n]
    rho = params_vec[2 * n + 1]

    log_lh = attack[home_idx] + defence[away_idx] + gamma
    log_la = attack[away_idx] + defence[home_idx]
    lh = np.exp(log_lh)
    la = np.exp(log_la)

    log_p_hg = hg * log_lh - lh - log_fac_hg
    log_p_ag = ag * log_la - la - log_fac_ag
    log_tau = _tau(lh, la, rho, m00, m10, m01, m11)

    nll = -float((weights * (log_p_hg + log_p_ag + log_tau)).sum())
    if regularization > 0.0:
        nll += regularization * (float(np.dot(attack, attack)) + float(np.dot(defence, defence)))
    return nll


def fit(matches: pd.DataFrame, phi: float = 0.0012, today: pd.Timestamp | None = None,
        regularization: float = 0.005, max_iter: int = 1500) -> DixonColesParams:
    """Fit Dixon-Coles model on matches with time-decay and L2 regularization.

    Args:
        matches: DataFrame with home_team, away_team, home_score, away_score, date
        phi: time-decay rate (per day). phi=0 = equal weighting.
        today: reference date for time-decay. Defaults to max(date)+1.
        regularization: L2 regularization coefficient.
        max_iter: scipy optimizer max iterations.

    Returns:
        DixonColesParams with fit_date=today.
    """
    if today is None:
        today = matches["date"].max() + pd.Timedelta(days=1)
    if not isinstance(today, pd.Timestamp):
        today = pd.Timestamp(today)

    m = matches.dropna(subset=["home_score", "away_score"]).reset_index(drop=True)
    teams = sorted(set(m["home_team"].tolist() + m["away_team"].tolist()))
    n = len(teams)
    reference_team = teams[0]
    team_idx = {t: i for i, t in enumerate(teams)}
    ref_idx = team_idx[reference_team]

    home_idx = m["home_team"].map(team_idx).values.astype(np.int32)
    away_idx = m["away_team"].map(team_idx).values.astype(np.int32)
    hg = np.minimum(m["home_score"].to_numpy(dtype=np.int32, na_value=0), 7)
    ag = np.minimum(m["away_score"].to_numpy(dtype=np.int32, na_value=0), 7)
    days = (today - pd.to_datetime(m["date"])).dt.days.values.clip(min=0).astype(np.float64)
    weights = np.exp(-phi * days)

    log_fac_hg = gammaln(hg + 1)
    log_fac_ag = gammaln(ag + 1)
    m00 = (hg == 0) & (ag == 0)
    m10 = (hg == 1) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m11 = (hg == 1) & (ag == 1)

    mean_home_goals = float(m["home_score"].mean())
    fallback_atk = np.log(max(mean_home_goals, 0.5))
    init_attack = np.full(n, fallback_atk)
    init_defence = np.zeros(n)
    x0 = np.concatenate([init_attack, init_defence, [0.3, -0.1]])

    a_lo, a_hi = _FIT_BOUNDS_ATTACK
    d_lo, d_hi = _FIT_BOUNDS_DEFENCE
    h_lo, h_hi = _FIT_BOUNDS_HOME_ADV
    r_lo, r_hi = _FIT_BOUNDS_RHO
    x0[:n] = np.clip(x0[:n], a_lo, a_hi)
    x0[n:2 * n] = np.clip(x0[n:2 * n], d_lo, d_hi)
    x0[2 * n] = np.clip(x0[2 * n], h_lo, h_hi)
    x0[2 * n + 1] = np.clip(x0[2 * n + 1], r_lo, r_hi)

    bounds = ([(a_lo, a_hi)] * n + [(d_lo, d_hi)] * n + [(h_lo, h_hi), (r_lo, r_hi)])
    args = (n, ref_idx, home_idx, away_idx, hg, ag, weights,
            log_fac_hg, log_fac_ag, m00, m10, m01, m11, regularization)

    result = minimize(_nll, x0, args=args, method="L-BFGS-B", bounds=bounds,
                      options={"maxiter": max_iter, "ftol": 1e-9})

    params_vec = result.x
    attack_vec = params_vec[:n].copy()
    attack_vec[ref_idx] = 0.0
    attack = dict(zip(teams, attack_vec))
    defence = dict(zip(teams, params_vec[n: 2 * n]))
    return DixonColesParams(
        attack=attack,
        defence=defence,
        home_adv=float(params_vec[2 * n]),
        rho=float(params_vec[2 * n + 1]),
        fit_date=today,
    )


def predict_match(home: str, away: str, params: DixonColesParams,
                  max_goals: int = _MAX_GOALS) -> dict[str, float]:
    """Returns {'p_home': float, 'p_draw': float, 'p_away': float}."""
    if home not in params.attack or away not in params.attack:
        return {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}
    lh = min(np.exp(params.attack[home] + params.defence[away] + params.home_adv), _MAX_LAMBDA)
    la = min(np.exp(params.attack[away] + params.defence[home]), _MAX_LAMBDA)
    rho = params.rho
    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            tau_val: float
            if i == 0 and j == 0:
                tau_val = max(_TAU_EPSILON, 1.0 - lh * la * rho)
            elif i == 1 and j == 0:
                tau_val = max(_TAU_EPSILON, 1.0 + la * rho)
            elif i == 0 and j == 1:
                tau_val = max(_TAU_EPSILON, 1.0 + lh * rho)
            elif i == 1 and j == 1:
                tau_val = max(_TAU_EPSILON, 1.0 - rho)
            else:
                tau_val = 1.0
            matrix[i, j] = poisson.pmf(i, lh) * poisson.pmf(j, la) * tau_val
    total = matrix.sum()
    if total > 0:
        matrix /= total
    p_home = float(np.tril(matrix, -1).sum())
    p_draw = float(np.trace(matrix))
    p_away = float(np.triu(matrix, 1).sum())
    total2 = p_home + p_draw + p_away
    return {"p_home": p_home / total2, "p_draw": p_draw / total2, "p_away": p_away / total2}
