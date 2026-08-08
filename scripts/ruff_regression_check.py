#!/usr/bin/env python3
"""
CEO-Roadmap O0-3b: Ruff regression check.

Vergleicht aktuelle Ruff-Violations gegen .ruff_baseline.json.
Failt (exit 1) NUR bei neuen Violations, die nicht in der Baseline stehen.
So können bestehende 1057 Altbestand-Violations sukzessive abgebaut
werden, ohne dass CI blockiert.

Match-Key: (filename_relativ, rule_code) — ohne Zeile/Spalte, damit
Refactors und Verschiebungen keine falschen Positive erzeugen. Line
column können sich verschieben, aber ein neuer Rule-Verstoß in einer
Datei ohne Vor-Verstoß dieser Regel wird erkannt.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".ruff_baseline.json"


def _load_violations(source: list[dict]) -> Counter:
    """Zählt Violations pro (relativer Pfad, Regel-Code)."""
    counts: Counter = Counter()
    for v in source:
        raw = v.get("filename", "")
        try:
            rel = str(Path(raw).resolve().relative_to(REPO_ROOT))
        except ValueError:
            rel = raw
        code = v.get("code") or "UNKNOWN"
        counts[(rel, code)] += 1
    return counts


def main() -> int:
    if not BASELINE_PATH.exists():
        print(f"ERROR: baseline missing at {BASELINE_PATH}", file=sys.stderr)
        return 2
    baseline_raw = json.loads(BASELINE_PATH.read_text())
    baseline = _load_violations(baseline_raw)

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src", "scripts",
         "--output-format", "json"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )
    if result.returncode not in (0, 1):
        print(f"ERROR: ruff exited {result.returncode}\n{result.stderr}",
              file=sys.stderr)
        return 2

    current_raw = json.loads(result.stdout) if result.stdout.strip() else []
    current = _load_violations(current_raw)

    new_violations = []
    for key, count in current.items():
        baseline_count = baseline.get(key, 0)
        if count > baseline_count:
            new_violations.append((key, count - baseline_count))

    removed = sum(
        baseline[k] - current.get(k, 0)
        for k in baseline if current.get(k, 0) < baseline[k]
    )

    total_current = sum(current.values())
    total_baseline = sum(baseline.values())
    print(f"[ruff-regression] baseline={total_baseline} current={total_current} "
          f"removed={removed} new={sum(n for _, n in new_violations)}")

    if new_violations:
        print("[ruff-regression] NEW violations vs baseline:", file=sys.stderr)
        for (f, code), n in sorted(new_violations):
            print(f"  {f}: +{n}x {code}", file=sys.stderr)
        print("\nEntweder Violation fixen, oder baseline neu snapshotten "
              "(nur bei bewusstem Netto-Fix):", file=sys.stderr)
        print("  python -m ruff check src scripts --output-format json "
              "> .ruff_baseline.json", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
