"""Narrow, no-retry iSports transport for the Nations League shadow run."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

ISPORTS_BASE_URL = "https://api.isportsapi.com"
SCHEDULE_PATH = "/sport/football/schedule/basic"
EUROPEAN_ODDS_PATH = "/sport/football/odds/european/all"
REQUEST_TIMEOUT_SECONDS = 60
SAFE_RATE_HEADERS = frozenset(
    {
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-rate-limit-limit",
        "x-rate-limit-remaining",
        "x-requests-used",
        "x-requests-remaining",
    }
)


class IsportsApiError(RuntimeError):
    """Sanitized failure from one of the two allowed iSports operations."""

    def __init__(self, message: str, *, request_count: int, http_status: int | None):
        super().__init__(message)
        self.request_count = request_count
        self.http_status = http_status


@dataclass(frozen=True)
class IsportsOperation:
    payload: list[dict[str, Any]]
    manifest_entry: dict[str, Any]
    safe_rate_headers: dict[str, str]


def load_isports_api_key() -> str:
    """Load the protected API key without exposing or persisting it."""
    load_dotenv()
    key = os.getenv("ISPORTS_API_KEY", "").strip()
    if not key:
        raise IsportsApiError(
            "iSports credential is unavailable", request_count=0, http_status=None
        )
    return key


def _safe_rate_headers(headers: Mapping[str, object]) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).casefold()
        text = str(value).strip()
        if key in SAFE_RATE_HEADERS and text.isascii() and text.isdecimal():
            evidence[key] = text
    return evidence


def _response_digest(response: Any, payload: Any, *, ordinal: int) -> str:
    body = getattr(response, "content", None)
    if isinstance(body, bytes):
        material = body
    else:
        try:
            material = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise IsportsApiError(
                "iSports response provenance could not be hashed",
                request_count=ordinal,
                http_status=getattr(response, "status_code", None),
            ) from None
    return hashlib.sha256(material).hexdigest()


def _decode_envelope(response: Any, *, request_count: int) -> list[dict[str, Any]]:
    try:
        envelope = response.json()
    except (ValueError, requests.RequestException):
        raise IsportsApiError(
            "iSports returned malformed JSON",
            request_count=request_count,
            http_status=int(response.status_code),
        ) from None
    if not isinstance(envelope, Mapping):
        raise IsportsApiError(
            "iSports response envelope is not an object",
            request_count=request_count,
            http_status=int(response.status_code),
        )
    code = envelope.get("code")
    if str(code) != "0":
        raise IsportsApiError(
            "iSports response code was not successful",
            request_count=request_count,
            http_status=int(response.status_code),
        )
    data = envelope.get("data")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise IsportsApiError(
            "iSports response data is not a list of objects",
            request_count=request_count,
            http_status=int(response.status_code),
        )
    return data


def request_once(
    *,
    api_key: str,
    operation_kind: str,
    ordinal: int,
    endpoint_path: str,
    query: Mapping[str, str | int],
    transport: Callable[..., Any] | None = None,
) -> IsportsOperation:
    """Issue exactly one authenticated GET, with redirects and retries disabled."""
    safe_query = {str(key): value for key, value in query.items()}
    started = datetime.now(timezone.utc)
    requester = transport or requests.get
    try:
        response = requester(
            f"{ISPORTS_BASE_URL}{endpoint_path}",
            params={
                **{key: str(value) for key, value in safe_query.items()},
                "api_key": api_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise IsportsApiError(
            f"iSports {operation_kind} request failed without retry",
            request_count=ordinal,
            http_status=None,
        ) from None
    except Exception:  # noqa: BLE001 - transport exception text may contain the API key URL.
        # The transport may include a fully rendered URL (and its API key) in
        # exception text. Never propagate that text to logs or artifacts.
        raise IsportsApiError(
            f"iSports {operation_kind} transport failed without retry",
            request_count=ordinal,
            http_status=None,
        ) from None

    completed = datetime.now(timezone.utc)
    try:
        status = int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        raise IsportsApiError(
            f"iSports {operation_kind} returned an invalid HTTP status",
            request_count=ordinal,
            http_status=None,
        ) from None
    if not 200 <= status < 300:
        raise IsportsApiError(
            f"iSports {operation_kind} returned HTTP {status}",
            request_count=ordinal,
            http_status=status,
        )

    payload = _decode_envelope(response, request_count=ordinal)
    try:
        rate_headers = _safe_rate_headers(response.headers)
    except (AttributeError, TypeError, ValueError):
        raise IsportsApiError(
            f"iSports {operation_kind} response headers were malformed",
            request_count=ordinal,
            http_status=status,
        ) from None
    entry = {
        "ordinal": int(ordinal),
        "operation": operation_kind,
        "method": "GET",
        "path": endpoint_path,
        "query": safe_query,
        "status_code": status,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "response_sha256": _response_digest(response, payload, ordinal=ordinal),
    }
    return IsportsOperation(
        payload=payload,
        manifest_entry=entry,
        safe_rate_headers=rate_headers,
    )
