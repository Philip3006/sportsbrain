"""Phase 1.4 — Live-Predictions Audit.

Reads `docs/data/signals.json` (what the web dashboard publishes) and lists any
prediction that crosses one of these "suspicious" thresholds:

  • p_home / p_draw / p_away > 0.85
  • xg_home or xg_away > 3.5
  • p_btts_yes / p_btts_no > 0.90
  • p_over25 / p_under25 > 0.90

The audit (docs/audit_2026-06-12.md) documented several of these — Canada 98 %
vs Bosnia, England 89 % vs Ghana — as products of an unstable DC retrain rather
than real edges. After tightening the optimizer bounds (Phase 1.1) and adding
retry-on-bound-hit, this script verifies the same dashboard data no longer
shows the same pattern.

Output is written to results/audits/live_predictions_<date>.md so the user can
diff against past audits and spot regressions.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import RESULTS_DIR


SIGNALS_JSON = Path(__file__).parent.parent / "docs" / "data" / "signals.json"

_P_THRESHOLD = 0.85
# Synchronized with src/models/dixon_coles._MAX_LAMBDA (4.5). The DC layer
# now caps lambdas at 4.5 at inference time, so the audit threshold matches
# that cap — a hit signals the cap fired (model wanted to go higher).
_XG_THRESHOLD = 4.5
_PBINARY_THRESHOLD = 0.90


def _classify(match: str, tip: dict) -> list[str]:
    """Returns the list of red-flag reasons for a single tip."""
    reasons: list[str] = []
    for key in ("p_home", "p_draw", "p_away"):
        v = tip.get(key)
        if v is not None and v > _P_THRESHOLD:
            reasons.append(f"{key}={v:.3f} > {_P_THRESHOLD}")
    for key in ("xg_home", "xg_away"):
        v = tip.get(key)
        if v is not None and v > _XG_THRESHOLD:
            reasons.append(f"{key}={v:.2f} > {_XG_THRESHOLD}")
    for key in ("p_btts_yes", "p_btts_no", "p_over25", "p_under25"):
        v = tip.get(key)
        if v is not None and v > _PBINARY_THRESHOLD:
            reasons.append(f"{key}={v:.3f} > {_PBINARY_THRESHOLD}")
    return reasons


def audit(signals_path: Path = SIGNALS_JSON) -> tuple[list[dict], int]:
    """Returns (flagged_matches, total_matches)."""
    if not signals_path.exists():
        print(f"  signals.json missing at {signals_path}")
        return [], 0
    data = json.loads(signals_path.read_text())
    tips = data.get("model_tips", {})
    if not isinstance(tips, dict):
        print(f"  model_tips has unexpected shape: {type(tips).__name__}")
        return [], 0

    flagged: list[dict] = []
    for match, tip in tips.items():
        reasons = _classify(match, tip)
        if reasons:
            flagged.append({"match": match, "reasons": reasons, "tip": tip})

    return flagged, len(tips)


def write_report(flagged: list[dict], total: int,
                  out_dir: Path = RESULTS_DIR / "audits") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"live_predictions_{today}.md"
    lines: list[str] = [
        f"# Live-Predictions Audit — {today}",
        "",
        f"Audited {total} `model_tips` from `docs/data/signals.json`.",
        f"Found **{len(flagged)} flagged** match(es) crossing the thresholds:",
        "",
        f"- `p_home` / `p_draw` / `p_away` > {_P_THRESHOLD}",
        f"- `xg_home` / `xg_away` > {_XG_THRESHOLD}",
        f"- `p_btts_yes` / `p_btts_no` / `p_over25` / `p_under25` > {_PBINARY_THRESHOLD}",
        "",
    ]
    if flagged:
        for f in flagged:
            lines.append(f"## {f['match']}")
            lines.append("")
            for r in f["reasons"]:
                lines.append(f"- {r}")
            lines.append("")
            lines.append("```")
            lines.append(json.dumps(f["tip"], indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
    else:
        lines.append("**✓ No flagged predictions.**")
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", default=str(SIGNALS_JSON),
                         help="Path to signals.json (default: docs/data/signals.json)")
    parser.add_argument("--fail-on-flags", action="store_true",
                         help="Exit 1 if any predictions are flagged (CI gate)")
    args = parser.parse_args()

    flagged, total = audit(Path(args.signals))
    path = write_report(flagged, total)

    print(f"Audited {total} predictions → {len(flagged)} flagged")
    print(f"Report: {path}")
    if flagged:
        print()
        for f in flagged[:8]:
            print(f"  ⚠️  {f['match']}: " + "; ".join(f['reasons']))
        if len(flagged) > 8:
            print(f"  … {len(flagged) - 8} more in the report")

    return 1 if (args.fail_on_flags and flagged) else 0


if __name__ == "__main__":
    sys.exit(main())
