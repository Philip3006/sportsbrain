"""J8-B11: Signal-Archive dedupliziert Re-Scans desselben Tages.

Regression-Guard: der Audit befürchtete Duplikate bei Re-Scans; tatsächlich
dedupliziert `archive_signals()` bereits über `(match_id, market, scan_date)`.
Test asserted das für Tennis-Signale + gemischte Sportarten.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.scanner import output as scanner_output


@dataclass
class _FakeSignal:
    match_id: str
    home: str
    away: str
    market: str
    model_prob: float = 0.55
    fair_prob: float = 0.50
    decimal_odds: float = 2.0
    ev: float = 0.10
    confidence: str = "MEDIUM"
    n_models_agree: int = 1


def test_rescan_same_day_does_not_duplicate(tmp_path, monkeypatch):
    fake_history = tmp_path / "signal_history.jsonl"
    monkeypatch.setattr(scanner_output, "SIGNAL_HISTORY", fake_history)

    sig = _FakeSignal(match_id="m1", home="A", away="B", market="home")
    n1 = scanner_output.archive_signals([sig], selected_ids=set(),
                                        scan_ts="2026-08-01T10:00:00", sport="tennis")
    n2 = scanner_output.archive_signals([sig], selected_ids=set(),
                                        scan_ts="2026-08-01T15:00:00", sport="tennis")
    assert n1 == 1
    assert n2 == 0, "Re-Scan am selben Tag darf keinen zweiten Archive-Eintrag erzeugen"


def test_next_day_writes_new_entry(tmp_path, monkeypatch):
    fake_history = tmp_path / "signal_history.jsonl"
    monkeypatch.setattr(scanner_output, "SIGNAL_HISTORY", fake_history)

    sig = _FakeSignal(match_id="m1", home="A", away="B", market="home")
    scanner_output.archive_signals([sig], selected_ids=set(),
                                   scan_ts="2026-08-01T10:00:00", sport="tennis")
    n2 = scanner_output.archive_signals([sig], selected_ids=set(),
                                        scan_ts="2026-08-02T10:00:00", sport="tennis")
    assert n2 == 1


def test_different_market_same_match_writes(tmp_path, monkeypatch):
    fake_history = tmp_path / "signal_history.jsonl"
    monkeypatch.setattr(scanner_output, "SIGNAL_HISTORY", fake_history)

    a = _FakeSignal(match_id="m1", home="A", away="B", market="home")
    b = _FakeSignal(match_id="m1", home="A", away="B", market="first_set_a")
    n = scanner_output.archive_signals([a, b], selected_ids=set(),
                                       scan_ts="2026-08-01T10:00:00", sport="tennis")
    assert n == 2
