"""Phase 1.4 — Live-Predictions audit script.

Smoke test only: feed the audit a synthetic signals.json and verify it
flags the known suspicious patterns from docs/audit_2026-06-12.md.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.audit_live_predictions as audit_mod


_SAMPLE = {
    "updated": "2026-06-14T00:00:00Z",
    "model_tips": {
        "Canada vs Bosnia and Herzegovina": {
            "p_home": 0.983, "p_draw": 0.015, "p_away": 0.002,
            "xg_home": 5.48, "xg_away": 0.38,
        },
        "Brazil vs Cameroon": {
            "p_home": 0.55, "p_draw": 0.25, "p_away": 0.20,
            "xg_home": 1.8, "xg_away": 1.1,
        },
        "Germany vs Curaçao": {
            "p_home": 0.36, "p_draw": 0.41, "p_away": 0.23,
            "xg_home": 1.31, "xg_away": 1.05,
        },
        "Scotland vs Morocco": {
            "p_home": 0.25, "p_draw": 0.35, "p_away": 0.40,
            "p_under25": 0.938,
        },
    },
}


def test_audit_flags_canada_bosnia(tmp_path):
    p = tmp_path / "signals.json"
    p.write_text(json.dumps(_SAMPLE))
    flagged, total = audit_mod.audit(p)
    matches = {f["match"] for f in flagged}
    assert "Canada vs Bosnia and Herzegovina" in matches
    assert "Scotland vs Morocco" in matches  # under-25 above 0.90
    assert "Germany vs Curaçao" not in matches  # all probs healthy
    assert "Brazil vs Cameroon" not in matches  # all probs healthy
    assert total == 4


def test_audit_writes_report(tmp_path):
    p = tmp_path / "signals.json"
    p.write_text(json.dumps(_SAMPLE))
    flagged, total = audit_mod.audit(p)
    out_dir = tmp_path / "audits"
    out_path = audit_mod.write_report(flagged, total, out_dir=out_dir)
    assert out_path.exists()
    body = out_path.read_text()
    assert "Canada vs Bosnia" in body
    assert "p_home=0.983" in body


def test_audit_no_signals_file_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    flagged, total = audit_mod.audit(missing)
    assert flagged == []
    assert total == 0


def test_audit_handles_unexpected_shape(tmp_path):
    p = tmp_path / "signals.json"
    p.write_text(json.dumps({"model_tips": ["unexpected", "list"]}))
    flagged, total = audit_mod.audit(p)
    assert flagged == []
    assert total == 0
