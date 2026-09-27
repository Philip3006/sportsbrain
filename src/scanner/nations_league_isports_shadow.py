"""Provider-native iSports capture and model bridge for UEFA Nations League."""

from __future__ import annotations

import math
import re
import statistics
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import DATA_CACHE, MODELS_DIR, canonical_name
from src.data.isports_api import (
    EUROPEAN_ODDS_PATH,
    SCHEDULE_PATH,
    IsportsApiError,
    load_isports_api_key,
    request_once,
)
from src.scanner.nations_league_shadow import (
    ACTIVE_END_EXCLUSIVE,
    ACTIVE_START,
    FrozenSnapshot,
    NationsLeagueShadowError,
    _canonical_json,
    _sha256_bytes,
    current_source_sha,
    load_cached_history,
    load_frozen_snapshot,
    predict_fixture,
    write_immutable_artifact,
)

PROVIDER = "isports_api"
PROVIDER_LEAGUE_ID = 146819
COMPETITION = "UEFA Nations League"
ARTIFACT_SCHEMA = "nations-league-isports-shadow-v1"
SNAPSHOT_DIR = MODELS_DIR / "snapshots" / "wm2026"
MAX_HISTORY_AGE = timedelta(hours=24)


class NationsLeagueIsportsError(RuntimeError):
    """A sanitized fail-closed iSports capture or coverage error."""

    def __init__(
        self,
        message: str,
        *,
        request_count: int = 0,
        http_statuses: list[int] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_count = request_count
        self.http_statuses = list(http_statuses or [])


def _parse_status(value: Any) -> int:
    if isinstance(value, bool):
        raise NationsLeagueIsportsError("schedule status is malformed")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise NationsLeagueIsportsError("schedule status is malformed") from None


def _parse_timestamp(value: Any, label: str) -> datetime:
    if isinstance(value, bool):
        raise NationsLeagueIsportsError(f"{label} is malformed")
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    raise NationsLeagueIsportsError(f"{label} is malformed")


def _schedule_fixtures(
    rows: list[dict[str, Any]], *, captured_at: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the league-filtered schedule and select the existing target window."""
    captured_utc = captured_at.astimezone(timezone.utc)
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        match_id = str(row.get("matchId", "")).strip()
        if not match_id:
            raise NationsLeagueIsportsError("schedule row has no stable matchId")
        if match_id in seen:
            raise NationsLeagueIsportsError(
                f"schedule contains duplicate matchId {match_id}"
            )
        seen.add(match_id)
        if str(row.get("leagueId", "")) != str(PROVIDER_LEAGUE_ID):
            raise NationsLeagueIsportsError(
                f"schedule matchId {match_id} has unexpected provider league ID"
            )
        league_name = " ".join(str(row.get("leagueName", "")).casefold().split())
        if league_name != COMPETITION.casefold():
            raise NationsLeagueIsportsError(
                f"schedule matchId {match_id} has unexpected competition identity"
            )
        if row.get("leagueType") not in (None, 2, "2"):
            raise NationsLeagueIsportsError(
                f"schedule matchId {match_id} is not a cup competition"
            )
        kickoff = _parse_timestamp(row.get("matchTime"), "schedule matchTime")
        status = _parse_status(row.get("status"))
        neutral = row.get("neutral")
        if not isinstance(neutral, bool):
            raise NationsLeagueIsportsError(
                f"schedule matchId {match_id} has no boolean neutral flag"
            )
        home = row.get("homeName")
        away = row.get("awayName")
        if (
            not isinstance(home, str)
            or not home.strip()
            or not isinstance(away, str)
            or not away.strip()
        ):
            raise NationsLeagueIsportsError(
                f"schedule matchId {match_id} has incomplete team identity"
            )
        entry = {
            "provider_match_id": match_id,
            "provider_league_id": PROVIDER_LEAGUE_ID,
            "league_name": COMPETITION,
            "kickoff": kickoff,
            "home_team": home,
            "away_team": away,
            "neutral": neutral,
            "status": status,
        }
        if not (ACTIVE_START <= kickoff < ACTIVE_END_EXCLUSIVE):
            excluded.append(
                {"provider_match_id": match_id, "reason": "outside_active_window"}
            )
        elif kickoff <= captured_utc:
            excluded.append(
                {"provider_match_id": match_id, "reason": "kickoff_not_future"}
            )
        elif status != 0:
            excluded.append(
                {"provider_match_id": match_id, "reason": f"schedule_status_{status}"}
            )
        else:
            eligible.append(entry)
    eligible.sort(
        key=lambda fixture: (fixture["kickoff"], fixture["provider_match_id"])
    )
    if not eligible:
        raise NationsLeagueIsportsError(
            "schedule returned no eligible future Nations League fixtures in the active window"
        )
    return eligible, excluded


def _valid_decimal_triple(
    home: Any, draw: Any, away: Any
) -> tuple[float, float, float] | None:
    try:
        values = (float(home), float(draw), float(away))
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 1.0 for value in values):
        return None
    return values


def _quote_update_time(value: Any, fallback: Any = None) -> datetime | None:
    for candidate in (value, fallback):
        if candidate is None:
            continue
        try:
            return _parse_timestamp(candidate, "odds changeTime")
        except NationsLeagueIsportsError:
            continue
    return None


def _bookmaker_quotes(odds_record: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_quotes = odds_record.get("odds")
    if not isinstance(raw_quotes, list):
        return []
    latest_by_company: dict[str, dict[str, Any]] = {}
    for raw_quote in raw_quotes:
        if not isinstance(raw_quote, Mapping):
            continue
        updated_at = _quote_update_time(
            raw_quote.get("changeTime"), odds_record.get("changeTime")
        )
        if updated_at is None:
            continue
        detail = raw_quote.get("oddsDetail")
        detail_rows = detail if isinstance(detail, list) else [detail]
        candidates: list[tuple[str, str, tuple[float, float, float]]] = []
        direct = _valid_decimal_triple(
            raw_quote.get("instantHome"),
            raw_quote.get("instantDraw"),
            raw_quote.get("instantAway"),
        )
        if direct is not None:
            company_id = str(raw_quote.get("companyId", "")).strip()
            company_name = str(raw_quote.get("companyName", "")).strip()
            if company_id and company_name:
                candidates.append((company_id, company_name, direct))
        for item in detail_rows:
            if not isinstance(item, str):
                continue
            columns = [column.strip() for column in item.split(",")]
            if len(columns) != 8:
                continue
            company_id, company_name = columns[0], columns[1]
            triple = _valid_decimal_triple(*columns[5:8])
            if company_id and company_name and triple is not None:
                candidates.append((company_id, company_name, triple))
        for company_id, company_name, triple in candidates:
            previous = latest_by_company.get(company_id)
            quote = {
                "company_id": company_id,
                "company_name": company_name,
                "change_time": updated_at.isoformat(),
                "_change_time": updated_at,
                "odds_decimal": {
                    "home": triple[0],
                    "draw": triple[1],
                    "away": triple[2],
                },
                "_triple": triple,
            }
            if previous is None or updated_at > previous["_change_time"]:
                latest_by_company[company_id] = quote
            elif (
                updated_at == previous["_change_time"] and triple != previous["_triple"]
            ):
                raise NationsLeagueIsportsError(
                    f"bookmaker {company_id} has conflicting quotes at the same changeTime"
                )
            elif (
                updated_at == previous["_change_time"]
                and company_name != previous["company_name"]
            ):
                raise NationsLeagueIsportsError(
                    f"bookmaker {company_id} has conflicting names at the same changeTime"
                )
    return [latest_by_company[key] for key in sorted(latest_by_company)]


def _odds_records_by_match(
    rows: list[dict[str, Any]], fixtures: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    targets = {fixture["provider_match_id"]: fixture for fixture in fixtures}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        match_id = str(row.get("matchId", "")).strip()
        if match_id not in targets:
            continue
        if match_id in found:
            raise NationsLeagueIsportsError(
                f"bulk odds response contains duplicate target matchId {match_id}"
            )
        expected = targets[match_id]
        required_identity = {"matchTime", "homeName", "awayName", "leagueName"}
        if not required_identity.issubset(row):
            raise NationsLeagueIsportsError(
                f"bulk odds identity is incomplete for matchId {match_id}"
            )
        odds_kickoff = _parse_timestamp(row["matchTime"], "odds matchTime")
        if odds_kickoff != expected["kickoff"]:
            raise NationsLeagueIsportsError(
                f"odds kickoff disagrees with schedule for matchId {match_id}"
            )
        if "leagueId" in row and str(row["leagueId"]) != str(PROVIDER_LEAGUE_ID):
            raise NationsLeagueIsportsError(
                f"odds provider league ID disagrees with schedule for matchId {match_id}"
            )
        for field, expected_name in (
            ("homeName", expected["home_team"]),
            ("awayName", expected["away_team"]),
        ):
            if canonical_name(str(row[field])) != canonical_name(expected_name):
                raise NationsLeagueIsportsError(
                    f"odds {field} disagrees with schedule for matchId {match_id}"
                )
        actual = " ".join(str(row["leagueName"]).casefold().split())
        if actual != COMPETITION.casefold():
            raise NationsLeagueIsportsError(
                f"odds competition disagrees with schedule for matchId {match_id}"
            )
        found[match_id] = row
    missing = sorted(set(targets) - set(found))
    if missing:
        raise NationsLeagueIsportsError(
            "bulk odds coverage gap: missing matchId " + ", ".join(missing)
        )
    return found


def _market_for_fixture(record: Mapping[str, Any], *, match_id: str) -> dict[str, Any]:
    quotes = _bookmaker_quotes(record)
    if not quotes:
        raise NationsLeagueIsportsError(
            f"bulk odds coverage gap: no valid 1X2 bookmaker quote for matchId {match_id}"
        )
    triples = [quote["_triple"] for quote in quotes]
    aggregate = tuple(
        statistics.median(triple[index] for triple in triples) for index in range(3)
    )
    public_quotes = [
        {
            "company_id": quote["company_id"],
            "company_name": quote["company_name"],
            "change_time": quote["change_time"],
            "odds_decimal": quote["odds_decimal"],
        }
        for quote in quotes
    ]
    return {
        "bookmaker": "iSports European odds component-wise median",
        "aggregation": "latest_valid_quote_per_bookmaker_then_componentwise_median",
        "bookmaker_count": len(public_quotes),
        "bookmakers": public_quotes,
        "home": aggregate[0],
        "draw": aggregate[1],
        "away": aggregate[2],
    }


def _prediction_event(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(fixture["provider_match_id"]),
        "commence_time": fixture["kickoff"].isoformat(),
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
    }


def _validate_operation_manifest(
    operations: list[dict[str, Any]], *, captured_at: datetime
) -> None:
    exact_keys = {
        "ordinal",
        "operation",
        "method",
        "path",
        "query",
        "status_code",
        "started_at",
        "completed_at",
        "response_sha256",
    }
    if len(operations) != 2:
        raise NationsLeagueIsportsError(
            "provider operation manifest must contain exactly two operations"
        )
    captured_utc = captured_at.astimezone(timezone.utc)
    for index, actual in enumerate(operations):
        ordinal = index + 1
        expected_operation = "schedule" if ordinal == 1 else "odds"
        expected_path = SCHEDULE_PATH if ordinal == 1 else EUROPEAN_ODDS_PATH
        if not isinstance(actual, Mapping) or set(actual) != exact_keys:
            raise NationsLeagueIsportsError(
                "provider operation manifest fields are not allowlisted"
            )
        query = actual["query"]
        if (
            actual["ordinal"] != ordinal
            or actual["operation"] != expected_operation
            or actual["method"] != "GET"
            or actual["path"] != expected_path
            or not isinstance(query, Mapping)
            or isinstance(actual["status_code"], bool)
            or not 200 <= int(actual["status_code"]) < 300
            or not re.fullmatch(r"[0-9a-f]{64}", str(actual["response_sha256"]))
        ):
            raise NationsLeagueIsportsError(
                "provider operation manifest identity is invalid"
            )
        if ordinal == 1:
            if dict(query) != {"leagueId": str(PROVIDER_LEAGUE_ID)}:
                raise NationsLeagueIsportsError(
                    "provider schedule query differs from the frozen contract"
                )
        elif (
            set(query) != {"day"}
            or isinstance(query.get("day"), bool)
            or not isinstance(query.get("day"), int)
            or query["day"] < 1
        ):
            raise NationsLeagueIsportsError(
                "provider odds operation must use one numeric day filter"
            )
        started = _parse_timestamp(actual["started_at"], "operation started_at")
        completed = _parse_timestamp(actual["completed_at"], "operation completed_at")
        if started > completed or completed > captured_utc:
            raise NationsLeagueIsportsError(
                "provider operation timestamp exceeds artifact capture"
            )


def _build_artifact(
    *,
    schedule_fixtures: list[dict[str, Any]],
    excluded_fixtures: list[dict[str, Any]],
    odds_by_match: Mapping[str, dict[str, Any]],
    operation_manifest: list[dict[str, Any]],
    provider_rate_evidence: list[dict[str, Any]],
    snapshot: FrozenSnapshot,
    historical: Any,
    history_provenance: dict[str, Any],
    source_sha: str,
    captured_at: datetime,
) -> dict[str, Any]:
    captured_utc = captured_at.astimezone(timezone.utc)
    results: list[dict[str, Any]] = []
    for fixture in schedule_fixtures:
        match_id = fixture["provider_match_id"]
        market = _market_for_fixture(odds_by_match[match_id], match_id=match_id)
        raw_event = _prediction_event(fixture)
        try:
            prediction = predict_fixture(
                event=raw_event,
                odds=market,
                snapshot=snapshot,
                historical=historical,
                captured_at=captured_utc,
                neutral=fixture["neutral"],
            )
        except NationsLeagueShadowError as exc:
            raise NationsLeagueIsportsError(
                f"model coverage failed for matchId {match_id}: {exc}"
            ) from None
        prediction.pop("provider_event_id", None)
        prediction["provider_match_id"] = match_id
        prediction["provider_league_id"] = PROVIDER_LEAGUE_ID
        prediction["schedule_status"] = fixture["status"]
        prediction["neutral"] = fixture["neutral"]
        prediction["tournament"] = COMPETITION
        prediction["market"]["bookmaker"] = market["bookmaker"]
        prediction["market"]["aggregation"] = market["aggregation"]
        prediction["market"]["bookmaker_count"] = market["bookmaker_count"]
        prediction["market"]["bookmakers"] = market["bookmakers"]
        results.append(prediction)

    if len(results) != len(schedule_fixtures):
        raise NationsLeagueIsportsError("fixture/model coverage is incomplete")
    _validate_operation_manifest(operation_manifest, captured_at=captured_utc)
    run_id = (
        f"unl-shadow-{captured_utc.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    )
    artifact: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "run_id": run_id,
        "capture_status": "complete",
        "source_sha": source_sha,
        "provider": PROVIDER,
        "provider_league_id": PROVIDER_LEAGUE_ID,
        "competition": COMPETITION,
        "sport_key": "soccer_uefa_nations_league",
        "captured_at": captured_utc.isoformat(),
        "model_snapshot": snapshot.provenance(),
        "input_data": {"international_results": history_provenance},
        "provider_operation_manifest": operation_manifest,
        "provider_rate_evidence": provider_rate_evidence,
        "request_count": 2,
        "retry_count": 0,
        "coverage": {
            "eligible_schedule_fixtures": len(schedule_fixtures),
            "valid_odds_fixtures": len(odds_by_match),
            "model_fixtures": len(results),
            "complete": len(schedule_fixtures) == len(odds_by_match) == len(results),
            "eligible_match_ids": [
                row["provider_match_id"] for row in schedule_fixtures
            ],
        },
        "provider_event_count": len(schedule_fixtures) + len(excluded_fixtures),
        "fixture_count": len(schedule_fixtures),
        "covered_fixture_count": len(results),
        "skipped_fixtures": [],
        "excluded_schedule_fixtures": excluded_fixtures,
        "fixtures": results,
        "evidence_status": "WEAK_EVIDENCE_SHADOW_ONLY",
        "shadow": True,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
        "scheduler_mutation": False,
    }
    artifact["artifact_digest"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _window_days(now: datetime) -> int:
    seconds = (ACTIVE_END_EXCLUSIVE - now.astimezone(timezone.utc)).total_seconds()
    return max(1, math.ceil(seconds / 86400))


def run_isports_shadow_scan(
    *,
    api_key: str | None = None,
    transport: Callable[..., Any] | None = None,
    snapshot_dir: Path = SNAPSHOT_DIR,
    history_path: Path = DATA_CACHE / "international_results.pkl",
    source_root: Path | None = None,
    output_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    """Make one schedule call plus one bulk 1X2 call; never retry or fall back."""
    preflight_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        snapshot = load_frozen_snapshot(snapshot_dir)
        historical, history_provenance = load_cached_history(
            history_path, now=preflight_at, max_age=MAX_HISTORY_AGE
        )
        root = Path(source_root) if source_root else Path(__file__).resolve().parents[2]
        source_sha = current_source_sha(root)
    except NationsLeagueShadowError as exc:
        raise NationsLeagueIsportsError(
            f"offline shadow preflight failed: {exc}", request_count=0
        ) from None
    try:
        key = api_key if api_key is not None else load_isports_api_key()
    except IsportsApiError as exc:
        raise NationsLeagueIsportsError(
            str(exc), request_count=exc.request_count, http_statuses=[]
        ) from None
    if not key:
        raise NationsLeagueIsportsError("iSports credential is unavailable")

    operations: list[dict[str, Any]] = []
    rate_evidence: list[dict[str, Any]] = []
    try:
        schedule_operation = request_once(
            api_key=key,
            operation_kind="schedule",
            ordinal=1,
            endpoint_path=SCHEDULE_PATH,
            query={"leagueId": str(PROVIDER_LEAGUE_ID)},
            transport=transport,
        )
    except IsportsApiError as exc:
        statuses = [exc.http_status] if exc.http_status is not None else []
        raise NationsLeagueIsportsError(
            str(exc), request_count=exc.request_count, http_statuses=statuses
        ) from None
    operations.append(schedule_operation.manifest_entry)
    try:
        if schedule_operation.safe_rate_headers:
            rate_evidence.append(
                {"ordinal": 1, "headers": schedule_operation.safe_rate_headers}
            )
        schedule_fixtures, excluded_fixtures = _schedule_fixtures(
            schedule_operation.payload, captured_at=preflight_at
        )
    except NationsLeagueIsportsError as exc:
        raise NationsLeagueIsportsError(
            str(exc),
            request_count=1,
            http_statuses=[schedule_operation.manifest_entry["status_code"]],
        ) from None
    try:
        odds_operation = request_once(
            api_key=key,
            operation_kind="odds",
            ordinal=2,
            endpoint_path=EUROPEAN_ODDS_PATH,
            query={"day": _window_days(preflight_at)},
            transport=transport,
        )
    except IsportsApiError as exc:
        statuses = [schedule_operation.manifest_entry["status_code"]]
        if exc.http_status is not None:
            statuses.append(exc.http_status)
        raise NationsLeagueIsportsError(
            str(exc), request_count=exc.request_count, http_statuses=statuses
        ) from None
    operations.append(odds_operation.manifest_entry)
    if odds_operation.safe_rate_headers:
        rate_evidence.append(
            {"ordinal": 2, "headers": odds_operation.safe_rate_headers}
        )

    try:
        odds_by_match = _odds_records_by_match(
            odds_operation.payload, schedule_fixtures
        )
    except NationsLeagueIsportsError as exc:
        raise NationsLeagueIsportsError(
            str(exc),
            request_count=2,
            http_statuses=[
                schedule_operation.manifest_entry["status_code"],
                odds_operation.manifest_entry["status_code"],
            ],
        ) from None
    captured_at = datetime.now(timezone.utc)
    try:
        artifact = _build_artifact(
            schedule_fixtures=schedule_fixtures,
            excluded_fixtures=excluded_fixtures,
            odds_by_match=odds_by_match,
            operation_manifest=operations,
            provider_rate_evidence=rate_evidence,
            snapshot=snapshot,
            historical=historical,
            history_provenance=history_provenance,
            source_sha=source_sha,
            captured_at=captured_at,
        )
    except NationsLeagueIsportsError as exc:
        raise NationsLeagueIsportsError(
            str(exc),
            request_count=2,
            http_statuses=[
                schedule_operation.manifest_entry["status_code"],
                odds_operation.manifest_entry["status_code"],
            ],
        ) from None
    if output_path is None:
        from src.runtime.paths import runtime_state_path

        try:
            output_path = runtime_state_path(
                f"data/nations-league-shadow/{artifact['run_id']}.json",
                require_external=True,
            )
        except (OSError, RuntimeError):
            raise NationsLeagueIsportsError(
                "governed runtime artifact path is unavailable",
                request_count=2,
                http_statuses=[
                    schedule_operation.manifest_entry["status_code"],
                    odds_operation.manifest_entry["status_code"],
                ],
            ) from None
    try:
        path = write_immutable_artifact(output_path, artifact)
    except OSError:
        raise NationsLeagueIsportsError(
            "immutable shadow artifact could not be persisted",
            request_count=2,
            http_statuses=[
                schedule_operation.manifest_entry["status_code"],
                odds_operation.manifest_entry["status_code"],
            ],
        ) from None
    return artifact, path
