"""Scoreline distribution utilities from a Dixon-Coles probability matrix.

Provides both analytical (exact matrix) and Monte Carlo (sampling-based)
distributions, always returned together as a combined result.

Optionally blends the DC matrix with an empirical historical prior:
    blended = alpha * dc_matrix + (1 - alpha) * prior_matrix
"""

from __future__ import annotations

import numpy as np


def _analytical(matrix: np.ndarray) -> tuple[list[dict], dict]:
    """Exact top-3 scores and goal distribution from matrix."""
    max_g = matrix.shape[0] - 1
    entries = [(float(matrix[i, j]), i, j) for i in range(max_g + 1) for j in range(max_g + 1)]
    entries.sort(key=lambda x: -x[0])
    top_scores = [{"h": h, "a": a, "p": round(p * 100, 1)} for p, h, a in entries[:3]]

    goal_dist: dict[str, float] = {}
    for total in range(3):
        p = float(sum(
            matrix[i, j]
            for i in range(max_g + 1)
            for j in range(max_g + 1)
            if i + j == total
        ))
        goal_dist[str(total)] = round(p * 100, 1)
    p3plus = float(sum(
        matrix[i, j]
        for i in range(max_g + 1)
        for j in range(max_g + 1)
        if i + j >= 3
    ))
    goal_dist["3+"] = round(p3plus * 100, 1)
    return top_scores, goal_dist


def _simulate(matrix: np.ndarray, n: int, rng: np.random.Generator) -> tuple[list[dict], dict]:
    """Monte Carlo: draw N scorelines from matrix, return empirical top-3 + goal dist."""
    flat = matrix.flatten()
    indices = rng.choice(len(flat), size=n, p=flat / flat.sum())
    rows, cols = np.divmod(indices, matrix.shape[1])

    from collections import Counter
    counts = Counter(zip(rows.tolist(), cols.tolist()))
    top3 = counts.most_common(3)
    top_scores = [{"h": int(h), "a": int(a), "p": round(cnt / n * 100, 1)} for (h, a), cnt in top3]

    totals = rows + cols
    goal_dist: dict[str, float] = {}
    for g in range(3):
        goal_dist[str(g)] = round(float((totals == g).sum()) / n * 100, 1)
    goal_dist["3+"] = round(float((totals >= 3).sum()) / n * 100, 1)

    return top_scores, goal_dist


def _blend(dc_matrix: np.ndarray, prior_matrix: np.ndarray, alpha: float) -> np.ndarray:
    """Linear blend: alpha × DC + (1-alpha) × prior, renormalised."""
    if prior_matrix.shape != dc_matrix.shape:
        # Resize prior to match DC matrix (pad with zeros or truncate)
        p = np.zeros_like(dc_matrix)
        s = min(prior_matrix.shape[0], dc_matrix.shape[0])
        p[:s, :s] = prior_matrix[:s, :s]
        prior_matrix = p
    blended = alpha * dc_matrix + (1.0 - alpha) * prior_matrix
    total = blended.sum()
    if total > 0:
        blended /= total
    return blended


def scoreline_distribution(
    matrix: np.ndarray,
    prior_matrix: np.ndarray | None = None,
    alpha: float = 1.0,
    n_mc: int = 10_000,
    seed: int | None = None,
) -> dict:
    """Combined analytical + Monte Carlo scoreline distribution.

    If prior_matrix is provided, blends DC matrix with empirical prior before
    computing both analytical and MC distributions.

    Args:
        matrix:       DC scoreline matrix (max_goals+1 x max_goals+1).
        prior_matrix: Empirical prior matrix, same shape (from empirical_prior.py).
        alpha:        DC weight in blend (0=pure prior, 1=pure DC). Default 1.0 (no blend).
        n_mc:         Number of MC draws (default 10 000).
        seed:         RNG seed for reproducibility.

    Returns:
        {
            "top_scores":    [{"h", "a", "p"}, ...],   # blended analytical top-3
            "goal_dist":     {"0", "1", "2", "3+"},    # blended analytical, in %
            "mc_top_scores": [{"h", "a", "p"}, ...],   # blended MC empirical top-3
            "mc_goal_dist":  {"0", "1", "2", "3+"},    # blended MC empirical, in %
            "n_mc":          int,
            "alpha":         float,                     # DC blend weight used
            "prior_used":    bool,
        }
    """
    rng = np.random.default_rng(seed)

    if prior_matrix is not None and alpha < 1.0:
        effective_matrix = _blend(matrix, prior_matrix, alpha)
    else:
        effective_matrix = matrix

    top_scores, goal_dist = _analytical(effective_matrix)
    mc_top_scores, mc_goal_dist = _simulate(effective_matrix, n_mc, rng)

    return {
        "top_scores":    top_scores,
        "goal_dist":     goal_dist,
        "mc_top_scores": mc_top_scores,
        "mc_goal_dist":  mc_goal_dist,
        "n_mc":          n_mc,
        "alpha":         alpha,
        "prior_used":    prior_matrix is not None and alpha < 1.0,
    }
