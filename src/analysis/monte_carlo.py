"""Scoreline distribution utilities from a Dixon-Coles probability matrix."""

from __future__ import annotations

import numpy as np


def scoreline_distribution(matrix: np.ndarray) -> dict:
    """Compute Top-3 scores and goal distribution from a DC scoreline matrix.

    Args:
        matrix: (max_goals+1 x max_goals+1) array of P(home_goals=i, away_goals=j).

    Returns:
        {
            "top_scores": [{"h": int, "a": int, "p": float}, ...],  # top-3, p in %
            "goal_dist":  {"0": float, "1": float, "2": float, "3+": float},  # in %
        }
    """
    max_g = matrix.shape[0] - 1

    # Top-3 most likely scorelines
    entries = [(float(matrix[i, j]), i, j) for i in range(max_g + 1) for j in range(max_g + 1)]
    entries.sort(key=lambda x: -x[0])
    top_scores = [{"h": h, "a": a, "p": round(p * 100, 1)} for p, h, a in entries[:3]]

    # Cumulative goal distribution by total goals
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

    return {"top_scores": top_scores, "goal_dist": goal_dist}
