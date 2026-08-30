"""Health and exit-code contracts for the launchd odds-refresh entrypoint."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "refresh_odds.py"


def _load_refresh_script():
    spec = importlib.util.spec_from_file_location("refresh_odds_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_success_writes_timed_ok_health(monkeypatch) -> None:
    from src.monitoring import health_writer
    from src.signals import odds_refresher

    recorded = {}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(odds_refresher, "run_refresh", lambda **_: {"failed": 0, "refreshed": 2})
    monkeypatch.setattr(health_writer, "write_health", lambda *args, **kwargs: recorded.update(args=args, kwargs=kwargs))

    assert _load_refresh_script().main() == 0
    assert recorded["args"] == ("odds_refresh",)
    assert recorded["kwargs"]["status"] == "ok"
    assert recorded["kwargs"]["exit_code"] == 0
    assert recorded["kwargs"]["duration_s"] >= 0
    assert recorded["kwargs"]["run_id"].startswith("odds_refresh-launchd-")
    assert recorded["kwargs"]["scheduler"] == "launchd"


def test_total_failure_is_nonzero_and_health_error(monkeypatch) -> None:
    from src.monitoring import health_writer
    from src.signals import odds_refresher

    recorded = {}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(odds_refresher, "run_refresh", lambda **_: {"failed": 2, "refreshed": 0})
    monkeypatch.setattr(health_writer, "write_health", lambda *args, **kwargs: recorded.update(kwargs=kwargs))

    assert _load_refresh_script().main() == 1
    assert recorded["kwargs"]["status"] == "error"
    assert recorded["kwargs"]["exit_code"] == 1
    assert "all due odds refreshes failed" in recorded["kwargs"]["error"]


def test_partial_failure_is_visible_without_falsifying_process_exit(monkeypatch) -> None:
    from src.monitoring import health_writer
    from src.signals import odds_refresher

    recorded = {}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(odds_refresher, "run_refresh", lambda **_: {"failed": 1, "refreshed": 1})
    monkeypatch.setattr(health_writer, "write_health", lambda *args, **kwargs: recorded.update(kwargs=kwargs))

    assert _load_refresh_script().main() == 0
    assert recorded["kwargs"]["status"] == "degraded"
    assert recorded["kwargs"]["exit_code"] == 0


def test_health_write_failure_fails_closed(monkeypatch) -> None:
    from src.monitoring import health_writer
    from src.signals import odds_refresher

    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(odds_refresher, "run_refresh", lambda **_: {"failed": 0, "refreshed": 0})

    def fail_health_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(health_writer, "write_health", fail_health_write)

    assert _load_refresh_script().main() == 2
