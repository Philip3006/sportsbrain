#!/usr/bin/env python3
"""Ablation: welcher Hebel schadet welcher Kategorie?

Läuft 6 Konfigurationen und vergleicht ROI pro Kategorie:
  0. BASE (Elo pur, keine Hebel)
  1. ALL (alle 4 Hebel-Gruppen an)
  2. ohne Bayesian (Hebel 3 aus)
  3. ohne Altitude (Hebel 1 aus)
  4. ohne Style+Momentum (Hebel 2+5 aus)
  5. ohne Sharp-Filter (Hebel 4 aus)

Fokus: wta500 Regression identifizieren.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from scripts.tennis_hebel_backtest import run_ab, _summarize


CONFIGS = [
    ("BASE",           dict(enable_bayesian=False, enable_altitude=False,
                            enable_style_momentum=False, enable_sharp=False)),
    ("ALL_HEBELS",     dict()),
    ("ohne_Bayesian",  dict(enable_bayesian=False)),
    ("ohne_Altitude",  dict(enable_altitude=False)),
    ("ohne_Style_Mom", dict(enable_style_momentum=False)),
    ("ohne_Sharp",     dict(enable_sharp=False)),
]


def run_all():
    results = {}
    for label, kwargs in CONFIGS:
        print(f"\n===== {label} =====")
        _base, hebel_df = run_ab(
            tours=["atp", "wta"],
            years=range(2019, 2026),
            min_year=2022,
            **kwargs,
        )
        if label == "BASE":
            # Für BASE ist _base = HEBEL beide identisch (alle Hebel off).
            # Wir nehmen das HEBEL-df, das ist bei allen Toggles off = pure Elo mit unverändertem stake.
            df = hebel_df
        else:
            df = hebel_df
        results[label] = df

    # Overall
    print("\n\n=========== OVERALL ===========")
    print(f"{'Config':<20} {'N':>6} {'ROI':>8} {'Hit%':>7}")
    for label, _ in CONFIGS:
        s = _summarize(results[label], label)
        print(f"{label:<20} {s['n']:>6} {s['roi']:>+7.2f}% {s['hit']:>6.1f}%")

    # Per-Kategorie
    all_keys = set()
    for df in results.values():
        if not df.empty:
            all_keys |= set(zip(df["category"], df["tour"]))

    print(f"\n=========== PER KATEGORIE (ROI %) ===========")
    header = ["Kategorie"] + [c[0][:14] for c in CONFIGS]
    print(f"{'Kategorie':<20} " + "  ".join(f"{c[0]:>14}" for c in CONFIGS))
    for cat, tour in sorted(all_keys):
        row = [f"{cat}/{tour}"]
        for label, _ in CONFIGS:
            df = results[label]
            if df.empty:
                row.append("  -   ")
                continue
            sub = df[(df["category"] == cat) & (df["tour"] == tour)]
            if sub.empty:
                row.append("  -   ")
                continue
            s = _summarize(sub, "")
            row.append(f"{s['roi']:+6.1f} (n={s['n']:>4})")
        print(f"{row[0]:<20} " + "  ".join(f"{v:>14}" for v in row[1:]))


if __name__ == "__main__":
    run_all()
