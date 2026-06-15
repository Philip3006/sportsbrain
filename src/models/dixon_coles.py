import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson

from src.config import DC_ELO_SCALE, DC_PHI, TOURNAMENT_WEIGHTS, WC2026_BOOST, WC2026_START

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
    elo_home: float | None = None,
    elo_away: float | None = None,
    elo_scale: float = DC_ELO_SCALE,
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
    if elo_home is not None and elo_away is not None:
        elo_adj = np.exp((elo_home - elo_away) / elo_scale)
        lh *= elo_adj
        la /= elo_adj
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
    wc2026_boost_override: float | None = None,
    elo_scale: float | None = None,
) -> tuple:
    """Pre-computes fixed numpy arrays for vectorized NLL. Called once before optimize.

    wc2026_boost_override: if set, replaces config.WC2026_BOOST for this fit only.
        Used by retry-logic in train_dixon_coles.py when the optimizer hits a bound
        (a sign the boost is over-fitting one team's calibration to a single match).
    """
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

        # WM 2026 freshness boost: current-tournament matches carry the most
        # signal about present team form (fitness, tactical shape, manager).
        wc2026_boost = WC2026_BOOST if wc2026_boost_override is None else wc2026_boost_override
        wc2026_mask = (
            (m["tournament"] == "FIFA World Cup")
            & (m["date"] >= pd.Timestamp(WC2026_START))
        ).values
        if wc2026_mask.any() and wc2026_boost != 1.0:
            weights = weights * np.where(wc2026_mask, wc2026_boost, 1.0)

    neutral = m.get("neutral", pd.Series(False, index=m.index)).fillna(False).values.astype(bool)

    # Elo-based opponent quality adjustment.
    # elo_adj > 1 when home team is stronger (higher Elo) → home expected goals boosted,
    # away expected goals reduced. Calibrates DC parameters cross-confederation: a 6-0
    # vs a 1100-Elo minnow is "expected" given the Elo gap, so Tunisia's attack param
    # no longer needs to be as high to explain it. Applied to both training weights and
    # the Poisson means inside _vectorized_nll.
    if elo_scale is not None and "elo_home_pre" in m.columns and "elo_away_pre" in m.columns:
        elo_diff = m["elo_home_pre"].values.astype(np.float64) - m["elo_away_pre"].values.astype(np.float64)
        elo_adj = np.exp(elo_diff / elo_scale)
    else:
        elo_adj = np.ones(len(m), dtype=np.float64)

    # Pre-compute log(k!) for Poisson log-pmf  (gammaln(k+1) = log(k!))
    log_fac_hg = gammaln(hg + 1)
    log_fac_ag = gammaln(ag + 1)

    # Boolean masks for tau correction (low-score matches only)
    m00 = (hg == 0) & (ag == 0)
    m10 = (hg == 1) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m11 = (hg == 1) & (ag == 1)

    return (home_idx, away_idx, hg, ag, weights, neutral,
            log_fac_hg, log_fac_ag, m00, m10, m01, m11, elo_adj)


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
    elo_adj: np.ndarray,
    regularization: float = 0.0,
    prior_attack: np.ndarray | None = None,
    prior_defence: np.ndarray | None = None,
    cluster_strength: float = 0.0,
    cluster_attack_center: np.ndarray | None = None,
    cluster_defence_center: np.ndarray | None = None,
) -> float:
    attack = params_vec[:n].copy()
    attack[ref_idx] = 0.0
    defence = params_vec[n : 2 * n]
    gamma = params_vec[2 * n]
    rho = params_vec[2 * n + 1]

    # Vectorized lambda computation via index arrays.
    # elo_adj encodes the Elo quality gap: exp((elo_home - elo_away) / DC_ELO_SCALE).
    # Multiplying lh by elo_adj means a strong-vs-weak match requires less DC attack
    # to explain the same scoreline — calibrating parameters cross-confederation.
    gamma_eff = np.where(neutral, 0.0, gamma)
    log_lh = attack[home_idx] + defence[away_idx] + gamma_eff
    log_la = attack[away_idx] + defence[home_idx]
    lh = np.exp(log_lh) * elo_adj
    la = np.exp(log_la) / elo_adj

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

    nll = -float((weights * (log_p_hg + log_p_ag + log_tau)).sum())

    if regularization > 0.0:
        # Bayesian prior: penalise deviation from prior_params when available.
        # This prevents a single boosted WM match from dominating a team's calibration
        # when the prior carries far more cumulative evidence than one game.
        # Falls back to standard L2 (shrink toward 0) when no prior is supplied.
        atk_center = prior_attack if prior_attack is not None else np.zeros(n)
        def_center = prior_defence if prior_defence is not None else np.zeros(n)
        nll += regularization * (
            float(np.dot(attack - atk_center, attack - atk_center))
            + float(np.dot(defence - def_center, defence - def_center))
        )

    if cluster_strength > 0.0 and cluster_attack_center is not None:
        # Hierarchical confederation prior (Phase 2.2):
        # Each team is softly shrunk toward its confederation cluster mean
        # (UEFA, CONMEBOL, …). Stops Cape Verde or Botswana from drifting
        # to extremes after one or two matches at WC level — the data has
        # one game, but the prior knows what CAF teams look like in
        # aggregate. cluster_strength tunes how much the cluster pulls vs.
        # the data; 0 disables the prior entirely.
        atk_cluster = cluster_attack_center
        def_cluster = cluster_defence_center if cluster_defence_center is not None else np.zeros(n)
        nll += cluster_strength * (
            float(np.dot(attack - atk_cluster, attack - atk_cluster))
            + float(np.dot(defence - def_cluster, defence - def_cluster))
        )

    return nll


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


# Optimizer hard bounds — kept tighter than validate_params() sanity ranges so
# that hitting a bound is a real signal (Mexico defence=-2.97 / rho=-0.5 in
# params_20260614.pkl both touched the previous wider bounds). When the optimizer
# hits a bound, _check_bounds_hit() reports it so the train script can retry with
# a smaller WC2026_BOOST.
_FIT_BOUNDS_ATTACK = (-3.0, 2.5)
_FIT_BOUNDS_DEFENCE = (-2.5, 2.0)
_FIT_BOUNDS_HOME_ADV = (0.0, 0.6)
_FIT_BOUNDS_RHO = (-0.30, 0.10)
_BOUND_TOLERANCE = 1e-3  # within this distance of a bound counts as "hit"


def _check_bounds_hit(
    params: "DixonColesParams",
    tolerance: float = _BOUND_TOLERANCE,
) -> dict[str, list]:
    """Returns dict of {param_group: [(name, value, hit_side)]} for params that
    touched the optimizer bounds. Empty lists = clean fit.

    hit_side ∈ {"low", "high"}. Used by train_dixon_coles.py to decide whether
    to retry with a reduced WC2026_BOOST.
    """
    hits: dict[str, list] = {"attack": [], "defence": [], "home_adv": [], "rho": []}
    a_lo, a_hi = _FIT_BOUNDS_ATTACK
    d_lo, d_hi = _FIT_BOUNDS_DEFENCE
    h_lo, h_hi = _FIT_BOUNDS_HOME_ADV
    r_lo, r_hi = _FIT_BOUNDS_RHO

    for t, v in params.attack.items():
        if abs(v - a_lo) < tolerance:
            hits["attack"].append((t, v, "low"))
        elif abs(v - a_hi) < tolerance:
            hits["attack"].append((t, v, "high"))
    for t, v in params.defence.items():
        if abs(v - d_lo) < tolerance:
            hits["defence"].append((t, v, "low"))
        elif abs(v - d_hi) < tolerance:
            hits["defence"].append((t, v, "high"))
    if abs(params.home_adv - h_lo) < tolerance:
        hits["home_adv"].append(("home_adv", params.home_adv, "low"))
    elif abs(params.home_adv - h_hi) < tolerance:
        hits["home_adv"].append(("home_adv", params.home_adv, "high"))
    if abs(params.rho - r_lo) < tolerance:
        hits["rho"].append(("rho", params.rho, "low"))
    elif abs(params.rho - r_hi) < tolerance:
        hits["rho"].append(("rho", params.rho, "high"))
    return hits


def _compute_cluster_centers(
    teams: list[str],
    prior_params: "DixonColesParams | None",
    cluster_map: dict[str, str] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """For each team, returns (attack_center, defence_center) shaped (n,).

    A team's center is the mean of its confederation cluster as observed in
    `prior_params`. Teams missing from `cluster_map` (e.g. small federations
    not enumerated in config.TEAM_CONFEDERATION) get the global prior mean.

    If `prior_params` is None we cannot derive a meaningful cluster mean, so
    the function returns zeros — i.e. the hierarchical penalty collapses to
    standard L2 shrinkage. The hierarchical effect only kicks in once a
    prior is available; subsequent retrains then refine the cluster picture.
    """
    n = len(teams)
    if prior_params is None or not cluster_map:
        return np.zeros(n), np.zeros(n)

    # Aggregate per-cluster mean from prior
    bucket_atk: dict[str, list[float]] = {}
    bucket_def: dict[str, list[float]] = {}
    for t in teams:
        c = cluster_map.get(t)
        if not c:
            continue
        if t in prior_params.attack:
            bucket_atk.setdefault(c, []).append(prior_params.attack[t])
        if t in prior_params.defence:
            bucket_def.setdefault(c, []).append(prior_params.defence[t])
    cluster_atk_mean = {c: float(np.mean(v)) for c, v in bucket_atk.items() if v}
    cluster_def_mean = {c: float(np.mean(v)) for c, v in bucket_def.items() if v}

    # Global fall-back for teams without a confederation entry
    global_atk = float(np.mean(list(prior_params.attack.values()))) if prior_params.attack else 0.0
    global_def = float(np.mean(list(prior_params.defence.values()))) if prior_params.defence else 0.0

    atk_center = np.array([
        cluster_atk_mean.get(cluster_map.get(t, ""), global_atk) for t in teams
    ])
    def_center = np.array([
        cluster_def_mean.get(cluster_map.get(t, ""), global_def) for t in teams
    ])
    return atk_center, def_center


def fit(
    matches: pd.DataFrame,
    phi: float = DC_PHI,
    today: pd.Timestamp | None = None,
    method: str = "L-BFGS-B",
    max_iter: int = 2000,
    prior_params: "DixonColesParams | None" = None,
    regularization: float = 0.005,
    wc2026_boost_override: float | None = None,
    cluster_map: dict[str, str] | None = None,
    cluster_strength: float = 0.0,
    elo_series: "pd.DataFrame | None" = None,
    elo_scale: float = DC_ELO_SCALE,
) -> DixonColesParams:
    """Fits Dixon-Coles model. Returns DixonColesParams.

    prior_params: warm-start the optimizer from a previous model's parameters
        instead of the global-average default. Prevents WM match boosts from
        pushing teams to counterintuitive extremes (e.g. a team scoring 4 goals
        having a lower attack than before the match).
    regularization: L2 penalty coefficient on attack/defence vectors. Keeps
        parameter shifts bounded when a small number of high-weight matches (WM
        with WC2026_BOOST) would otherwise explain a blowout entirely through
        one team's parameter.
    wc2026_boost_override: replaces config.WC2026_BOOST for this fit (retry-logic).
    cluster_map: optional {team: cluster_id} dict (e.g. TEAM_CONFEDERATION).
        When combined with cluster_strength > 0 and prior_params, applies a
        soft hierarchical prior that shrinks each team toward its cluster mean.
    cluster_strength: penalty coefficient for the hierarchical cluster prior.
        Defaults to 0 (no effect). Typical values: 0.01–0.05.
    """
    if today is None:
        today = matches["date"].max() + pd.Timedelta(days=1)

    # Join pre-match Elo columns when available — used by _prepare_arrays() to compute
    # elo_adj per match. The elo_series must be computed from the same (filtered) matches
    # DataFrame so indices align.
    if elo_series is not None:
        matches = matches.copy()
        matches["elo_home_pre"] = elo_series["elo_home_pre"].values
        matches["elo_away_pre"] = elo_series["elo_away_pre"].values

    teams = sorted(
        set(matches["home_team"].tolist() + matches["away_team"].tolist())
    )
    n = len(teams)
    reference_team = teams[0]
    team_idx = {t: i for i, t in enumerate(teams)}
    ref_idx = team_idx[reference_team]

    # Pre-compute fixed arrays once (not on every NLL call)
    arrays = _prepare_arrays(matches, team_idx, phi, today,
                              wc2026_boost_override=wc2026_boost_override,
                              elo_scale=elo_scale if elo_series is not None else None)

    mean_home_goals = float(matches["home_score"].mean())
    fallback_atk = np.log(max(mean_home_goals, 0.5))

    if prior_params is not None:
        init_attack = np.array([prior_params.attack.get(t, fallback_atk) for t in teams])
        init_defence = np.array([prior_params.defence.get(t, 0.0) for t in teams])
        x0 = np.concatenate([init_attack, init_defence,
                              [prior_params.home_adv, prior_params.rho]])
        prior_atk_vec = init_attack.copy()
        prior_def_vec = init_defence.copy()
    else:
        init_attack = np.full(n, fallback_atk)
        init_defence = np.zeros(n)
        x0 = np.concatenate([init_attack, init_defence, [0.3, -0.1]])
        prior_atk_vec = None
        prior_def_vec = None

    # Clamp warm-start x0 into the (tighter) optimizer bounds.
    a_lo, a_hi = _FIT_BOUNDS_ATTACK
    d_lo, d_hi = _FIT_BOUNDS_DEFENCE
    h_lo, h_hi = _FIT_BOUNDS_HOME_ADV
    r_lo, r_hi = _FIT_BOUNDS_RHO
    x0[:n] = np.clip(x0[:n], a_lo, a_hi)
    x0[n:2 * n] = np.clip(x0[n:2 * n], d_lo, d_hi)
    x0[2 * n] = np.clip(x0[2 * n], h_lo, h_hi)
    x0[2 * n + 1] = np.clip(x0[2 * n + 1], r_lo, r_hi)

    bounds = ([(a_lo, a_hi)] * n
              + [(d_lo, d_hi)] * n
              + [(h_lo, h_hi), (r_lo, r_hi)])

    cluster_atk_vec, cluster_def_vec = _compute_cluster_centers(
        teams, prior_params, cluster_map,
    )

    result = minimize(
        _vectorized_nll,
        x0,
        args=(n, ref_idx, *arrays, regularization, prior_atk_vec, prior_def_vec,
              cluster_strength, cluster_atk_vec, cluster_def_vec),
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
    elo_home: float | None = None,
    elo_away: float | None = None,
    elo_scale: float = DC_ELO_SCALE,
) -> np.ndarray:
    """Returns (max_goals+1 x max_goals+1) matrix of P(home_goals=i, away_goals=j).
    rho_override: replaces params.rho when set (used for stage-specific calibration).
    elo_home/elo_away: current Elo ratings; when provided, applies the same quality
        adjustment used during training so predictions are cross-confederation calibrated.
    """
    lh, la = _lambdas(home, away, params, neutral, elo_home, elo_away, elo_scale)
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
    elo_home: float | None = None,
    elo_away: float | None = None,
    elo_scale: float = DC_ELO_SCALE,
) -> dict[str, float]:
    """Returns {'p_home': float, 'p_draw': float, 'p_away': float}.
    rho_override: applies stage-specific low-score correction (see predict_match_staged).
    elo_home/elo_away: when provided, applies Elo quality adjustment at inference
        (must match the scale used during training for consistent results).
    """
    matrix = predict_scoreline(home, away, params, max_goals, neutral, rho_override,
                                elo_home=elo_home, elo_away=elo_away, elo_scale=elo_scale)
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
    elo_home: float | None = None,
    elo_away: float | None = None,
    elo_scale: float = DC_ELO_SCALE,
) -> dict[str, float]:
    """
    Stage-aware prediction. Rho factors are loaded from rho_stages.json
    (fit by scripts/fit_rho_stages.py with walk-forward DC snapshots; no leakage).

    If `stage` is provided ("group", "r16", "qf", "sf", "third_place", "final"),
    uses that specific factor. Otherwise falls back to binary is_knockout:
    knockout → mean of KO-stage factors weighted by sample size; else group.
    elo_home/elo_away: pass current Elo ratings for cross-confederation calibration.
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
    return predict_match(home, away, params, neutral=neutral, rho_override=rho,
                         elo_home=elo_home, elo_away=elo_away, elo_scale=elo_scale)


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


# Sanity bounds applied at save() time to prevent silently shipping a
# corrupted snapshot (see docs/audit_2026-06-12.md, section A).
# Phase 1.1: tightened to match optimizer _FIT_BOUNDS_*; the previous wider
# ranges allowed params_20260614 (rho=-0.5, Mexico defence=-2.97) to silently
# pass even though they were optimizer bound-hits.
_ATTACK_RANGE = _FIT_BOUNDS_ATTACK
_DEFENCE_RANGE = _FIT_BOUNDS_DEFENCE
_HOME_ADV_MAX = _FIT_BOUNDS_HOME_ADV[1]
_RHO_RANGE = _FIT_BOUNDS_RHO
_MAX_TEAM_DRIFT = 1.5  # max |Δ attack| or |Δ defence| per team vs prior


def validate_params(
    params: DixonColesParams,
    prior: "DixonColesParams | None" = None,
) -> list[str]:
    """Returns a list of human-readable issues. Empty list = OK.

    Range check: every team's attack/defence must fall inside the sanity
    intervals; home_adv and rho must be within the optimizer's natural bounds.
    Drift check (only when prior is given): no team's attack or defence may
    move by more than _MAX_TEAM_DRIFT between successive snapshots — guards
    against a single high-weight match dominating the retrain.
    """
    issues: list[str] = []

    a_lo, a_hi = _ATTACK_RANGE
    d_lo, d_hi = _DEFENCE_RANGE
    for team, v in params.attack.items():
        if not (a_lo <= v <= a_hi):
            issues.append(f"{team}.attack={v:+.3f} out of range [{a_lo}, {a_hi}]")
    for team, v in params.defence.items():
        if not (d_lo <= v <= d_hi):
            issues.append(f"{team}.defence={v:+.3f} out of range [{d_lo}, {d_hi}]")

    if abs(params.home_adv) > _HOME_ADV_MAX:
        issues.append(f"home_adv={params.home_adv:+.3f} exceeds |{_HOME_ADV_MAX}|")
    r_lo, r_hi = _RHO_RANGE
    if not (r_lo <= params.rho <= r_hi):
        issues.append(f"rho={params.rho:+.3f} out of range [{r_lo}, {r_hi}]")

    if prior is not None:
        for team, v in params.attack.items():
            pv = prior.attack.get(team)
            if pv is not None and abs(v - pv) > _MAX_TEAM_DRIFT:
                issues.append(
                    f"{team}.attack drift |{v:+.3f} - {pv:+.3f}|={abs(v - pv):.3f}"
                    f" exceeds {_MAX_TEAM_DRIFT}"
                )
        for team, v in params.defence.items():
            pv = prior.defence.get(team)
            if pv is not None and abs(v - pv) > _MAX_TEAM_DRIFT:
                issues.append(
                    f"{team}.defence drift |{v:+.3f} - {pv:+.3f}|={abs(v - pv):.3f}"
                    f" exceeds {_MAX_TEAM_DRIFT}"
                )

    return issues


def save(
    params: DixonColesParams,
    path: Path,
    prior: "DixonColesParams | None" = None,
    force: bool = False,
) -> None:
    issues = validate_params(params, prior=prior)
    if issues and not force:
        raise ValueError(
            "DC sanity check failed (use force=True to override):\n  "
            + "\n  ".join(issues)
        )
    if issues and force:
        print("⚠️  DC sanity check failed but force=True; saving anyway:")
        for line in issues:
            print(f"   {line}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(params, f)


def load(path: Path) -> DixonColesParams:
    with open(path, "rb") as f:
        return pickle.load(f)
