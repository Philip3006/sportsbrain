"""Scoreline distribution utilities from a Dixon-Coles probability matrix.

Provides both analytical (exact matrix) and Monte Carlo (sampling-based)
distributions, always returned together as a combined result.
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
    # Draw N indices from the flattened matrix using matrix probabilities
    indices = rng.choice(len(flat), size=n, p=flat / flat.sum())
    rows, cols = np.divmod(indices, matrix.shape[1])

    # Empirical top-3 scorelines
    from collections import Counter
    counts = Counter(zip(rows.tolist(), cols.tolist()))
    top3 = counts.most_common(3)
    top_scores = [{"h": int(h), "a": int(a), "p": round(cnt / n * 100, 1)} for (h, a), cnt in top3]

    # Empirical goal distribution
    totals = rows + cols
    goal_dist: dict[str, float] = {}
    for g in range(3):
        goal_dist[str(g)] = round(float((totals == g).sum()) / n * 100, 1)
    goal_dist["3+"] = round(float((totals >= 3).sum()) / n * 100, 1)

    return top_scores, goal_dist


def scoreline_distribution(matrix: np.ndarray, n_mc: int = 10_000,
                            seed: int | None = None) -> dict:
    """Combined analytical + Monte Carlo scoreline distribution.

    Args:
        matrix:  (max_goals+1 x max_goals+1) DC probability matrix.
        n_mc:    Number of MC draws (default 10 000).
        seed:    RNG seed for reproducibility (None = random).

    Returns:
        {
            "top_scores":    [{"h", "a", "p"}, ...],   # analytical, top-3, p in %
            "goal_dist":     {"0", "1", "2", "3+"},    # analytical, in %
            "mc_top_scores": [{"h", "a", "p"}, ...],   # MC empirical, top-3
            "mc_goal_dist":  {"0", "1", "2", "3+"},    # MC empirical, in %
            "n_mc":          int,
        }
    """
    rng = np.random.default_rng(seed)
    top_scores, goal_dist = _analytical(matrix)
    mc_top_scores, mc_goal_dist = _simulate(matrix, n_mc, rng)

    return {
        "top_scores":    top_scores,
        "goal_dist":     goal_dist,
        "mc_top_scores": mc_top_scores,
        "mc_goal_dist":  mc_goal_dist,
        "n_mc":          n_mc,
    }
