"""Unit tests for scripts._http_retry (Roadmap F2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from scripts._http_retry import retry_get, retry_request


class _FakeSession:
    def __init__(self, side_effects):
        self.calls = 0
        self._side_effects = list(side_effects)

    def request(self, method, url, **kwargs):
        idx = self.calls
        self.calls += 1
        eff = self._side_effects[idx]
        if isinstance(eff, Exception):
            raise eff
        return eff


def _resp(status: int) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    return r


def test_success_first_try():
    sleeps: list[float] = []
    sess = _FakeSession([_resp(200)])
    resp = retry_request(
        "GET", "http://x", session=sess, sleep=sleeps.append
    )
    assert resp.status_code == 200
    assert sess.calls == 1
    assert sleeps == []


def test_retry_on_request_exception_then_success():
    sleeps: list[float] = []
    sess = _FakeSession([
        requests.ConnectionError("DNS fail"),
        requests.ConnectionError("DNS fail again"),
        _resp(200),
    ])
    resp = retry_request(
        "GET", "http://x",
        session=sess,
        backoff=(1, 2, 3),
        sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sess.calls == 3
    assert sleeps == [1, 2]


def test_raises_after_exhaustion():
    sleeps: list[float] = []
    sess = _FakeSession([
        requests.ConnectionError("a"),
        requests.ConnectionError("b"),
        requests.ConnectionError("c"),
    ])
    with pytest.raises(requests.ConnectionError):
        retry_request(
            "GET", "http://x",
            session=sess,
            backoff=(0, 0, 0),
            sleep=sleeps.append,
        )
    assert sess.calls == 3
    # 2 backoffs between attempts; no sleep after last
    assert sleeps == [0, 0]


def test_retry_on_status_then_success():
    sleeps: list[float] = []
    sess = _FakeSession([_resp(503), _resp(200)])
    resp = retry_request(
        "GET", "http://x",
        session=sess,
        backoff=(0,),
        retry_on_status={503},
        sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sess.calls == 2


def test_non_retryable_status_returned():
    sess = _FakeSession([_resp(404)])
    resp = retry_request(
        "GET", "http://x",
        session=sess,
        retry_on_status={503},
        sleep=lambda _s: None,
    )
    assert resp.status_code == 404
    assert sess.calls == 1


def test_retry_get_shortcut():
    sess = _FakeSession([_resp(200)])
    resp = retry_get("http://x", session=sess, sleep=lambda _s: None)
    assert resp.status_code == 200


def test_backoff_clamped_to_last_value():
    sleeps: list[float] = []
    sess = _FakeSession([
        requests.Timeout("t1"),
        requests.Timeout("t2"),
        requests.Timeout("t3"),
        _resp(200),
    ])
    resp = retry_request(
        "GET", "http://x",
        session=sess,
        retries=4,
        backoff=(1, 5),
        sleep=sleeps.append,
    )
    assert resp.status_code == 200
    # backoffs after attempts 0,1,2 → 1, 5, 5 (clamped)
    assert sleeps == [1, 5, 5]
