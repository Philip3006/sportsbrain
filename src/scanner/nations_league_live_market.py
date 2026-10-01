"""Bounded current 1X2 market capture for the Nations League LIVE seam.

The existing The Odds API Nations League shadow transport is the only current
source with the reviewed 1X2 contract and provider-budget accounting.  This
module only materializes its response into the #247 canonical market-snapshot
batch.  It does not predict, publish, bet, or mutate financial state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_live_edge import (
    SNAPSHOT_BATCH_SCHEMA,
    NationsLeagueLiveEdgeError,
    build_market_snapshot,
)
from src.analysis.nations_league_live_runtime import due_phase
from src.config import canonical_name
from src.scanner.nations_league_shadow import (
    NationsLeagueShadowError,
    _event_identity,
    _event_market_odds,
    fetch_provider_events,
)

PROVIDER = "the_odds_api"
SPORT_KEY = "soccer_uefa_nations_league"
MAX_PROVIDER_REQUESTS = 1
MAX_RETRIES = 0


class NationsLeagueLiveMarketError(ValueError):
    """A malformed manifest or unsafe market identity."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveMarketError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueLiveMarketError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueLiveMarketError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveMarketError(f"{field} is required")
    return value.strip()


def _captured_phases(
    existing_records: Iterable[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    captured: set[tuple[str, str]] = set()
    for record in existing_records:
        if not isinstance(record, Mapping):
            raise NationsLeagueLiveMarketError("LIVE prediction store row is malformed")
        fixture_id = _text(record.get("fixture_id"), "stored fixture_id")
        phase = _text(record.get("phase"), "stored phase")
        if phase not in {"initial", "refinement"}:
            raise NationsLeagueLiveMarketError("LIVE prediction store phase is invalid")
        key = (fixture_id, phase)
        if key in captured:
            raise NationsLeagueLiveMarketError("duplicate captured fixture phase")
        captured.add(key)
    return captured


def _manifest_targets(
    manifest: Mapping[str, Any],
    *,
    as_of: datetime,
    existing_records: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    if manifest.get("competition") != "UEFA Nations League":
        raise NationsLeagueLiveMarketError("wrong future fixture manifest")
    expected_digest = manifest.get("manifest_digest")
    if expected_digest != _digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    ):
        raise NationsLeagueLiveMarketError("future fixture manifest digest mismatch")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise NationsLeagueLiveMarketError("future fixture manifest fixtures are missing")

    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    captured_phases = _captured_phases(existing_records)
    due: list[dict[str, Any]] = []
    for raw in fixtures:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLiveMarketError("future fixture manifest row is malformed")
        if raw.get("status") != "VERIFIED":
            continue
        fixture_id = _text(raw.get("fixture_id"), "fixture_id")
        if fixture_id in seen_ids:
            raise NationsLeagueLiveMarketError("duplicate future fixture_id")
        seen_ids.add(fixture_id)
        home = canonical_name(_text(raw.get("home_team"), "home_team"))
        away = canonical_name(_text(raw.get("away_team"), "away_team"))
        kickoff = _utc(raw.get("kickoff_utc"), "kickoff_utc")
        identity = (home, away, _stamp(kickoff))
        if home == away or identity in seen_identities:
            raise NationsLeagueLiveMarketError("ambiguous future fixture identity")
        seen_identities.add(identity)
        phase, state = due_phase(_stamp(kickoff), _stamp(as_of))
        if phase is not None and (fixture_id, phase) not in captured_phases:
            due.append(
                {
                    "fixture_id": fixture_id,
                    "home_team": home,
                    "away_team": away,
                    "kickoff_utc": _stamp(kickoff),
                    "phase": phase,
                    "due_state": state,
                }
            )
    return due


def _provider_matches(
    events: Iterable[Mapping[str, Any]], target: Mapping[str, str]
) -> list[Mapping[str, Any]]:
    matches: list[Mapping[str, Any]] = []
    target_identity = (
        target["home_team"],
        target["away_team"],
        target["kickoff_utc"],
    )
    for event in events:
        if not isinstance(event, Mapping):
            continue
        accepted, _ = _event_identity(event)
        if not accepted:
            continue
        try:
            kickoff = _utc(str(event.get("commence_time", "")), "provider kickoff")
            identity = (
                canonical_name(_text(event.get("home_team"), "provider home_team")),
                canonical_name(_text(event.get("away_team"), "provider away_team")),
                _stamp(kickoff),
            )
        except NationsLeagueLiveMarketError:
            continue
        if identity == target_identity:
            matches.append(event)
    return matches


def _batch(
    *,
    selection_as_of: str,
    captured_at: str,
    due_targets: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    status: str,
    failure_reasons: Mapping[str, str],
    request: Mapping[str, Any] | None,
    request_count: int,
    retry_count: int,
    needs_provider: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": SNAPSHOT_BATCH_SCHEMA,
        "provider": PROVIDER,
        "sport_key": SPORT_KEY,
        "selection_as_of": selection_as_of,
        "captured_at": captured_at,
        "due_fixtures": [
            {
                "fixture_id": target["fixture_id"],
                "phase": target["phase"],
                "due_state": target["due_state"],
            }
            for target in due_targets
        ],
        "snapshots": snapshots,
        "status": status,
        "failure_reasons": dict(sorted(failure_reasons.items())),
        "request": dict(request) if request is not None else None,
        "request_count": request_count,
        "retry_count": retry_count,
        "needs_provider": needs_provider,
        "no_bet": True,
        "betting_enabled": False,
        "ledger_mutation": False,
    }
    body["batch_digest"] = _digest(body)
    return body


def prepare_market_preflight(
    manifest: Mapping[str, Any],
    *,
    as_of: str,
    existing_records: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a zero-network provider-need decision and empty batch."""

    selection_clock = _utc(as_of, "as_of")
    due_targets = _manifest_targets(
        manifest,
        as_of=selection_clock,
        existing_records=existing_records,
    )
    needs_provider = bool(due_targets)
    return _batch(
        selection_as_of=_stamp(selection_clock),
        captured_at=_stamp(selection_clock),
        due_targets=due_targets,
        snapshots=[],
        status="PROVIDER_REQUIRED" if needs_provider else "NO_MARKET_SNAPSHOT",
        failure_reasons={},
        request=None,
        request_count=0,
        retry_count=0,
        needs_provider=needs_provider,
    )


def acquire_live_market_snapshots(
    manifest: Mapping[str, Any],
    *,
    as_of: str,
    existing_records: Iterable[Mapping[str, Any]] = (),
    fetcher: Callable[[], tuple[list[dict[str, Any]], int, int, dict[str, Any]]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture due current 1X2 markets, or return a safe empty batch.

    The fetcher is deliberately injected for deterministic tests.  The default
    is the existing one-request, no-retry The Odds API transport, which owns
    the provider budget gate and quota-header accounting.
    """

    selection_clock = _utc(as_of, "as_of")
    due_targets = _manifest_targets(
        manifest,
        as_of=selection_clock,
        existing_records=existing_records,
    )
    selection_stamp = _stamp(selection_clock)

    if not due_targets:
        capture_clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return _batch(
            selection_as_of=selection_stamp,
            captured_at=_stamp(capture_clock),
            due_targets=[],
            snapshots=[],
            status="NO_MARKET_SNAPSHOT",
            failure_reasons={},
            request=None,
            request_count=0,
            retry_count=0,
            needs_provider=False,
        )

    provider_fetcher = fetcher or fetch_provider_events
    try:
        events, request_count, retry_count, request = provider_fetcher()
    except (NationsLeagueShadowError, OSError, RuntimeError, ValueError) as exc:
        capture_clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        safe_reason = f"provider_error:{type(exc).__name__}"
        return _batch(
            selection_as_of=selection_stamp,
            captured_at=_stamp(capture_clock),
            due_targets=due_targets,
            snapshots=[],
            status="NO_MARKET_SNAPSHOT",
            failure_reasons={target["fixture_id"]: safe_reason for target in due_targets},
            request=None,
            request_count=0,
            retry_count=0,
            needs_provider=True,
        )

    capture_clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        not isinstance(events, list)
        or request_count != MAX_PROVIDER_REQUESTS
        or retry_count != MAX_RETRIES
        or not isinstance(request, Mapping)
    ):
        return _batch(
            selection_as_of=selection_stamp,
            captured_at=_stamp(capture_clock),
            due_targets=due_targets,
            snapshots=[],
            status="MARKET_INVALID",
            failure_reasons={target["fixture_id"]: "provider_contract_invalid" for target in due_targets},
            request=None,
            request_count=0,
            retry_count=0,
            needs_provider=True,
        )

    snapshots: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for target in due_targets:
        matches = _provider_matches(events, target)
        fixture_id = target["fixture_id"]
        if not matches:
            failures[fixture_id] = "target_fixture_not_returned"
            continue
        if len(matches) != 1:
            failures[fixture_id] = "ambiguous_provider_fixture_identity"
            continue
        event = matches[0]
        event_id = _text(event.get("id"), "provider event id")
        odds, odds_error = _event_market_odds(event)
        if odds is None:
            failures[fixture_id] = odds_error or "incomplete_1x2_market"
            continue
        try:
            kickoff = _utc(str(event.get("commence_time", "")), "provider kickoff")
            if kickoff <= capture_clock:
                failures[fixture_id] = "provider_fixture_started"
                continue
            capture_phase, _ = due_phase(
                target["kickoff_utc"], _stamp(capture_clock)
            )
            if capture_phase != target["phase"]:
                failures[fixture_id] = "capture_window_changed"
                continue
            snapshots.append(
                build_market_snapshot(
                    {
                        "provider": PROVIDER,
                        "bookmaker": odds["bookmaker"],
                        "captured_at": _stamp(capture_clock),
                        "fixture_id": fixture_id,
                        "provider_match_id": event_id,
                        "odds_decimal": {
                            "home": odds["home"],
                            "draw": odds["draw"],
                            "away": odds["away"],
                        },
                    }
                )
            )
        except (NationsLeagueLiveEdgeError, NationsLeagueLiveMarketError) as exc:
            failures[fixture_id] = str(exc)

    if not failures and len(snapshots) == len(due_targets):
        status = "READY"
    elif snapshots:
        status = "PARTIAL_MARKET"
    else:
        status = "MARKET_INVALID" if any(
            reason in {"ambiguous_provider_fixture_identity", "incomplete_1x2_market", "malformed_h2h_odds"}
            for reason in failures.values()
        ) else "NO_MARKET_SNAPSHOT"
    return _batch(
        selection_as_of=selection_stamp,
        captured_at=_stamp(capture_clock),
        due_targets=due_targets,
        snapshots=snapshots,
        status=status,
        failure_reasons=failures,
        request=request,
        request_count=request_count,
        retry_count=retry_count,
        needs_provider=True,
    )


def write_market_snapshot_batch(path: Path, batch: Mapping[str, Any]) -> Path:
    """Write one local ephemeral batch for the provider-free lifecycle step."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(batch), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
