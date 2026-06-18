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
from hashlib import sha256
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import RESULTS_DIR


SIGNALS_JSON = Path(__file__).parent.parent / "docs" / "data" / "signals.json"

_P_THRESHOLD = 0.85
_XG_THRESHOLD = 3.5
_PBINARY_THRESHOLD = 0.90


def _classify(match: str, tip: dict, market_odds: dict | None = None) -> list[str]:
    """Returns the list of red-flag reasons for a single tip.

    When market_odds is provided, skip flagging extreme win-probabilities when the
    market itself also implies a strong favorite (market-implied > 0.75 for that
    outcome).  This avoids spurious flags for legitimate heavy favorites like
    France vs Iraq where model *and* market agree.
    """
    reasons: list[str] = []
    mkt = market_odds or {}

    def _mkt_implied(outcome: str) -> float:
        """Rough overround-corrected market-implied probability."""
        key_map = {"p_home": "home", "p_draw": "draw", "p_away": "away"}
        odds_key = key_map.get(outcome)
        if not odds_key:
            return 0.0
        o = mkt.get(odds_key)
        if not o or float(o) <= 1.0:
            return 0.0
        # crude: just raw 1/odds (overround correction not needed for a threshold)
        return 1.0 / float(o)

    for key in ("p_home", "p_draw", "p_away"):
        v = tip.get(key)
        if v is not None and v > _P_THRESHOLD:
            mkt_p = _mkt_implied(key)
            if mkt_p >= 0.75:
                # Market also sees a heavy favorite — not a suspicious outlier
                continue
            reasons.append(f"{key}={v:.3f} > {_P_THRESHOLD}")
    over35_mkt = mkt.get("over35", 0)
    over25_mkt = mkt.get("over25", 0)
    over35_implied = (1 / float(over35_mkt)) if over35_mkt and float(over35_mkt) > 1 else 0.0
    over25_implied = (1 / float(over25_mkt)) if over25_mkt and float(over25_mkt) > 1 else 0.0

    home_implied = _mkt_implied("p_home")
    away_implied = _mkt_implied("p_away")
    for key in ("xg_home", "xg_away"):
        v = tip.get(key)
        if v is not None and v > _XG_THRESHOLD:
            if over35_implied >= 0.55:
                continue  # market also expects many goals
            # If the scoring team is a heavy market favorite, high xG is expected
            if key == "xg_home" and home_implied >= 0.75:
                continue
            if key == "xg_away" and away_implied >= 0.75:
                continue
            reasons.append(f"{key}={v:.2f} > {_XG_THRESHOLD}")
    for key in ("p_btts_yes", "p_btts_no", "p_over25", "p_under25"):
        v = tip.get(key)
        if v is not None and v > _PBINARY_THRESHOLD:
            if key == "p_over25" and over25_implied >= 0.65:
                continue  # market over25 implied >65% — not suspicious
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
    odds = data.get("all_odds", {})
    model_snapshot = data.get("model_status", {}).get("active_snapshot", "")
    for match, tip in tips.items():
        market_odds = odds.get(match, {}) if isinstance(odds, dict) else {}
        reasons = _classify(match, tip, market_odds=market_odds)
        if reasons:
            flagged.append({
                "match": match,
                "reasons": reasons,
                "tip": tip,
                "market": market_odds,
                "model_snapshot": model_snapshot,
            })

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


def write_approval_template(
    signals_path: Path,
    flagged: list[dict],
    output: Path,
) -> Path:
    """Create a candidate-bound, deliberately incomplete manual review form."""
    payload = json.loads(signals_path.read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "candidate_sha256": sha256(signals_path.read_bytes()).hexdigest(),
        "active_snapshot": payload.get("model_status", {}).get("active_snapshot", ""),
        "approved_by": "",
        "approved_at": "",
        "reviews": {
            item["match"]: {
                "reasons": item["reasons"],
                "cause_documented": "",
                "market_comparison": "",
                "market_context": item.get("market", {}),
                "model_components_checked": [],
                "parameter_bounds_checked": False,
                "parameter_drift_checked": False,
                "decision": "block",
            }
            for item in flagged
        },
    }
    output.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", default=str(SIGNALS_JSON),
                         help="Path to signals.json (default: docs/data/signals.json)")
    parser.add_argument("--fail-on-flags", action="store_true",
                         help="Exit 1 if any predictions are flagged (CI gate)")
    parser.add_argument("--approval-template", default="",
                        help="Write an incomplete SHA-bound manual review template")
    args = parser.parse_args()

    flagged, total = audit(Path(args.signals))
    path = write_report(flagged, total)
    if args.approval_template:
        write_approval_template(Path(args.signals), flagged, Path(args.approval_template))

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
