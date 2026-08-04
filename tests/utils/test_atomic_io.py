"""Tests for atomic write helpers."""
from __future__ import annotations

import json
import os
import threading

import pytest

from src.utils.atomic_io import atomic_write_json, atomic_write_text


def test_write_json_creates_file(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"a": 1, "b": [1, 2]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2]}


def test_write_json_overwrites_existing(tmp_path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({"old": True}))
    atomic_write_json(p, {"new": True})
    assert json.loads(p.read_text()) == {"new": True}


def test_write_json_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deep" / "out.json"
    atomic_write_json(p, {"x": 1})
    assert p.exists()


def test_write_json_no_partial_on_crash(tmp_path, monkeypatch):
    """Wenn json.dumps mitten drin crashed, darf das Zielfile NICHT modifiziert sein."""
    p = tmp_path / "out.json"
    p.write_text('{"stable": true}')

    class BadObj:
        def __repr__(self):
            raise RuntimeError("boom")

    with pytest.raises(TypeError):
        atomic_write_json(p, {"broken": BadObj()})

    # Original data still intact
    assert json.loads(p.read_text()) == {"stable": True}
    # No leftover tmp files
    tmp_files = [f for f in os.listdir(tmp_path) if f.startswith(".out.json.")]
    assert tmp_files == []


def test_atomic_write_text(tmp_path):
    p = tmp_path / "note.txt"
    atomic_write_text(p, "hello\nworld\n")
    assert p.read_text() == "hello\nworld\n"


def test_concurrent_writes_no_corruption(tmp_path):
    """Zwei parallele Writer schreiben abwechselnd; File ist immer valid JSON."""
    p = tmp_path / "shared.json"
    p.write_text(json.dumps({"init": True}))

    stop = threading.Event()
    errors: list[Exception] = []

    def writer(tag: str, n: int):
        try:
            for i in range(n):
                atomic_write_json(p, {"writer": tag, "i": i})
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("A", 50))
    t2 = threading.Thread(target=writer, args=("B", 50))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    stop.set()

    assert not errors
    # After all writes, file is still valid JSON (not truncated/mid-write)
    data = json.loads(p.read_text())
    assert data["writer"] in {"A", "B"}
