"""Privacy and interaction tests for the anonymous PWA analytics boundary."""

import base64
import functools
import json
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"


@pytest.fixture(scope="module")
def server_url():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DOCS_DIR))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _payload() -> dict:
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "football": [],
        "tennis": [],
        "schedule": [],
        "all_odds": {},
        "model_tips": {},
        "model_evals": {},
        "health": {},
        "build_info": {},
        "wm_results": [],
        "odds_history": {},
    }


def _open_page(page: Page, server_url: str, payload: dict | None = None) -> None:
    body = base64.b64encode(json.dumps(payload or _payload()).encode()).decode()
    page.add_init_script(f"""
        window.__SB_ANALYTICS_TEST__ = true;
        localStorage.setItem('sb_seen_onboarding', '1');
        const body = atob('{body}');
        const originalFetch = window.fetch.bind(window);
        window.fetch = (url, options) => String(url).includes('signals.json')
          ? Promise.resolve(new Response(body, {{status: 200, headers: {{'Content-Type': 'application/json'}}}}))
          : originalFetch(url, options);
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('disabled'));
    """)
    page.goto(server_url, wait_until="domcontentloaded")
    page.wait_for_timeout(500)


def _events(page: Page, name: str | None = None) -> list[dict]:
    events = page.evaluate("window.__sbAnalyticsEvents || []")
    return [event for event in events if name is None or event["event"] == name]


def test_app_load_and_navigation_emit_safe_events(page: Page, server_url: str) -> None:
    _open_page(page, server_url)
    expect(page.locator("nav.bottom-nav")).to_be_visible()
    assert len(_events(page, "pwa_opened")) == 1
    page.locator("[data-view='football']").click()
    tab_events = _events(page, "tab_opened")
    assert len(tab_events) == 1
    assert tab_events[0]["properties"] == {"tab": "football", "previous_tab": "home"}
    page.locator("[data-view='football']").click()
    assert len(_events(page, "tab_opened")) == 1


def test_match_opening_emits_canonical_events_without_private_fields(
    page: Page, server_url: str
) -> None:
    _open_page(page, server_url)
    page.evaluate("""
        window._openMatchDetail({home:'Alpha', away:'Beta', sport:'football', competition:'Nations League',
          fixture_key:'alpha_beta_1', p_home:48, p_draw:27, p_away:25, signal_status:'SHADOW',
          confidence:'MEDIUM', bankroll:100, stake_eur:5, access_token:'Bearer secret'});
    """)
    assert len(_events(page, "match_opened")) == 1
    assert len(_events(page, "prediction_viewed")) == 1
    assert len(_events(page, "signal_opened")) == 1
    assert len(_events(page, "nl_match_opened")) == 1
    for event in _events(page):
        serialized = json.dumps(event)
        assert (
            "bankroll" not in serialized
            and "stake" not in serialized
            and "secret" not in serialized
        )
        assert "access_token" not in serialized


def test_diagnostics_event_requires_explicit_expansion_and_deduplicates(
    page: Page, server_url: str
) -> None:
    _open_page(page, server_url)
    page.evaluate("""
      document.body.insertAdjacentHTML('beforeend', `<details data-analytics="model-diagnostics" data-fixture-id="fixture-1"
        data-sport="football" data-competition="Nations League" data-model-version="test-model" data-lifecycle-stage="INITIAL">
        <summary>Diagnostics</summary><div>Details</div></details>`);
    """)
    page.locator("summary", has_text="Diagnostics").evaluate("node => node.click()")
    page.wait_for_timeout(50)
    assert page.locator('details[data-analytics="model-diagnostics"]').evaluate(
        "node => node.open"
    )
    assert len(_events(page, "model_diagnostics_opened")) == 1
    page.locator("summary", has_text="Diagnostics").click(force=True)
    page.locator("summary", has_text="Diagnostics").click(force=True)
    page.wait_for_timeout(50)
    assert len(_events(page, "model_diagnostics_opened")) == 1


def test_analytics_failure_is_fail_open_and_properties_are_allowlisted(
    page: Page, server_url: str
) -> None:
    _open_page(page, server_url)
    result = page.evaluate("""
      () => { window.sbAnalytics.capture('frontend_error', {
        error_type:'test', message:'Bearer secret-token', email:'person@example.com', token:'private',
        bankroll:100, arbitrary:'not allowed'}); return window.__sbAnalyticsEvents.at(-1); }
    """)
    assert result["event"] == "frontend_error"
    assert set(result["properties"]) <= {
        "error_type",
        "message",
        "source_file",
        "app_version",
        "active_view",
    }
    assert "secret" not in json.dumps(
        result
    ) and "person@example.com" not in json.dumps(result)
