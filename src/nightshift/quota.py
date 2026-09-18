"""Conservative detection and scheduling helpers for AI usage exhaustion."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from .models import isoformat, parse_timestamp, utc_now

DEFAULT_QUOTA_BACKOFF_SECONDS = 15 * 60
MAX_QUOTA_BACKOFF_SECONDS = 6 * 60 * 60
MAX_RELIABLE_RESET_HORIZON_SECONDS = 30 * 24 * 60 * 60

_QUOTA_MARKERS = (
    re.compile(
        r"\b(?:usage|account|organization|org|project)\s+(?:quota|limit)"
        r"\s+(?:has\s+been\s+|is\s+|are\s+)?(?:reached|exceeded|exhausted|depleted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:credits?|credit\s+balance)\s+(?:has\s+been\s+|is\s+|are\s+)?"
        r"(?:reached|exceeded|exhausted|depleted|empty|zero|insufficient)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:usage\s+quota|quota\s+for\s+(?:the\s+)?(?:account|organization|org|project))\s+"
        r"(?:has\s+been\s+|is\s+|are\s+)?(?:reached|exceeded|exhausted|depleted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:out\s+of|no\s+remaining)\s+(?:credits?|quota|usage)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:you(?:\s+have)?['’]ve|you\s+have)\s+hit\s+"
        r"(?:your\s+)?(?:usage\s+)?limit\b",
        re.IGNORECASE,
    ),
)
_RESET_TIMESTAMP = re.compile(
    r"\b(?:reset|resets|available|try\s+again\s+after)\b[^\n]{0,160}?"
    r"\b(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2}))\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password|secret|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class QuotaDetection:
    """Sanitized, explicit evidence that the provider capped AI usage."""

    reason: str
    marker: str
    reset_at: str | None = None
    provider: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_quota_text(value: str | None, *, limit: int = 400) -> str:
    """Redact likely credentials and URLs before quota text is persisted."""

    if not value:
        return "quota exhaustion reported by the AI worker"
    cleaned = _SENSITIVE.sub(r"\1=[REDACTED]", value)
    cleaned = _URL.sub("[URL]", cleaned)
    return " ".join(cleaned.replace("\x00", "").split())[:limit]


def _reset_at_from_text(text: str, *, now: datetime) -> str | None:
    for match in _RESET_TIMESTAMP.finditer(text):
        candidate = match.group(1)
        try:
            parsed = parse_timestamp(candidate)
        except (TypeError, ValueError):
            continue
        seconds = (parsed - now).total_seconds()
        if 0 < seconds <= MAX_RELIABLE_RESET_HORIZON_SECONDS:
            return isoformat(parsed)
    return None


def normalize_reset_at(
    value: Any, *, now: datetime | None = None
) -> str | None:
    """Accept only a future, bounded, timezone-aware reset timestamp."""

    if not isinstance(value, str) or not value.strip():
        return None
    current = now or utc_now()
    try:
        parsed = parse_timestamp(value.strip())
    except (TypeError, ValueError):
        return None
    seconds = (parsed - current).total_seconds()
    if not 0 < seconds <= MAX_RELIABLE_RESET_HORIZON_SECONDS:
        return None
    return isoformat(parsed)


def classify_quota_exhaustion(
    stdout: str | bytes,
    stderr: str | bytes,
    *,
    provider: str | None = None,
    now: datetime | None = None,
) -> QuotaDetection | None:
    """Classify only explicit usage/credit/quota exhaustion markers.

    Generic rate-limit, test, provider, and code errors intentionally do not
    match.  Adapters that know they are invoking Codex or Claude may pass the
    provider name, but the marker must still identify usage, credits, quota, or
    a provider usage limit.
    """

    def text(value: str | bytes) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    combined = f"{text(stdout)}\n{text(stderr)}"
    match = next(
        (candidate for pattern in _QUOTA_MARKERS if (candidate := pattern.search(combined))),
        None,
    )
    if match is None:
        return None
    current = now or utc_now()
    marker = sanitize_quota_text(match.group(0), limit=180)
    reset_at = _reset_at_from_text(combined, now=current)
    provider_name = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    return QuotaDetection(
        reason=f"AI usage quota exhausted{f' ({provider_name})' if provider_name else ''}: {marker}",
        marker=marker,
        reset_at=reset_at,
        provider=provider_name,
    )


def next_quota_eligible_at(
    *,
    current: datetime,
    prior_pause_count: int,
    reset_at: Any = None,
) -> str:
    """Return a reliable reset time or a bounded exponential backoff."""

    normalized = normalize_reset_at(reset_at, now=current)
    if normalized is not None:
        return normalized
    count = max(0, min(prior_pause_count, 5))
    seconds = min(
        MAX_QUOTA_BACKOFF_SECONDS,
        DEFAULT_QUOTA_BACKOFF_SECONDS * (2**count),
    )
    return isoformat(current + timedelta(seconds=seconds))


def is_quota_failure_class(value: Any) -> bool:
    """Recognize only explicit canonical quota result classes."""

    if not isinstance(value, str):
        return False
    return value.strip().upper() in {
        "QUOTA_EXHAUSTED",
        "AI_QUOTA_EXHAUSTED",
        "USAGE_LIMIT",
        "CREDIT_EXHAUSTED",
        "ACCOUNT_QUOTA_EXHAUSTED",
        "PROVIDER_RESET_REQUIRED",
    }


__all__ = [
    "DEFAULT_QUOTA_BACKOFF_SECONDS",
    "MAX_QUOTA_BACKOFF_SECONDS",
    "QuotaDetection",
    "classify_quota_exhaustion",
    "is_quota_failure_class",
    "next_quota_eligible_at",
    "normalize_reset_at",
    "sanitize_quota_text",
]
