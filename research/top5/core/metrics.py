"""Top-5 — Shared evaluation primitives."""
from __future__ import annotations

import numpy as np


def brier(y: np.ndarray, p: np.ndarray) -> float:
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10, min_bin: int = 20) -> float:
    eces = []
    for k in range(3):
        labels = (y == k).astype(int)
        bins = np.linspace(0, 1, n_bins + 1)
        idx = np.clip(np.digitize(p[:, k], bins) - 1, 0, n_bins - 1)
        used, gap = 0, 0.0
        for b in range(n_bins):
            mask = idx == b
            cnt = int(mask.sum())
            if cnt < min_bin:
                continue
            gap += abs(float(p[mask, k].mean()) - float(labels[mask].mean())) * cnt
            used += cnt
        if used > 0:
            eces.append(gap / used)
    return float(np.mean(eces)) if eces else float("nan")


def boot_ci(y: np.ndarray, p: np.ndarray, fn=brier, n_boot: int = 1000,
            seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(y)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        vals.append(fn(y[idx], p[idx]))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def paired_bootstrap(y: np.ndarray, p_a: np.ndarray, p_b: np.ndarray,
                     n_boot: int = 1000, seed: int = 42):
    rng = np.random.default_rng(seed)
    n = len(y)
    deltas = []
    a_wins = 0
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        ba = brier(y[idx], p_a[idx])
        bb = brier(y[idx], p_b[idx])
        deltas.append(ba - bb)
        if ba < bb:
            a_wins += 1
    point = brier(y, p_a) - brier(y, p_b)
    lo = float(np.percentile(deltas, 2.5))
    hi = float(np.percentile(deltas, 97.5))
    return point, lo, hi, a_wins / n_boot


def label_from_scores(hs: int, as_: int) -> int:
    return 2 if hs > as_ else (1 if hs == as_ else 0)
