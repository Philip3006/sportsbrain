"""Atomic JSON/text writes.

Motivation: verhindert partial writes und Race-Conditions zwischen parallelen
Prozessen (Cron + PWA + CI). Ohne diesen Helper hatten wir 2026-06-26 einen
signals.json-Wipe durch Konflikt-Marker und mehrfach corrupt bankroll_snapshots
nach Crashes während write_text().

Pattern: tempfile im selben Verzeichnis (damit os.replace atomic bleibt) +
os.replace() (POSIX atomic rename).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: int | None = None,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> None:
    payload = json.dumps(
        data, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys
    )
    atomic_write_text(path, payload)
