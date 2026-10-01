"""Capture one bounded, iSports-only Nations League bet-time quote.

The default path is a zero-network preflight.  ``--execute-network`` is an
explicit operator opt-in and requires a current, scoped authorization file.
This command never changes the immutable Nations League prediction/lifecycle
artifact and never writes betting or ledger state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.betting.nations_league_actionability import (
    NationsLeagueActionabilityError,
    build_nations_league_actionable_projection,
    validate_nations_league_actionable_projection,
)
from src.football.top5_b4_provider_neutral_evidence import canonical_evidence_digest
from src.notifications.nations_league_live_public import (
    NationsLeagueLivePublicError,
    validate_live_public_nations_league,
)
from src.scanner.nations_league_live_market import _fetch_isports_market_snapshots
from src.utils.atomic_io import atomic_write_json

AUTHORIZATION_SCHEMA = "nations-league-bet-time-quote-authorization-v1"
PROVIDER = "isports_api"
PHASE = "refinement"
MAX_REQUESTS = 2
MAX_RETRIES = 0
MAX_AGE = timedelta(minutes=30)


class BetQuoteOperatorError(ValueError):
    """The operator input is stale, unsafe, or cannot be executed."""


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BetQuoteOperatorError(f"cannot read JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise BetQuoteOperatorError(f"JSON object required: {path}")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BetQuoteOperatorError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetQuoteOperatorError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BetQuoteOperatorError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _current_refinement_targets(
    nations_league: Mapping[str, Any], *, now: datetime
) -> list[dict[str, str]]:
    try:
        public = validate_live_public_nations_league(nations_league)
    except (NationsLeagueLivePublicError, TypeError, ValueError) as exc:
        raise BetQuoteOperatorError("trusted Nations League input is invalid") from exc
    targets: list[dict[str, str]] = []
    for fixture in public["fixtures"]:
        if fixture.get("phase") != PHASE:
            continue
        kickoff = _utc(fixture.get("kickoff_utc"), "fixture.kickoff_utc")
        if kickoff <= now:
            continue
        identity = fixture.get("canonical_identity")
        if not isinstance(identity, Mapping):
            raise BetQuoteOperatorError("fixture canonical identity is missing")
        home_team = identity.get("home_team")
        away_team = identity.get("away_team")
        if (
            not isinstance(home_team, str)
            or not home_team.strip()
            or not isinstance(away_team, str)
            or not away_team.strip()
        ):
            raise BetQuoteOperatorError("fixture canonical team identity is invalid")
        targets.append(
            {
                "fixture_id": str(fixture["fixture_id"]),
                "home_team": home_team.strip(),
                "away_team": away_team.strip(),
                "kickoff_utc": _stamp(kickoff),
                "phase": PHASE,
            }
        )
    if not targets:
        raise BetQuoteOperatorError("no future refinement fixtures are available")
    return sorted(targets, key=lambda item: item["fixture_id"])


def _validate_authorization(
    raw: Mapping[str, Any], *, targets: list[dict[str, str]], now: datetime
) -> dict[str, Any]:
    if raw.get("schema_version") != AUTHORIZATION_SCHEMA:
        raise BetQuoteOperatorError("unsupported bet-time quote authorization")
    required = {
        "schema_version",
        "authorization_id",
        "provider",
        "fixture_scope",
        "phase",
        "issued_at",
        "expires_at",
        "maximum_request_count",
        "retry_count",
        "no_bet",
        "publication",
        "production_activation",
        "betting",
        "authorization_digest",
    }
    if set(raw) != required:
        raise BetQuoteOperatorError("authorization fields are not exact")
    if (
        raw.get("provider") != PROVIDER
        or raw.get("phase") != PHASE
        or raw.get("maximum_request_count") != MAX_REQUESTS
        or raw.get("retry_count") != MAX_RETRIES
        or raw.get("no_bet") is not True
        or any(
            raw.get(key) is not False
            for key in ("publication", "production_activation", "betting")
        )
    ):
        raise BetQuoteOperatorError("authorization safety or budget binding is invalid")
    authorization_id = raw.get("authorization_id")
    if not isinstance(authorization_id, str) or not authorization_id.strip():
        raise BetQuoteOperatorError("authorization_id is required")
    issued = _utc(raw.get("issued_at"), "issued_at")
    expires = _utc(raw.get("expires_at"), "expires_at")
    if issued > now or expires <= now or expires <= issued:
        raise BetQuoteOperatorError("authorization is not currently valid")
    scope = raw.get("fixture_scope")
    expected_scope = [target["fixture_id"] for target in targets]
    if scope != expected_scope:
        raise BetQuoteOperatorError(
            "authorization fixture scope does not match current refinement targets"
        )
    supplied = raw.get("authorization_digest")
    if (
        not isinstance(supplied, str)
        or canonical_evidence_digest(
            {key: value for key, value in raw.items() if key != "authorization_digest"}
        )
        != supplied
    ):
        raise BetQuoteOperatorError("authorization digest mismatch")
    return dict(raw)


def preflight(
    *, input_path: Path, authorization_path: Path, now: datetime
) -> dict[str, Any]:
    payload = _json(input_path)
    nations_league = payload.get("nations_league")
    if not isinstance(nations_league, Mapping):
        raise BetQuoteOperatorError("input has no trusted nations_league object")
    targets = _current_refinement_targets(nations_league, now=now)
    authorization = _validate_authorization(
        _json(authorization_path), targets=targets, now=now
    )
    return {
        "status": "PREFLIGHT_READY",
        "authorization_id": authorization["authorization_id"],
        "provider": PROVIDER,
        "fixture_count": len(targets),
        "request_count": 0,
        "credential_access_count": 0,
        "retry_count": 0,
        "network_executed": False,
        "main_odds_requested": False,
        "fallback_used": False,
        "lifecycle_mutated": False,
    }


def execute(
    *, input_path: Path, authorization_path: Path, output_path: Path, now: datetime
) -> dict[str, Any]:
    payload = _json(input_path)
    nations_league = payload.get("nations_league")
    if not isinstance(nations_league, Mapping):
        raise BetQuoteOperatorError("input has no trusted nations_league object")
    targets = _current_refinement_targets(nations_league, now=now)
    authorization = _validate_authorization(
        _json(authorization_path), targets=targets, now=now
    )

    # Credential loading occurs only inside the reviewed transport after all
    # authorization and trusted-input checks have succeeded.
    (
        snapshots,
        request_count,
        retry_count,
        request,
        _quote_times,
        failures,
        captured_at,
    ) = _fetch_isports_market_snapshots(targets, captured_at=None)
    if failures or request_count != MAX_REQUESTS or retry_count != MAX_RETRIES:
        raise BetQuoteOperatorError(
            f"iSports quote coverage failed closed: {dict(sorted(failures.items()))}"
        )
    projection = build_nations_league_actionable_projection(
        nations_league,
        snapshots,
        now=captured_at,
        request_provenance=request,
    )
    projection["authorization_id"] = authorization["authorization_id"]
    projection["authorization_digest"] = authorization["authorization_digest"]
    body = {key: value for key, value in projection.items() if key != "artifact_digest"}
    from src.betting.nations_league_actionability import _digest

    projection["artifact_digest"] = _digest(body)
    validate_nations_league_actionable_projection(projection)
    atomic_write_json(
        output_path, projection, indent=2, ensure_ascii=False, sort_keys=True
    )
    # Reload the atomic artifact before reporting success; the operator never
    # receives an in-memory-only success claim.
    written = _json(output_path)
    validate_nations_league_actionable_projection(written)
    return {
        "status": "COMPLETED",
        "authorization_id": authorization["authorization_id"],
        "provider": PROVIDER,
        "request_count": request_count,
        "credential_access_count": 1,
        "retry_count": retry_count,
        "quote_captured_at": _stamp(captured_at),
        "signal_count": len(projection["signals"]),
        "artifact_digest": projection["artifact_digest"],
        "output": str(output_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="trusted public NL input JSON"
    )
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="private quote projection output"
    )
    parser.add_argument("--now", help="explicit UTC clock for offline preflight/tests")
    parser.add_argument("--execute-network", action="store_true")
    args = parser.parse_args(argv)
    now = _utc(args.now, "now") if args.now else datetime.now(timezone.utc)
    try:
        result = (
            execute(
                input_path=args.input,
                authorization_path=args.authorization_file,
                output_path=args.output,
                now=now,
            )
            if args.execute_network
            else preflight(
                input_path=args.input,
                authorization_path=args.authorization_file,
                now=now,
            )
        )
    except (
        BetQuoteOperatorError,
        NationsLeagueActionabilityError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps({"status": "FAILED_CLOSED", "reason": str(exc)}, sort_keys=True)
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
