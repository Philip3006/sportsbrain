"""Bounded current 1X2 market capture for the Nations League LIVE seam.

The Odds API remains the primary source.  When its provider-free budget or
circuit state says it is unavailable, the reviewed iSports schedule plus
bulk-European-odds contract is used once as a bounded fallback.  This module
only materializes the canonical market-snapshot batch.  It does not predict,
publish, bet, or mutate financial state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_live_edge import (
    MAX_MARKET_AGE,
    SNAPSHOT_BATCH_SCHEMA,
    NationsLeagueLiveEdgeError,
    build_market_snapshot,
)
from src.analysis.nations_league_live_runtime import due_phase
from src.config import canonical_name
from src.data.isports_api import (
    EUROPEAN_ODDS_PATH,
    SCHEDULE_PATH,
    IsportsApiError,
    load_isports_api_key,
    request_once,
)
from src.scanner import nations_league_isports_shadow as isports_shadow
from src.scanner.nations_league_shadow import (
    NationsLeagueShadowError,
    _event_identity,
    _event_market_odds,
    fetch_provider_events,
)

PROVIDER = "the_odds_api"
ISPORTS_PROVIDER = "isports_api"
PROVIDER_ORDER = (PROVIDER, ISPORTS_PROVIDER)
SPORT_KEY = "soccer_uefa_nations_league"
MAX_PROVIDER_REQUESTS = 1
MAX_ISPORTS_REQUESTS = 2
MAX_RETRIES = 0


class NationsLeagueLiveMarketError(ValueError):
    """A malformed manifest or unsafe market identity."""


def _provider_budget_snapshot() -> dict[str, dict[str, Any]]:
    from src.signals.provider_budget import get_budget_snapshot

    snapshot = get_budget_snapshot()
    return snapshot if isinstance(snapshot, dict) else {}


def _primary_provider_state(*, now: datetime) -> str:
    """Read the primary state without probing or changing provider state."""

    from src.signals.provider_budget import odds_api_quota_state

    quota = odds_api_quota_state()
    if isinstance(quota, Mapping):
        remaining = quota.get("requests_remaining")
        if (
            isinstance(remaining, int)
            and not isinstance(remaining, bool)
            and remaining <= 0
        ):
            return "QUOTA_EXHAUSTED"
    budget = _provider_budget_snapshot()
    primary = budget.get(PROVIDER, {})
    if isinstance(primary, Mapping) and primary.get("circuit_open") is True:
        return "CIRCUIT_OPEN"
    return "AVAILABLE"


def _provider_state(name: str) -> str:
    budget = _provider_budget_snapshot()
    entry = budget.get(name, {})
    if isinstance(entry, Mapping) and entry.get("circuit_open") is True:
        return "CIRCUIT_OPEN"
    return "AVAILABLE"


def _provider_trace(
    *,
    selected_provider: str | None,
    fallback_depth: int,
    primary_provider_state: str,
    provider_attempt_count: int,
    provider_request_counts: Mapping[str, int],
    retry_count: int,
    safe_failure: str | None = None,
    quote_updated_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    counts = {
        provider: int(provider_request_counts.get(provider, 0))
        for provider in PROVIDER_ORDER
    }
    trace: dict[str, Any] = {
        "configured_provider_order": list(PROVIDER_ORDER),
        "selected_provider": selected_provider,
        "fallback_depth": int(fallback_depth),
        "primary_provider_state": primary_provider_state,
        "provider_attempt_count": int(provider_attempt_count),
        "provider_request_counts": counts,
        "total_network_request_count": sum(counts.values()),
        "retry_count": int(retry_count),
    }
    if safe_failure is not None:
        trace["safe_failure"] = safe_failure
    if quote_updated_at:
        trace["quote_updated_at"] = dict(sorted(quote_updated_at.items()))
    return trace


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
        raise NationsLeagueLiveMarketError(
            "future fixture manifest fixtures are missing"
        )

    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    captured_phases = _captured_phases(existing_records)
    due: list[dict[str, Any]] = []
    for raw in fixtures:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLiveMarketError(
                "future fixture manifest row is malformed"
            )
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


def _isports_target_matches(
    fixtures: Iterable[Mapping[str, Any]], target: Mapping[str, str]
) -> list[dict[str, Any]]:
    target_identity = (
        target["home_team"],
        target["away_team"],
        target["kickoff_utc"],
    )
    matches: list[dict[str, Any]] = []
    for fixture in fixtures:
        try:
            identity = (
                canonical_name(_text(fixture.get("home_team"), "schedule home_team")),
                canonical_name(_text(fixture.get("away_team"), "schedule away_team")),
                _stamp(fixture["kickoff"]),
            )
        except (KeyError, NationsLeagueLiveMarketError):
            continue
        if identity == target_identity:
            matches.append(dict(fixture))
    return matches


def _isports_request_descriptor(
    operations: list[dict[str, Any]], rate_evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return only the reviewed, credential-free iSports provenance."""

    return {
        "provider": ISPORTS_PROVIDER,
        "provider_operation_manifest": operations,
        "provider_rate_evidence": rate_evidence,
    }


def _isports_error_reason(exc: BaseException) -> str:
    if isinstance(exc, IsportsApiError):
        if exc.http_status in {401, 403}:
            return "isports_api_auth_unavailable"
        if exc.http_status == 429:
            return "isports_api_rate_limited"
        return "isports_api_request_failed"
    return f"isports_api_error:{type(exc).__name__}"


def _record_isports_error(exc: BaseException) -> None:
    from src.signals.provider_budget import record_error

    status = getattr(exc, "http_status", None)
    code = (
        int(status) if isinstance(status, int) and not isinstance(status, bool) else 0
    )
    record_error(ISPORTS_PROVIDER, code, open_circuit=code in {401, 403, 429})


def _fetch_isports_market_snapshots(
    due_targets: list[dict[str, Any]],
    *,
    captured_at: datetime | None,
    api_key: str | None = None,
    transport: Callable[..., Any] | None = None,
) -> tuple[
    list[dict[str, Any]],
    int,
    int,
    dict[str, Any],
    dict[str, str],
    dict[str, str],
    datetime,
]:
    """Fetch one schedule plus one bounded bulk-European-odds request.

    This deliberately reuses the already-reviewed iSports identity and odds
    parsers. It materializes only canonical LIVE market snapshots; it does not
    invoke the model or create a shadow artifact.
    """

    key = api_key if api_key is not None else load_isports_api_key()
    if not key:
        raise IsportsApiError(
            "iSports credential is unavailable", request_count=0, http_status=None
        )

    operations: list[dict[str, Any]] = []
    rate_evidence: list[dict[str, Any]] = []
    schedule_operation = request_once(
        api_key=key,
        operation_kind="schedule",
        ordinal=1,
        endpoint_path=SCHEDULE_PATH,
        query={"leagueId": str(isports_shadow.PROVIDER_LEAGUE_ID)},
        transport=transport,
    )
    operations.append(schedule_operation.manifest_entry)
    if schedule_operation.safe_rate_headers:
        rate_evidence.append(
            {"ordinal": 1, "headers": schedule_operation.safe_rate_headers}
        )

    schedule_clock = (captured_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    schedule_fixtures, _excluded = isports_shadow._schedule_fixtures(
        schedule_operation.payload, captured_at=schedule_clock
    )
    target_fixtures: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for target in due_targets:
        matches = _isports_target_matches(schedule_fixtures, target)
        fixture_id = target["fixture_id"]
        if not matches:
            failures[fixture_id] = "target_fixture_not_returned"
        elif len(matches) != 1:
            failures[fixture_id] = "ambiguous_provider_fixture_identity"
        else:
            target_fixtures.append(matches[0])

    if failures or not target_fixtures:
        return (
            [],
            1,
            0,
            _isports_request_descriptor(operations, rate_evidence),
            {},
            failures,
            schedule_clock,
        )

    eligible_match_ids = isports_shadow._eligible_match_ids(target_fixtures)
    odds_operation = request_once(
        api_key=key,
        operation_kind="odds",
        ordinal=2,
        endpoint_path=EUROPEAN_ODDS_PATH,
        query={"matchId": ",".join(eligible_match_ids)},
        transport=transport,
    )
    operations.append(odds_operation.manifest_entry)
    if odds_operation.safe_rate_headers:
        rate_evidence.append(
            {"ordinal": 2, "headers": odds_operation.safe_rate_headers}
        )
    final_capture_clock = (captured_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    isports_shadow._validate_operation_manifest(
        operations,
        captured_at=final_capture_clock,
        eligible_match_ids=eligible_match_ids,
    )
    odds_by_match, skipped = isports_shadow._odds_records_by_match(
        odds_operation.payload, target_fixtures
    )
    if len(odds_by_match) + len(skipped) != len(target_fixtures):
        raise isports_shadow.NationsLeagueIsportsError(
            "iSports odds coverage accounting is incomplete", request_count=2
        )
    snapshots: list[dict[str, Any]] = []
    quote_updated_at: dict[str, str] = {}
    for target in due_targets:
        fixture_id = target["fixture_id"]
        matches = _isports_target_matches(target_fixtures, target)
        if len(matches) != 1:
            failures[fixture_id] = "ambiguous_provider_fixture_identity"
            continue
        provider_match_id = matches[0]["provider_match_id"]
        record = odds_by_match.get(provider_match_id)
        if record is None:
            failures[fixture_id] = "incomplete_1x2_market"
            continue
        try:
            market = isports_shadow._market_for_fixture(
                record, match_id=provider_match_id
            )
            capture_phase, _ = due_phase(
                target["kickoff_utc"], _stamp(final_capture_clock)
            )
            if capture_phase != target["phase"]:
                failures[fixture_id] = "capture_window_changed"
                continue
            if matches[0]["kickoff"] <= final_capture_clock:
                failures[fixture_id] = "provider_fixture_started"
                continue
            quote_times = [
                _utc(quote["change_time"], "iSports quote change_time")
                for quote in market["bookmakers"]
            ]
            if not quote_times or any(
                quote_time > final_capture_clock for quote_time in quote_times
            ):
                failures[fixture_id] = "quote_after_capture"
                continue
            latest_quote = max(quote_times)
            if final_capture_clock - latest_quote > MAX_MARKET_AGE:
                failures[fixture_id] = "stale_quote"
                continue
            snapshots.append(
                build_market_snapshot(
                    {
                        "provider": ISPORTS_PROVIDER,
                        "bookmaker": market["bookmaker"],
                        "captured_at": _stamp(final_capture_clock),
                        "fixture_id": fixture_id,
                        "provider_match_id": provider_match_id,
                        "odds_decimal": {
                            "home": market["home"],
                            "draw": market["draw"],
                            "away": market["away"],
                        },
                    }
                )
            )
            quote_updated_at[fixture_id] = _stamp(latest_quote)
        except (
            NationsLeagueLiveEdgeError,
            NationsLeagueLiveMarketError,
            isports_shadow.NationsLeagueIsportsError,
        ) as exc:
            failures[fixture_id] = (
                "incomplete_1x2_market"
                if "coverage gap" in str(exc)
                else "provider_market_invalid"
            )
    request = _isports_request_descriptor(operations, rate_evidence)
    return snapshots, 2, 0, request, quote_updated_at, failures, final_capture_clock


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
    selected_provider: str | None = None,
    provider_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trace = dict(
        provider_trace
        or _provider_trace(
            selected_provider=selected_provider,
            fallback_depth=0,
            primary_provider_state="NOT_REQUIRED" if not needs_provider else "UNKNOWN",
            provider_attempt_count=0,
            provider_request_counts={},
            retry_count=retry_count,
        )
    )
    body: dict[str, Any] = {
        "schema": SNAPSHOT_BATCH_SCHEMA,
        "provider": selected_provider or PROVIDER,
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
        "configured_provider_order": list(PROVIDER_ORDER),
        "selected_provider": selected_provider,
        "fallback_depth": trace.get("fallback_depth", 0),
        "primary_provider_state": trace.get("primary_provider_state", "UNKNOWN"),
        "provider_attempt_count": trace.get("provider_attempt_count", 0),
        "provider_request_counts": trace.get("provider_request_counts", {}),
        "total_network_request_count": trace.get(
            "total_network_request_count", request_count
        ),
        "provider_trace": trace,
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
    primary_state = _primary_provider_state(now=selection_clock)
    planned_provider = None
    fallback_depth = 0
    if needs_provider:
        planned_provider = (
            PROVIDER if primary_state == "AVAILABLE" else ISPORTS_PROVIDER
        )
        fallback_depth = int(planned_provider == ISPORTS_PROVIDER)
    trace = _provider_trace(
        selected_provider=planned_provider,
        fallback_depth=fallback_depth,
        primary_provider_state=primary_state if needs_provider else "NOT_REQUIRED",
        provider_attempt_count=0,
        provider_request_counts={},
        retry_count=0,
    )
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
        selected_provider=planned_provider,
        provider_trace=trace,
    )


def acquire_live_market_snapshots(
    manifest: Mapping[str, Any],
    *,
    as_of: str,
    existing_records: Iterable[Mapping[str, Any]] = (),
    fetcher: Callable[[], tuple[list[dict[str, Any]], int, int, dict[str, Any]]]
    | None = None,
    isports_transport: Callable[..., Any] | None = None,
    isports_api_key: str | None = None,
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
        trace = _provider_trace(
            selected_provider=None,
            fallback_depth=0,
            primary_provider_state="NOT_REQUIRED",
            provider_attempt_count=0,
            provider_request_counts={},
            retry_count=0,
        )
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
            selected_provider=None,
            provider_trace=trace,
        )

    capture_clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    primary_state = _primary_provider_state(now=selection_clock)
    fallback_depth = 0
    provider_attempt_count = 0
    provider_request_counts: dict[str, int] = {
        provider: 0 for provider in PROVIDER_ORDER
    }
    request: Mapping[str, Any] | None = None
    quote_updated_at: dict[str, str] = {}
    failures: dict[str, str] = {}
    injected_provider_failure = False

    if fetcher is not None or primary_state == "AVAILABLE":
        provider_attempt_count = 1
        provider_fetcher = fetcher or fetch_provider_events
        try:
            events, request_count, retry_count, request = provider_fetcher()
            provider_request_counts[PROVIDER] = request_count
        except (NationsLeagueShadowError, OSError, RuntimeError, ValueError) as exc:
            injected_provider_failure = fetcher is not None
            message = str(exc).casefold()
            provider_request_counts[PROVIDER] = (
                0
                if fetcher is not None
                or "credential" in message
                or "budget gate" in message
                else 1
            )
            primary_state = "UNAVAILABLE"
            fallback_depth = 1
            provider_attempt_count = 2
            events = None
            request_count = provider_request_counts[PROVIDER]
            retry_count = 0
            failures = {
                target["fixture_id"]: f"provider_error:{type(exc).__name__}"
                for target in due_targets
            }
        else:
            request_count = provider_request_counts[PROVIDER]
            retry_count = int(retry_count)
    else:
        events = None
        request_count = 0
        retry_count = 0
        fallback_depth = 1
        provider_attempt_count = 1

    if events is None:
        if injected_provider_failure:
            trace = _provider_trace(
                selected_provider=None,
                fallback_depth=0,
                primary_provider_state=primary_state,
                provider_attempt_count=provider_attempt_count,
                provider_request_counts=provider_request_counts,
                retry_count=retry_count,
                safe_failure=next(iter(failures.values()), "provider_unavailable"),
            )
            return _batch(
                selection_as_of=selection_stamp,
                captured_at=_stamp(capture_clock),
                due_targets=due_targets,
                snapshots=[],
                status="NO_MARKET_SNAPSHOT",
                failure_reasons=failures,
                request=None,
                request_count=sum(provider_request_counts.values()),
                retry_count=retry_count,
                needs_provider=True,
                selected_provider=None,
                provider_trace=trace,
            )
        # The primary error is represented in the trace; only the selected
        # provider's target-level result belongs in failure_reasons.
        failures = {}
        isports_state = _provider_state(ISPORTS_PROVIDER)
        if isports_state == "CIRCUIT_OPEN":
            failures = {
                target["fixture_id"]: "isports_api_circuit_open"
                for target in due_targets
            }
            provider_attempt_count = max(provider_attempt_count - 1, 0)
        else:
            try:
                (
                    isports_snapshots,
                    isports_count,
                    isports_retries,
                    request,
                    quote_updated_at,
                    isports_failures,
                    isports_capture_clock,
                ) = _fetch_isports_market_snapshots(
                    due_targets,
                    captured_at=capture_clock if now is not None else None,
                    api_key=isports_api_key,
                    transport=isports_transport,
                )
                capture_clock = isports_capture_clock
                snapshots = isports_snapshots
                provider_request_counts[ISPORTS_PROVIDER] = isports_count
                retry_count = isports_retries
                from src.signals.provider_budget import record_success

                record_success(ISPORTS_PROVIDER)
                failures.update(isports_failures)
            except (
                IsportsApiError,
                isports_shadow.NationsLeagueIsportsError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                provider_request_counts[ISPORTS_PROVIDER] = int(
                    getattr(exc, "request_count", 0) or 0
                )
                retry_count = 0
                failures = {
                    target["fixture_id"]: _isports_error_reason(exc)
                    for target in due_targets
                }
                try:
                    _record_isports_error(exc)
                except (OSError, RuntimeError, TypeError, ValueError):
                    failures = {
                        target["fixture_id"]: "isports_api_accounting_failed"
                        for target in due_targets
                    }
            else:
                trace = _provider_trace(
                    selected_provider=ISPORTS_PROVIDER,
                    fallback_depth=fallback_depth,
                    primary_provider_state=primary_state,
                    provider_attempt_count=provider_attempt_count,
                    provider_request_counts=provider_request_counts,
                    retry_count=retry_count,
                    quote_updated_at=quote_updated_at,
                )
                status = (
                    "READY"
                    if not failures and len(snapshots) == len(due_targets)
                    else "PARTIAL_MARKET"
                    if snapshots
                    else "MARKET_INVALID"
                    if any(
                        reason
                        in {
                            "ambiguous_provider_fixture_identity",
                            "incomplete_1x2_market",
                            "provider_market_invalid",
                            "quote_after_capture",
                            "stale_quote",
                        }
                        for reason in failures.values()
                    )
                    else "NO_MARKET_SNAPSHOT"
                )
                return _batch(
                    selection_as_of=selection_stamp,
                    captured_at=_stamp(capture_clock),
                    due_targets=due_targets,
                    snapshots=snapshots,
                    status=status,
                    failure_reasons=failures,
                    request=request,
                    request_count=sum(provider_request_counts.values()),
                    retry_count=retry_count,
                    needs_provider=True,
                    selected_provider=ISPORTS_PROVIDER,
                    provider_trace=trace,
                )

    if events is None:
        trace = _provider_trace(
            selected_provider=None,
            fallback_depth=fallback_depth,
            primary_provider_state=primary_state,
            provider_attempt_count=provider_attempt_count,
            provider_request_counts=provider_request_counts,
            retry_count=retry_count,
            safe_failure=next(iter(failures.values()), "no_qualified_provider"),
        )
        return _batch(
            selection_as_of=selection_stamp,
            captured_at=_stamp(capture_clock),
            due_targets=due_targets,
            snapshots=[],
            status="NO_MARKET_SNAPSHOT",
            failure_reasons=failures
            or {
                target["fixture_id"]: "no_qualified_provider" for target in due_targets
            },
            request=request,
            request_count=sum(provider_request_counts.values()),
            retry_count=retry_count,
            needs_provider=True,
            selected_provider=None,
            provider_trace=trace,
        )

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
            failure_reasons={
                target["fixture_id"]: "provider_contract_invalid"
                for target in due_targets
            },
            request=None,
            request_count=0,
            retry_count=0,
            needs_provider=True,
            selected_provider=PROVIDER,
            provider_trace=_provider_trace(
                selected_provider=PROVIDER,
                fallback_depth=0,
                primary_provider_state=primary_state,
                provider_attempt_count=provider_attempt_count,
                provider_request_counts={PROVIDER: 0, ISPORTS_PROVIDER: 0},
                retry_count=0,
            ),
        )

    snapshots = []
    failures = {}
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
            capture_phase, _ = due_phase(target["kickoff_utc"], _stamp(capture_clock))
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
        status = (
            "MARKET_INVALID"
            if any(
                reason
                in {
                    "ambiguous_provider_fixture_identity",
                    "incomplete_1x2_market",
                    "malformed_h2h_odds",
                }
                for reason in failures.values()
            )
            else "NO_MARKET_SNAPSHOT"
        )
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
        selected_provider=PROVIDER,
        provider_trace=_provider_trace(
            selected_provider=PROVIDER,
            fallback_depth=0,
            primary_provider_state=primary_state,
            provider_attempt_count=provider_attempt_count,
            provider_request_counts=provider_request_counts,
            retry_count=retry_count,
        ),
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
