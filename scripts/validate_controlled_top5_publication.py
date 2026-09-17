#!/usr/bin/env python3
"""Validate a detached Top-5 controlled-publication attestation."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.football.top5_publisher import (
    ControlledPublicationAttestation,
    ControlledPublicationCapability,
    FileControlledPublicationCapabilityStore,
)


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[5] != "--consume":
        print(
            "usage: validate_controlled_top5_publication.py "
            "<artifact-json> <attestation-json> <artifact-path> "
            "<capability-token-json> --consume",
            file=sys.stderr,
        )
        return 2
    try:
        repository_root = Path(__file__).resolve().parents[1]
        token_path = Path(argv[4])
        if token_path.resolve().is_relative_to(repository_root):
            raise ValueError(
                "capability token must be operator-owned outside the repository"
            )
        artifact = json.loads(Path(argv[1]).read_text())
        attestation = ControlledPublicationAttestation.from_mapping(
            json.loads(Path(argv[2]).read_text())
        )
        capability = ControlledPublicationCapability.from_mapping(
            json.loads(token_path.read_text())
        )
        FileControlledPublicationCapabilityStore().consume(
            capability,
            attestation,
            artifact=artifact,
            artifact_path=argv[3],
            now=datetime.now(timezone.utc),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"controlled publication rejected: {exc}", file=sys.stderr)
        return 1
    print("controlled publication attestation valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
