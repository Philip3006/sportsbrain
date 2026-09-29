"""Offline-safe executor and validator for the Nations League odds plan.

The module deliberately keeps the transport and credential resolver outside the
CLI.  This makes dry runs completely offline and makes a future real run
require an explicit, testable runtime integration after all preflight gates
have passed.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from src.betting.odds_utils import remove_margin_shin

PLAN_SCHEMA = "sportsbrain-nl-historical-odds-request-plan-v1"
PROVIDER = "the_odds_api"
SPORT_KEY = "soccer_uefa_nations_league"
REGION = "eu"
MARKET = "h2h"
EXPECTED_TIMELINE_DIGEST = (
    "842c0cc608b4221d63cbda079756dc392524e53349c9c24af5e8116c1d27e1af"
)
DEFAULT_PLAN_PATH = Path(
    "results/audits/nations_league_historical_odds_request_plan_20260929.json"
)
SAFE_HEADER_NAMES = frozenset(
    {
        "content-type",
        "x-requests-used",
        "x-requests-remaining",
        "x-requests-limit",
        "x-rate-limit",
        "x-rate-limit-remaining",
    }
)


class BackfillError(RuntimeError):
    """Base class for fail-closed backfill errors."""


class PlanIntegrityError(BackfillError):
    """The plan or one of its digests is not trustworthy."""


class PreflightError(BackfillError):
    """A runtime authorization or budget guard failed."""


class ResumeSafetyError(BackfillError):
    """A previous request may have been paid but is not completed safely."""


class ExecutionMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    PREDICTION_ONLY = "PREDICTION_ONLY"
    FULL_RESEARCH = "FULL_RESEARCH"


@dataclass(frozen=True)
class HistoricalRequest:
    phase: str
    requested_timestamp: str

    @property
    def identity_payload(self) -> dict[str, str]:
        return {
            "provider": PROVIDER,
            "sport_key": SPORT_KEY,
            "region": REGION,
            "market": MARKET,
            "phase": self.phase,
            "requested_timestamp": self.requested_timestamp,
        }

    @property
    def request_identifier(self) -> str:
        payload = json.dumps(
            self.identity_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            **self.identity_payload,
            "request_identifier": self.request_identifier,
        }


@dataclass(frozen=True)
class ProviderResponse:
    """Decoded response supplied by an explicitly injected future transport."""

    status_code: int
    payload: Any
    received_at: str
    headers: Mapping[str, object]
    provider_snapshot_timestamp: str | None = None
    next_snapshot_timestamp: str | None = None


class HistoricalOddsTransport(Protocol):
    def fetch(self, request: HistoricalRequest, credential: str) -> ProviderResponse:
        """Perform exactly one request for the canonical request identity."""


CredentialProvider = Callable[[], str]
Clock = Callable[[], datetime]


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PlanIntegrityError(f"{field} must be an ISO-8601 UTC timestamp")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PlanIntegrityError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PlanIntegrityError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value))


def _safe_headers(headers: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key).casefold(): str(value)
        for key, value in headers.items()
        if str(key).casefold() in SAFE_HEADER_NAMES
    }


def _norm_team(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _phase_key(value: object) -> str:
    if not isinstance(value, str):
        raise PlanIntegrityError("plan phase must be a string")
    phase = value.upper()
    if phase not in {"INITIAL", "REFINEMENT", "CLOSING_BENCHMARK"}:
        raise PlanIntegrityError(f"unsupported plan phase: {value!r}")
    return phase


@dataclass(frozen=True)
class LoadedPlan:
    path: Path
    file_digest: str
    raw: Mapping[str, Any]
    fixture_targets: tuple[Mapping[str, Any], ...]

    @property
    def timeline_digest(self) -> str:
        return str(self.raw["source"]["timeline_dataset_digest"])

    @property
    def credits_per_request(self) -> int:
        return int(self.raw["provider"]["estimated_credits_per_unique_request"])

    def phases_for(self, mode: ExecutionMode) -> tuple[str, ...]:
        if mode is ExecutionMode.PREDICTION_ONLY:
            return ("INITIAL", "REFINEMENT")
        if mode is ExecutionMode.FULL_RESEARCH:
            return ("INITIAL", "REFINEMENT", "CLOSING_BENCHMARK")
        raise PlanIntegrityError("DRY_RUN has no single execution phase set")

    def requests_for(self, mode: ExecutionMode) -> tuple[HistoricalRequest, ...]:
        if mode is ExecutionMode.DRY_RUN:
            raise PlanIntegrityError("DRY_RUN requests both named execution plans")
        requests: list[HistoricalRequest] = []
        plans = self.raw["plans"][mode.value]["phases"]
        for phase in plans:
            phase_name = _phase_key(phase["phase"])
            timestamps = phase["requested_historical_snapshot_timestamps"]
            requests.extend(HistoricalRequest(phase_name, str(ts)) for ts in timestamps)
        return tuple(requests)

    def estimated_credits(self, mode: ExecutionMode) -> int:
        if mode is ExecutionMode.DRY_RUN:
            return max(
                self.estimated_credits(ExecutionMode.PREDICTION_ONLY),
                self.estimated_credits(ExecutionMode.FULL_RESEARCH),
            )
        return len(self.requests_for(mode)) * self.credits_per_request


def load_plan(path: Path = DEFAULT_PLAN_PATH) -> LoadedPlan:
    """Load and structurally validate the immutable request-plan artifact."""

    raw_bytes = path.read_bytes()
    file_digest = _sha256_bytes(raw_bytes)
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise PlanIntegrityError(f"invalid JSON plan: {path}") from exc
    if raw.get("schema_version") != PLAN_SCHEMA:
        raise PlanIntegrityError("unsupported request-plan schema")
    if raw.get("status") != "NL_HISTORICAL_ODDS_REQUEST_PLAN_READY":
        raise PlanIntegrityError("request plan is not marked ready")
    provider = raw.get("provider", {})
    if {
        provider.get("name"),
        provider.get("sport_key"),
        provider.get("region"),
        provider.get("market"),
    } != {PROVIDER, SPORT_KEY, REGION, MARKET}:
        raise PlanIntegrityError("provider contract does not match governed plan")
    if raw.get("source", {}).get("timeline_dataset_digest") != EXPECTED_TIMELINE_DIGEST:
        raise PlanIntegrityError("unexpected canonical timeline digest in plan")
    if int(provider.get("estimated_credits_per_unique_request", 0)) <= 0:
        raise PlanIntegrityError("plan must declare a positive request credit cost")

    fixture_targets = tuple(raw.get("fixture_targets", ()))
    fixture_ids = [str(item.get("fixture_id", "")) for item in fixture_targets]
    if len(fixture_ids) != len(set(fixture_ids)) or not all(fixture_ids):
        raise PlanIntegrityError("fixture targets must have unique fixture IDs")

    for mode in (ExecutionMode.PREDICTION_ONLY, ExecutionMode.FULL_RESEARCH):
        phase_names: list[str] = []
        for phase in raw.get("plans", {}).get(mode.value, {}).get("phases", ()):
            phase_names.append(_phase_key(phase.get("phase")))
            timestamps = phase.get("requested_historical_snapshot_timestamps", ())
            if len(timestamps) != len(set(timestamps)):
                raise PlanIntegrityError(f"duplicate timestamps in {mode.value}")
            for timestamp in timestamps:
                _parse_utc(timestamp, "planned request timestamp")
        if tuple(phase_names) != (
            ("INITIAL", "REFINEMENT")
            if mode is ExecutionMode.PREDICTION_ONLY
            else ("INITIAL", "REFINEMENT", "CLOSING_BENCHMARK")
        ):
            raise PlanIntegrityError(f"wrong phase contract for {mode.value}")
        expected = int(raw["plans"][mode.value]["estimated_credits"])
        actual = len(load_requests_from_raw(raw, mode)) * int(
            provider["estimated_credits_per_unique_request"]
        )
        if expected != actual:
            raise PlanIntegrityError(f"credit arithmetic mismatch for {mode.value}")

    return LoadedPlan(path, file_digest, raw, fixture_targets)


def load_requests_from_raw(
    raw: Mapping[str, Any], mode: ExecutionMode
) -> tuple[HistoricalRequest, ...]:
    requests: list[HistoricalRequest] = []
    for phase in raw["plans"][mode.value]["phases"]:
        phase_name = _phase_key(phase["phase"])
        requests.extend(
            HistoricalRequest(phase_name, str(timestamp))
            for timestamp in phase["requested_historical_snapshot_timestamps"]
        )
    return tuple(requests)


@dataclass(frozen=True)
class PreflightContext:
    historical_entitlement: bool
    available_credits: int
    quota_reset_at: str
    requested_mode: ExecutionMode
    expected_plan_digest: str
    expected_timeline_digest: str
    safety_buffer_credits: int


def validate_preflight(plan: LoadedPlan, context: PreflightContext) -> dict[str, Any]:
    """Validate every authorization/budget gate before credential access."""

    if not context.historical_entitlement:
        raise PreflightError("Historical Odds entitlement was not confirmed")
    if context.available_credits < 0:
        raise PreflightError("available credit balance cannot be negative")
    if context.safety_buffer_credits < 0:
        raise PreflightError("safety buffer cannot be negative")
    try:
        _parse_utc(context.quota_reset_at, "quota reset timestamp")
    except PlanIntegrityError as exc:
        raise PreflightError(str(exc)) from exc
    if context.requested_mode not in ExecutionMode:
        raise PreflightError("unsupported requested execution mode")
    if context.expected_plan_digest != plan.file_digest:
        raise PreflightError("request-plan digest confirmation mismatch")
    if context.expected_timeline_digest != plan.timeline_digest:
        raise PreflightError("timeline digest confirmation mismatch")
    estimated = plan.estimated_credits(context.requested_mode)
    required = estimated + context.safety_buffer_credits
    if context.available_credits < required:
        raise PreflightError(
            f"insufficient credits: need {required}, have {context.available_credits}"
        )
    return {
        "status": "PASSED",
        "requested_mode": context.requested_mode.value,
        "estimated_credits": estimated,
        "safety_buffer_credits": context.safety_buffer_credits,
        "required_credits": required,
        "quota_reset_at": context.quota_reset_at,
        "plan_digest": plan.file_digest,
        "timeline_digest": plan.timeline_digest,
    }


class ExecutionManifest:
    """Append-only request ledger used solely for restart-safe research runs."""

    def __init__(self, path: Path):
        self.path = path
        self._latest: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ResumeSafetyError(
                        f"invalid execution manifest line {line_number}"
                    ) from exc
                request_id = record.get("request_identifier")
                if not isinstance(request_id, str) or not request_id:
                    raise ResumeSafetyError("manifest record has no request identifier")
                self._latest[request_id] = record

    def state(self, request_id: str) -> dict[str, Any] | None:
        return self._latest.get(request_id)

    def append(self, record: Mapping[str, Any]) -> None:
        request_id = record.get("request_identifier")
        if not isinstance(request_id, str) or not request_id:
            raise ResumeSafetyError("cannot append manifest record without identity")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._latest[request_id] = dict(record)


class RawResponseStore:
    """Write-once storage for decoded raw provider responses."""

    def __init__(self, root: Path):
        self.root = root

    def write_once(
        self,
        request: HistoricalRequest,
        response: ProviderResponse,
        response_digest: str,
    ) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{request.request_identifier}.json"
        envelope = {
            "schema_version": "sportsbrain-nl-historical-odds-raw-v1",
            "request_identifier": request.request_identifier,
            "phase": request.phase,
            "requested_historical_timestamp": request.requested_timestamp,
            "received_at": response.received_at,
            "provider_snapshot_timestamp": response.provider_snapshot_timestamp,
            "next_snapshot_timestamp": response.next_snapshot_timestamp,
            "response_digest": response_digest,
            "safe_headers": _safe_headers(response.headers),
            "status_code": response.status_code,
            "raw_provider_payload": response.payload,
        }
        serialized = (
            json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("response_digest") != response_digest:
                raise BackfillError(
                    "immutable raw response was presented with a new digest"
                )
            return path
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
        return path


class JoinedObservationStore:
    """Write-once normalized fixture join output; closing is research-only."""

    def __init__(self, root: Path):
        self.root = root

    def write_once(self, request: HistoricalRequest, joined: Mapping[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{request.request_identifier}.json"
        serialized = (
            json.dumps(joined, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        if path.exists():
            if path.read_text(encoding="utf-8") != serialized:
                raise BackfillError(
                    "immutable joined observation was presented with new content"
                )
            return path
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
        return path


def _response_timestamp(response: ProviderResponse) -> datetime:
    timestamp = response.provider_snapshot_timestamp
    if timestamp is None and isinstance(response.payload, Mapping):
        timestamp = response.payload.get("timestamp")
    return _parse_utc(timestamp, "provider snapshot timestamp")


def _next_timestamp(response: ProviderResponse) -> datetime | None:
    timestamp = response.next_snapshot_timestamp
    if timestamp is None and isinstance(response.payload, Mapping):
        timestamp = response.payload.get("next_timestamp")
    if timestamp is None:
        return None
    return _parse_utc(timestamp, "provider next snapshot timestamp")


def _events_from_payload(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, Mapping):
        events = payload.get("data", payload.get("events"))
    else:
        events = None
    if not isinstance(events, list) or not all(
        isinstance(item, Mapping) for item in events
    ):
        raise BackfillError("provider response has no valid event list")
    return list(events)


def _outcome_odds(
    event: Mapping[str, Any], home: str, away: str
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        return observations
    for bookmaker in sorted(
        (item for item in bookmakers if isinstance(item, Mapping)),
        key=lambda item: str(item.get("key", "")),
    ):
        markets = bookmaker.get("markets", [])
        if not isinstance(markets, list):
            continue
        for market in sorted(
            (
                item
                for item in markets
                if isinstance(item, Mapping) and item.get("key") == MARKET
            ),
            key=lambda item: str(item.get("last_update", "")),
        ):
            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue
            odds_by_name: dict[str, float] = {}
            raw_names: dict[str, str] = {}
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    continue
                name = outcome.get("name")
                price = outcome.get("price")
                key = _norm_team(name)
                if key == _norm_team(home):
                    label = "home"
                elif key == _norm_team(away):
                    label = "away"
                elif isinstance(name, str) and name.casefold() == "draw":
                    label = "draw"
                else:
                    continue
                if (
                    label in odds_by_name
                    or not isinstance(price, (int, float))
                    or price <= 1
                ):
                    continue
                odds_by_name[label] = float(price)
                raw_names[label] = str(name)
            if set(odds_by_name) != {"home", "draw", "away"}:
                continue
            raw_odds = (
                odds_by_name["home"],
                odds_by_name["draw"],
                odds_by_name["away"],
            )
            fair = remove_margin_shin(raw_odds)
            observations.append(
                {
                    "bookmaker_key": str(bookmaker.get("key", "")),
                    "bookmaker_title": bookmaker.get("title"),
                    "market_key": MARKET,
                    "last_update": market.get("last_update"),
                    "outcome_names": raw_names,
                    "raw_odds_decimal": {
                        "home": raw_odds[0],
                        "draw": raw_odds[1],
                        "away": raw_odds[2],
                    },
                    "implied_probabilities": {
                        "home": 1.0 / raw_odds[0],
                        "draw": 1.0 / raw_odds[1],
                        "away": 1.0 / raw_odds[2],
                    },
                    "shin_fair_probabilities": {
                        "home": fair[0],
                        "draw": fair[1],
                        "away": fair[2],
                    },
                }
            )
    return observations


def join_snapshot(
    request: HistoricalRequest,
    response: ProviderResponse,
    fixture_targets: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Join one bulk response without fabricating identity or odds."""

    if not 200 <= response.status_code < 300:
        raise BackfillError(f"provider returned HTTP {response.status_code}")
    events = _events_from_payload(response.payload)
    actual_snapshot = _response_timestamp(response)
    next_snapshot = _next_timestamp(response)
    targets = [
        item
        for item in fixture_targets
        if request.phase == "CLOSING_BENCHMARK"
        and item.get("closing_boundary_utc") == request.requested_timestamp
        or request.phase == "INITIAL"
        and item.get("initial_target_utc") == request.requested_timestamp
        or request.phase == "REFINEMENT"
        and item.get("refinement_target_utc") == request.requested_timestamp
    ]
    seen_event_ids: set[str] = set()
    fixtures: list[dict[str, Any]] = []
    for target in sorted(targets, key=lambda item: str(item["fixture_id"])):
        kickoff = _parse_utc(target["kickoff_utc"], "fixture kickoff_utc")
        candidates = [
            event
            for event in events
            if _norm_team(event.get("home_team")) == _norm_team(target["home_team"])
            and _norm_team(event.get("away_team")) == _norm_team(target["away_team"])
            and _parse_utc(event.get("commence_time"), "provider commence_time")
            == kickoff
        ]
        state = "matched"
        reason = None
        event = None
        if len(candidates) == 0:
            state, reason = "missing", "provider_event_not_found"
        elif len(candidates) > 1:
            state, reason = "ambiguous", "multiple_provider_events"
        else:
            event = candidates[0]
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                state, reason = "ambiguous", "missing_provider_event_id"
            elif event_id in seen_event_ids:
                state, reason = "ambiguous", "duplicate_provider_event_id"
            else:
                seen_event_ids.add(event_id)
        if event is not None and state == "matched":
            observations = _outcome_odds(
                event, target["home_team"], target["away_team"]
            )
            if not observations:
                state, reason = "missing", "missing_complete_h2h_market"
            else:
                provider_event_id = event["id"]
        else:
            observations = []
            provider_event_id = None
        temporal_ok = actual_snapshot < kickoff
        temporal_reason = None
        if request.phase == "INITIAL":
            hours = (kickoff - actual_snapshot).total_seconds() / 3600
            temporal_ok = temporal_ok and 22 <= hours <= 26
            temporal_reason = "outside_initial_window" if not temporal_ok else None
        elif request.phase == "REFINEMENT":
            minutes = (kickoff - actual_snapshot).total_seconds() / 60
            temporal_ok = temporal_ok and 60 <= minutes <= 120
            temporal_reason = "outside_refinement_window" if not temporal_ok else None
        else:
            temporal_ok = (
                temporal_ok and next_snapshot is not None and next_snapshot >= kickoff
            )
            temporal_reason = (
                "closing_snapshot_not_proven_latest_pre_kickoff"
                if not temporal_ok
                else None
            )
        if not temporal_ok and state == "matched":
            state, reason = "ambiguous", temporal_reason
        fixtures.append(
            {
                "fixture_id": target["fixture_id"],
                "kickoff_utc": target["kickoff_utc"],
                "home_team": target["home_team"],
                "away_team": target["away_team"],
                "phase": request.phase,
                "requested_snapshot_timestamp": request.requested_timestamp,
                "actual_provider_snapshot_timestamp": _iso(actual_snapshot),
                "provider_event_id": provider_event_id,
                "market_state": state,
                "missing_or_ambiguous_reason": reason,
                "bookmaker_market_observations": observations,
                "prediction_input": request.phase != "CLOSING_BENCHMARK"
                and state == "matched",
                "research_classification": (
                    "RESEARCH_BENCHMARK_ONLY"
                    if request.phase == "CLOSING_BENCHMARK"
                    else "PREDICTION_MARKET_OBSERVATION"
                ),
            }
        )
    return {
        "schema_version": "sportsbrain-nl-historical-odds-joined-v1",
        "request_identifier": request.request_identifier,
        "phase": request.phase,
        "requested_snapshot_timestamp": request.requested_timestamp,
        "provider_snapshot_timestamp": _iso(actual_snapshot),
        "next_snapshot_timestamp": _iso(next_snapshot) if next_snapshot else None,
        "closing_prediction_input_forbidden": request.phase == "CLOSING_BENCHMARK",
        "fixtures": fixtures,
        "accepted_fixture_count": sum(item["prediction_input"] for item in fixtures),
    }


def _request_manifest_record(
    request: HistoricalRequest,
    status: str,
    *,
    credit_cost: int = 10,
    response_digest: str | None = None,
    completed_at: str | None = None,
    retry_state: str = "none",
    outcome: str | None = None,
) -> dict[str, Any]:
    return {
        "request_identifier": request.request_identifier,
        "phase": request.phase,
        "requested_historical_timestamp": request.requested_timestamp,
        "provider": PROVIDER,
        "sport_key": SPORT_KEY,
        "region": REGION,
        "market": MARKET,
        "execution_status": status,
        "response_digest": response_digest,
        "completed_at": completed_at,
        "credit_cost": credit_cost,
        "retry_state": retry_state,
        "outcome": outcome,
    }


class BackfillExecutor:
    def __init__(self, plan: LoadedPlan):
        self.plan = plan

    def dry_run(self, context: PreflightContext) -> dict[str, Any]:
        preflight = validate_preflight(self.plan, context)
        reports: dict[str, Any] = {}
        for mode in (ExecutionMode.PREDICTION_ONLY, ExecutionMode.FULL_RESEARCH):
            requests = self.plan.requests_for(mode)
            reports[mode.value] = {
                "unique_http_requests": len(requests),
                "estimated_credits": self.plan.estimated_credits(mode),
                "requests": [request.as_dict() for request in requests],
            }
        return {
            "schema_version": "sportsbrain-nl-historical-odds-dry-run-v1",
            "mode": ExecutionMode.DRY_RUN.value,
            "preflight": preflight,
            "network_requests": 0,
            "credential_accesses": 0,
            "plans": reports,
        }

    def execute(
        self,
        context: PreflightContext,
        *,
        transport: HistoricalOddsTransport,
        credential_provider: CredentialProvider,
        manifest: ExecutionManifest,
        raw_store: RawResponseStore,
        joined_store: JoinedObservationStore,
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> dict[str, Any]:
        preflight = validate_preflight(self.plan, context)
        if context.requested_mode is ExecutionMode.DRY_RUN:
            raise PreflightError("use dry_run() for DRY_RUN; it has no transport")
        credential = credential_provider()
        if not isinstance(credential, str) or not credential:
            raise PreflightError(
                "approved runtime credential mechanism returned no credential"
            )
        requests = self.plan.requests_for(context.requested_mode)
        executed = 0
        skipped = 0
        accepted = 0
        failed_closed = 0
        for request in requests:
            prior = manifest.state(request.request_identifier)
            if prior and prior.get("execution_status") == "completed":
                skipped += 1
                continue
            if prior:
                raise ResumeSafetyError(
                    f"request {request.request_identifier} has an unresolved prior attempt"
                )
            manifest.append(
                _request_manifest_record(
                    request, "started", credit_cost=self.plan.credits_per_request
                )
            )
            try:
                response = transport.fetch(request, credential)
            except Exception as exc:
                manifest.append(
                    _request_manifest_record(
                        request,
                        "uncertain",
                        credit_cost=self.plan.credits_per_request,
                        retry_state="blocked_after_transport_error",
                        outcome=type(exc).__name__,
                    )
                )
                raise BackfillError(
                    f"transport failed for {request.request_identifier}; resume is blocked"
                ) from exc
            response_digest = _sha256_json(response.payload)
            raw_store.write_once(request, response, response_digest)
            outcome = "accepted"
            joined: dict[str, Any] | None = None
            try:
                joined = join_snapshot(request, response, self.plan.fixture_targets)
                accepted += int(joined["accepted_fixture_count"])
                if any(
                    item["market_state"] != "matched" for item in joined["fixtures"]
                ):
                    outcome = "accepted_with_missing_or_ambiguous_fixtures"
                joined_store.write_once(request, joined)
            except BackfillError as exc:
                outcome = f"failed_closed:{type(exc).__name__}"
                failed_closed += 1
            manifest.append(
                _request_manifest_record(
                    request,
                    "completed",
                    credit_cost=self.plan.credits_per_request,
                    response_digest=response_digest,
                    completed_at=_iso(clock()),
                    outcome=outcome,
                )
            )
            executed += 1
        return {
            "schema_version": "sportsbrain-nl-historical-odds-execution-v1",
            "mode": context.requested_mode.value,
            "preflight": preflight,
            "network_requests": executed,
            "skipped_completed_requests": skipped,
            "accepted_fixture_observations": accepted,
            "failed_closed_requests": failed_closed,
            "credential_accesses": 1,
        }
