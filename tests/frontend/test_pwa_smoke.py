"""Playwright smoke tests: PWA loads, bet-modal opens, stale-banner appears."""
import base64
import json
import functools
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"

_NOW = datetime.now(timezone.utc)

_BASE: dict = {
    "updated": _NOW.isoformat(),
    "football": [],
    "tennis": [],
    "open_bets": [],
    "settled_bets": [],
    "bankroll_state": {},
    "schedule": [],
    "all_odds": {},
    "model_tips": {},
    "health": {},
    "build_info": {},
    "wm_results": [],
    "wm_stats": {},
    "portfolio": {},
    "top_elo": [],
    "history": {},
    "odds_history": {},
}

_FRESH: dict = {
    **_BASE,
    "football": [{
        "sport": "football",
        "match": "Alpha vs Beta",
        "market": "1x2_home",
        "odds": 2.10,
        "model_prob": 62.0,
        "fair_prob": 62.0,
        "ev_pct": 30.2,
        "stake_eur": 5.0,
        "stake_pct": 5.0,
        "confidence": "HIGH",
        "n_models_agree": 2,
        "kickoff": (_NOW + timedelta(hours=3)).isoformat(),
    }],
}

_STALE: dict = {
    **_BASE,
    "updated": (_NOW - timedelta(hours=3)).isoformat(),
}


@pytest.fixture(scope="module")
def server_url():
    """Serve docs/ via local HTTP server on a free port."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DOCS_DIR))
    srv = HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _inject_signals(page: Page, payload: dict) -> None:
    """Override window.fetch before page load so signals.json returns mock data.

    Also disables the service worker: its activate handler calls client.navigate()
    which forces a page reload that would cause Playwright to wait for an
    unexpected navigation indefinitely.
    Uses base64 to avoid JS escaping issues with arbitrary JSON content.
    """
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    page.add_init_script(f"""
        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register = () =>
                Promise.reject(new Error('SW disabled in tests'));
        }}
        localStorage.setItem('sb_seen_onboarding', '1');
        const _mockBody = atob('{b64}');
        const _origFetch = window.fetch.bind(window);
        window.fetch = function(url, opts) {{
            if (String(url).includes('signals.json')) {{
                return Promise.resolve(new Response(_mockBody, {{
                    status: 200,
                    headers: {{'Content-Type': 'application/json'}}
                }}));
            }}
            return _origFetch(url, opts);
        }};
    """)


# ── Test 1: PWA lädt ──────────────────────────────────────────────────────────

def test_pwa_loads(page: Page, server_url: str) -> None:
    """Bottom-nav with all 6 tabs is visible after page load."""
    _inject_signals(page, _FRESH)
    page.goto(server_url, wait_until="domcontentloaded")

    nav = page.locator("nav.bottom-nav")
    expect(nav).to_be_visible(timeout=10_000)
    expect(nav.locator("[role='tab']")).to_have_count(6)


# ── Test 2: Bet-Modal öffnet ──────────────────────────────────────────────────

def test_bet_modal_opens_and_closes(page: Page, server_url: str) -> None:
    """Clicking 'Wette platzieren' opens bet modal; Cancel closes it."""
    _inject_signals(page, _FRESH)
    page.goto(server_url, wait_until="domcontentloaded")

    # Signal cards with bet buttons live in the football tab, not the home tab
    page.locator("[data-view='football']").click()

    place_btn = page.locator(".place-bet-btn").first
    expect(place_btn).to_be_visible(timeout=10_000)
    place_btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)  # .show → display:flex

    page.locator("#bet-modal-cancel").click()
    expect(modal).not_to_be_visible(timeout=3_000)


# ── Test 3: Stale-Banner erscheint ───────────────────────────────────────────

def test_stale_banner_shown_for_old_data(page: Page, server_url: str) -> None:
    """Stale-banner visible when signals.json.updated is >90 min ago."""
    _inject_signals(page, _STALE)
    page.goto(server_url, wait_until="domcontentloaded")

    banner = page.locator("#stale-banner")
    expect(banner).to_be_visible(timeout=10_000)
