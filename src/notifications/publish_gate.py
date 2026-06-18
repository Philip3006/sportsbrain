"""Validate, audit, upload, and atomically activate dashboard snapshots."""
from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from scripts.audit_live_predictions import audit, write_report


class PublishBlocked(RuntimeError):
    pass


def _payload_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()


def payload_digest(payload: dict) -> str:
    return sha256(_payload_bytes(payload)).hexdigest()


def _validate_signal(row: dict, section: str, index: int) -> list[str]:
    prefix = f"{section}[{index}]"
    issues: list[str] = []
    if not isinstance(row, dict):
        return [f"{prefix} must be an object"]
    for key in ("match", "market", "odds", "model_prob", "ev_pct", "stake_eur"):
        if key not in row:
            issues.append(f"{prefix}.{key} is required")
    try:
        ev = float(row.get("ev_pct"))
        minimum = float(row.get("min_ev_pct", 0))
        stake = float(row.get("stake_eur"))
    except (TypeError, ValueError):
        issues.append(f"{prefix} has non-numeric EV/stake fields")
        return issues
    if ev <= 0 or ev + 1e-9 < minimum:
        issues.append(f"{prefix} fails positive/minimum EV gate")
    if stake < 0:
        issues.append(f"{prefix} has negative stake")
    gates = row.get("safety_gates", {})
    for gate in ("positive_ev", "min_ev", "kelly"):
        if gates.get(gate) is not True:
            issues.append(f"{prefix} fails {gate} gate")
    return issues


def validate_payload(payload: dict, model_dir: Path) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    issues: list[str] = []
    required_types = {
        "updated": str,
        "schedule": list,
        "all_odds": dict,
        "model_tips": dict,
        "football": list,
        "tennis": list,
        "bankroll_state": dict,
        "open_bets": list,
    }
    for key, expected in required_types.items():
        if not isinstance(payload.get(key), expected):
            issues.append(f"{key} must be {expected.__name__}")
    for section in ("football", "tennis"):
        rows = payload.get(section, [])
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                issues.extend(_validate_signal(row, section, index))
    open_bets = payload.get("open_bets", [])
    if isinstance(open_bets, list) and len(open_bets) > 5:
        issues.append(f"portfolio has {len(open_bets)} active bets; maximum is 5")
    tips = payload.get("model_tips", {})
    if isinstance(tips, dict) and any(not isinstance(tip, dict) for tip in tips.values()):
        issues.append("model_tips values must be objects")
    return issues


def _write_failure(path: Path, issues: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "blocked",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alert": {"level": "error", "code": "PUBLISH_BLOCKED", "message": "; ".join(issues)},
    }, indent=2) + "\n")


def approval_issues(
    candidate: Path,
    flagged: list[dict],
    model_dir: Path,
    approval_path: Path,
    expected_snapshot: str = "",
) -> list[str]:
    try:
        approval = json.loads(approval_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"audit approval unreadable: {exc}"]
    if not isinstance(approval, dict):
        return ["audit approval must be an object"]
    issues: list[str] = []
    digest = sha256(candidate.read_bytes()).hexdigest()
    if approval.get("candidate_sha256") != digest:
        issues.append("audit approval candidate_sha256 mismatch")
    if not expected_snapshot:
        snaps = sorted(Path(model_dir).glob("params_*.pkl"))
        expected_snapshot = snaps[-1].name if snaps else ""
    if approval.get("active_snapshot") != expected_snapshot:
        issues.append("audit approval active_snapshot mismatch")
    if not str(approval.get("approved_by", "")).strip():
        issues.append("audit approval approved_by is required")
    approved_at = str(approval.get("approved_at", ""))
    try:
        datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError:
        issues.append("audit approval approved_at must be ISO-8601")
    reviews = approval.get("reviews")
    if not isinstance(reviews, dict):
        return issues + ["audit approval reviews must be an object"]
    for item in flagged:
        match = item["match"]
        review = reviews.get(match)
        if not isinstance(review, dict):
            issues.append(f"audit approval missing review for {match}")
            continue
        if review.get("reasons") != item["reasons"]:
            issues.append(f"audit approval reasons changed for {match}")
        if len(str(review.get("cause_documented", "")).strip()) < 20:
            issues.append(f"audit approval cause is incomplete for {match}")
        if len(str(review.get("market_comparison", "")).strip()) < 20:
            issues.append(f"audit approval market comparison is incomplete for {match}")
        components = set(review.get("model_components_checked", []))
        if not {"dixon_coles", "market"}.issubset(components):
            issues.append(f"audit approval components incomplete for {match}")
        if review.get("parameter_bounds_checked") is not True:
            issues.append(f"audit approval bounds check missing for {match}")
        if review.get("parameter_drift_checked") is not True:
            issues.append(f"audit approval drift check missing for {match}")
        if review.get("decision") != "approve":
            issues.append(f"audit approval decision is not approve for {match}")
    return issues


def publish_payload(
    payload: dict,
    destination: Path,
    model_dir: Path,
    failure_report: Path,
    upload: Callable[[Path], bool] | None = None,
    approval_path: Path | None = None,
) -> None:
    """Publish only after every gate and an optional configured upload passes."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=".signals-candidate-", suffix=".json", dir=destination.parent)
    candidate = Path(raw_tmp)
    issues: list[str] = []
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_payload_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())

        parsed = json.loads(candidate.read_text())
        issues.extend(validate_payload(parsed, model_dir))
        if not issues:
            flagged, total = audit(candidate)
            write_report(flagged, total, out_dir=failure_report.parent)
            if flagged:
                # Only block when a flagged match also has a MEDIUM/HIGH signal in
                # the portfolio — LOW-only flags are informational and don't block.
                football_signals = parsed.get("football", [])
                actionable_matches = {
                    s.get("match", "")
                    for s in football_signals
                    if s.get("confidence", "LOW") in {"MEDIUM", "HIGH"}
                }
                blocking_flags = [
                    item for item in flagged
                    if item["match"] in actionable_matches
                ]
                configured_approval = approval_path or (
                    Path(os.environ["SPORTSBRAIN_AUDIT_APPROVAL"])
                    if os.getenv("SPORTSBRAIN_AUDIT_APPROVAL") else None
                )
                if configured_approval is None:
                    issues.extend(
                        f"audit flag {item['match']}: {', '.join(item['reasons'])}"
                        for item in blocking_flags
                    )
                else:
                    issues.extend(approval_issues(candidate, blocking_flags, model_dir, configured_approval))
        if issues:
            raise PublishBlocked("; ".join(issues))

        writer_mode = os.getenv("SPORTSBRAIN_WRITER_MODE", "production").strip().lower()
        if writer_mode not in {"production", "shadow"}:
            issues.append(f"invalid SPORTSBRAIN_WRITER_MODE: {writer_mode}")
            raise PublishBlocked(issues[-1])
        if writer_mode == "shadow":
            shadow = failure_report.parent / "shadow_signals.json"
            shadow.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, shadow)
            return

        cloud_configured = bool(os.getenv("SIGNALS_CLOUD_URL") or os.getenv("SIGNALS_API_TOKEN"))
        if cloud_configured and (upload is None or not upload(candidate)):
            issues.append("Worker upload failed")
            raise PublishBlocked(issues[-1])
        os.replace(candidate, destination)
    except (json.JSONDecodeError, OSError, TypeError, PublishBlocked) as exc:
        if not issues:
            issues.append(str(exc))
        _write_failure(failure_report, issues)
        raise PublishBlocked("; ".join(issues)) from exc
    finally:
        candidate.unlink(missing_ok=True)
