"""Smoke-Tests für scripts/tennis_scan.py (Roadmap J2-D Dispatcher).

Vorgänger-Tests (Wimbledon-Hardcode-Helper wie _mock_wimbledon_matches,
_format_report, min_edge_for, _ATP_MIN_EDGE) wurden durch das Dispatcher-
Pattern obsolet. Die Tests sind nach tests/tennis/test_scanner_dispatcher.py
verschoben (deckt _parse_event_markets, detect_all_markets, format_scan_report,
category_mode etc.).

Dieser File hält nur einen Import-Smoke-Test, damit Scanner-Modul lädt.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_scanner_module_imports_cleanly():
    """Tennis-Scanner-Modul lädt ohne ImportError."""
    import scripts.tennis_scan as scan
    # Dispatcher-Public-API
    for attr in ("main", "category_min_edge", "category_mode",
                 "fetch_tournament_odds", "detect_all_markets",
                 "format_scan_report", "_parse_event_markets"):
        assert hasattr(scan, attr), f"tennis_scan fehlt: {attr}"


def test_scanner_mock_tournament_helper_returns_wimbledon():
    """Mock-Helper liefert Wimbledon ATP + ≥1 Mock-Match."""
    from scripts.tennis_scan import _mock_tournament_matches
    t, matches = _mock_tournament_matches()
    assert t.slug == "wimbledon_atp"
    assert len(matches) >= 1
    for m in matches:
        assert m["odds_a"] > 1.0
        assert m["odds_b"] > 1.0
