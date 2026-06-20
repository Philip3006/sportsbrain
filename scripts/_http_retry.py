"""Centralized HTTP retry helper for transient DNS / network failures.

Background (Roadmap F2): wiederkehrendes DNS-Failure-Pattern bei Workers,
TheOddsAPI, ESPN, Sofascore. Statt jeden Caller eigene Retry-Schleife
basteln zu lassen, gibt es hier *einen* Helper.

Default-Policy:
    retries=3, backoff=(5, 15, 30) Sekunden

Retry-Auslöser:
    - requests.RequestException (DNS, Connection, Timeout, SSL, …)
    - optional HTTP-Status in `retry_on_status` (z. B. {502, 503, 504})

NICHT retried:
    - 4xx-Statuscodes außer in retry_on_status (Caller entscheidet)

Usage:
    from scripts._http_retry import retry_request
    resp = retry_request("GET", url, timeout=15)
    resp.raise_for_status()
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence

import requests

DEFAULT_BACKOFF: tuple[int, ...] = (5, 15, 30)


def retry_request(
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
    retry_on_status: set[int] | None = None,
    log_prefix: str = "[http_retry]",
    sleep: Callable[[float], None] = time.sleep,
    session: requests.Session | None = None,
    **kwargs,
) -> requests.Response:
    """Issue an HTTP request with automatic retry on transient failures.

    Raises the last `requests.RequestException` if all attempts fail. If the
    final attempt returns an HTTP status in `retry_on_status`, that response
    is returned anyway (caller can decide what to do).
    """
    if retries < 1:
        raise ValueError("retries must be >= 1")
    last_exc: Exception | None = None
    do = (session.request if session is not None else requests.request)
    for attempt in range(retries):
        try:
            resp = do(method, url, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                print(
                    f"{log_prefix} {method} {url} failed after {retries} attempts: {e}",
                    file=sys.stderr,
                )
                raise
            delay = backoff[min(attempt, len(backoff) - 1)]
            print(
                f"{log_prefix} {method} {url} attempt {attempt + 1}/{retries} "
                f"failed ({type(e).__name__}); retry in {delay}s",
                file=sys.stderr,
            )
            sleep(delay)
            continue

        if retry_on_status and resp.status_code in retry_on_status and attempt < retries - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            print(
                f"{log_prefix} {method} {url} status {resp.status_code}; "
                f"retry {attempt + 1}/{retries} in {delay}s",
                file=sys.stderr,
            )
            sleep(delay)
            continue
        return resp

    # Should not reach here, but mypy-safe
    assert last_exc is not None
    raise last_exc


def retry_get(url: str, **kwargs) -> requests.Response:
    return retry_request("GET", url, **kwargs)


def retry_post(url: str, **kwargs) -> requests.Response:
    return retry_request("POST", url, **kwargs)
