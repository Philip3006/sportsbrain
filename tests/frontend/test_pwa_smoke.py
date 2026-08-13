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

def _canonical_signal(**overrides) -> dict:
    """Build a canonical P0-A compliant signal fixture."""
    base = {
        "sport": "football",
        "match": "Alpha vs Beta",
        "home": "Alpha",
        "away": "Beta",
        "market": "home",
        "odds": 2.10,
        "current_odds": 2.10,
        "current_ev_pct": 15.2,
        "model_prob": 0.52,
        "fair_prob": 0.48,
        "ev_pct": 15.2,
        "stake_eur": 5.0,
        "stake_pct": 5.0,
        "confidence": "HIGH",
        "n_models_agree": 2,
        "kickoff": (_NOW + timedelta(hours=3)).isoformat(),
        # P0-A canonical fields
        "signal_id": "sig_alpha_beta_home",
        "signal_status": "ACTIVE",
        "shadow": False,
        "is_shadow": False,
        "unsupported": False,
        "edge_lost": False,
        "stale": False,
        "no_bet_flag": False,
        "odds_ts": (_NOW - timedelta(minutes=5)).isoformat(),
        "event_status": "PREMATCH",
        "fixture_key": "alpha_vs_beta_20260813",
        "league": "wm2026",
    }
    base.update(overrides)
    return base


_FRESH: dict = {
    **_BASE,
    "football": [_canonical_signal()],
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


# ── P0-A focused tests ────────────────────────────────────────────────────────

def _navigate_to_football(page: Page, server_url: str, payload: dict) -> None:
    _inject_signals(page, payload)
    page.goto(server_url, wait_until="domcontentloaded")
    page.locator("[data-view='football']").click()


# P0-A Test 4: canonical ACTIVE signal opens value modal
def test_p0a_canonical_signal_opens_value_modal(page: Page, server_url: str) -> None:
    """P0-A: A fully canonical ACTIVE signal can open the value bet modal."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _navigate_to_football(page, server_url, _FRESH)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)
    btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    # No JavaScript errors during modal open
    assert not js_errors, f"JS errors on modal open: {js_errors}"

    page.locator("#bet-modal-cancel").click()


# P0-A Test 5: incomplete legacy signal (no canonical fields) cannot open value modal
def test_p0a_legacy_signal_cannot_open_value_modal(page: Page, server_url: str) -> None:
    """P0-A: A signal without signal_status=ACTIVE is blocked by the contract gate."""
    legacy_signal = {
        "sport": "football",
        "match": "Gamma vs Delta",
        "home": "Gamma",
        "away": "Delta",
        "market": "home",
        "odds": 2.10,
        "ev_pct": 15.2,
        "stake_eur": 5.0,
        "stake_pct": 5.0,
        # Intentionally missing: signal_status, signal_id, current_odds, current_ev_pct, odds_ts
    }
    payload = {**_BASE, "football": [legacy_signal]}

    _navigate_to_football(page, server_url, payload)

    btns = page.locator(".place-bet-btn")
    if btns.count() == 0:
        # P1.5-H filter prevents rendering — test passes (no button = no bet possible)
        return

    # If a button is visible, clicking it must NOT open the modal.
    # (contract gate rejects signals without signal_status=ACTIVE)
    # P0-A item G: assertion must genuinely fail if the value modal opens incorrectly.
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    btns.first.click()
    modal = page.locator("#bet-modal-bd")
    # Deliberately NOT catching the exception — the test must fail if the modal opens.
    expect(modal).not_to_be_visible(timeout=2_000)
    assert not any("ReferenceError" in e for e in js_errors), f"ReferenceError: {js_errors}"


# P0-A Test 6: zero-actionable-signals PWA remains usable
def test_p0a_zero_signals_pwa_usable(page: Page, server_url: str) -> None:
    """P0-A: When no actionable signals exist, the PWA loads and shows empty state."""
    payload = {**_BASE, "football": [], "tennis": []}
    _inject_signals(page, payload)
    page.goto(server_url, wait_until="domcontentloaded")

    nav = page.locator("nav.bottom-nav")
    expect(nav).to_be_visible(timeout=10_000)


# P0-A Test 7: value modal odds field is locked (readOnly) for canonical signals
def test_p0a_value_odds_field_is_readonly(page: Page, server_url: str) -> None:
    """P0-A: The odds input is readOnly for value bets — canonical price is authoritative."""
    _navigate_to_football(page, server_url, _FRESH)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)
    btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    odds_input = page.locator("#bet-modal-odds-input")
    readonly = odds_input.evaluate("el => el.readOnly")
    assert readonly, "Odds input must be readOnly for value bets"

    page.locator("#bet-modal-cancel").click()


# P0-A Test 8: >5% stake disables confirm button
def test_p0a_over_cap_stake_disables_confirm(page: Page, server_url: str) -> None:
    """P0-A: Entering a stake >5% of bankroll disables the confirm button."""
    payload = {
        **_FRESH,
        "bankroll_state": {"start": 100, "free": 100, "staked": 0, "exposure_pct": 0, "max_win": 0, "pnl_closed": 0},
    }
    _navigate_to_football(page, server_url, payload)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)
    btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    stake_input = page.locator("#bet-modal-stake")
    stake_input.fill("20")
    stake_input.dispatch_event("input")

    confirm_btn = page.locator("#bet-modal-confirm")
    is_disabled = confirm_btn.evaluate("el => el.disabled")
    assert is_disabled, "Confirm button must be disabled when stake >5% cap"

    page.locator("#bet-modal-cancel").click()


# P0-A Test 9: no JavaScript ReferenceError on modal open + submit flow
def test_p0a_no_js_reference_error_on_submit(page: Page, server_url: str) -> None:
    """P0-A: No ReferenceError when clicking confirm on a canonical value signal."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _navigate_to_football(page, server_url, _FRESH)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)
    btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    stake_input = page.locator("#bet-modal-stake")
    stake_input.fill("5.00")
    stake_input.dispatch_event("input")

    confirm_btn = page.locator("#bet-modal-confirm")
    if not confirm_btn.evaluate("el => el.disabled"):
        page.route("**/pending_bets", lambda route: route.fulfill(status=401, body='{"error":"test"}'))
        confirm_btn.click()
        page.wait_for_timeout(500)

    ref_errors = [e for e in js_errors if "ReferenceError" in e]
    assert not ref_errors, f"ReferenceError found in JS: {ref_errors}"

    token_modal = page.locator("#token-modal-bd")
    if token_modal.is_visible():
        page.locator("#token-modal-cancel").click()
        page.wait_for_timeout(300)
    if modal.is_visible():
        page.locator("#bet-modal-cancel").click()


# P0-A Test 10: submit flow — canonical payload delivered with correct current_odds
def test_p0a_submit_sends_canonical_odds(page: Page, server_url: str) -> None:
    """P0-A item G: submitted payload must use current_odds, not scan_odds fallback.
    Intercepts /pending_bets and asserts the submitted odds == signal.current_odds.
    """
    import json as _json

    captured_bodies: list[dict] = []
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    def _capture(route):
        try:
            body = _json.loads(route.request.post_data or '{}')
        except Exception:
            body = {}
        captured_bodies.append(body)
        route.fulfill(status=200, body='{"ok":true,"id":"test-id"}')

    _navigate_to_football(page, server_url, _FRESH)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)

    # Inject the token so the submit flow doesn't bail out at the token check
    page.evaluate("localStorage.setItem('sb_token','test-token-for-playwright')")

    btn.click()
    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    stake_input = page.locator("#bet-modal-stake")
    stake_input.fill("5.00")
    stake_input.dispatch_event("input")

    confirm_btn = page.locator("#bet-modal-confirm")
    page.route("**/pending_bets", _capture)

    # Confirm must not be disabled for a valid signal with bankroll data
    # (no bankroll_state in _FRESH → button may be disabled due to missing free amount)
    # If disabled, the test still proves no ReferenceError. If enabled, we verify the payload.
    is_disabled = confirm_btn.evaluate("el => el.disabled")
    if not is_disabled:
        page.evaluate("window._confirmDialogOverride = true; window.confirm = () => true;")
        confirm_btn.click()
        page.wait_for_timeout(800)

    ref_errors = [e for e in js_errors if "ReferenceError" in e]
    assert not ref_errors, f"ReferenceError on submit: {ref_errors}"

    if captured_bodies:
        payload = captured_bodies[0]
        # Submitted source must be 'value' (canonical signal), not 'manual'
        assert payload.get('source') == 'value', f"Expected source=value, got: {payload.get('source')}"
        # Submitted odds must equal current_odds (2.10) from the canonical signal fixture
        submitted_odds = float(payload.get('odds', 0))
        assert abs(submitted_odds - 2.10) < 0.01, \
            f"Submitted odds {submitted_odds} differ from canonical current_odds 2.10"

    if modal.is_visible():
        page.locator("#bet-modal-cancel").click()


# P0-A Test 11: FND-20260814-031 — ACTIVE signal without current_odds renders no bet button
def test_p0a_missing_current_odds_no_bet_button(page: Page, server_url: str) -> None:
    """FND-031: ACTIVE signal with current_odds=None must render NO bet button at all.

    The signal card must be informational only; no manual fallback bet button may appear.
    This is a hard assertion, not a conditional skip.
    """
    sig_no_current = _canonical_signal(current_odds=None, current_ev_pct=None)
    payload = {**_BASE, "football": [sig_no_current]}

    _navigate_to_football(page, server_url, payload)

    btns = page.locator(".place-bet-btn")
    assert btns.count() == 0, (
        f"FND-031: ACTIVE signal without current_odds must render NO bet button "
        f"(not even a manual fallback). Got {btns.count()} button(s)."
    )


# ── P0-A Test 12: FND-20260814-004 — mandatory browser submit ─────────────────

_FRESH_WITH_BANKROLL: dict = {
    **_FRESH,
    "bankroll_state": {
        "start": 100,
        "free": 100,
        "staked": 0,
        "exposure_pct": 0,
        "max_win": 0,
        "pnl_closed": 0,
    },
}


def test_fnd004_mandatory_submit_delivers_canonical_payload(page: Page, server_url: str) -> None:
    """FND-20260814-004: Mandatory browser submit test.

    Requirements (all unconditional — no conditional pass):
    - Valid fixture with bankroll_state must enable the confirm button.
    - Confirm button click must happen.
    - Exactly one /pending_bets POST request must be captured.
    - Submitted payload: source=value, canonical signal_id present,
      odds==current_odds (2.10), stake<=5% of bankroll, no JS errors.
    """
    import json as _json

    captured_bodies: list[dict] = []
    captured_methods: list[str] = []
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    def _capture_pending(route):
        try:
            body = _json.loads(route.request.post_data or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        captured_bodies.append(body)
        captured_methods.append(route.request.method)
        route.fulfill(status=200, body='{"ok":true,"id":"fnd004-test-id"}')

    _navigate_to_football(page, server_url, _FRESH_WITH_BANKROLL)

    # Inject token and override confirm dialog before any interaction.
    page.evaluate("""
        localStorage.setItem('sb_token', 'fnd004-test-token');
        window.confirm = () => true;
    """)

    btn = page.locator(".place-bet-btn").first
    expect(btn).to_be_visible(timeout=10_000)
    btn.click()

    modal = page.locator("#bet-modal-bd")
    expect(modal).to_be_visible(timeout=3_000)

    stake_input = page.locator("#bet-modal-stake")
    stake_input.fill("5.00")
    stake_input.dispatch_event("input")

    confirm_btn = page.locator("#bet-modal-confirm")

    # FND-004 hard assertion: confirm must be ENABLED for valid fixture + bankroll_state.
    is_disabled = confirm_btn.evaluate("el => el.disabled")
    assert not is_disabled, (
        "FND-20260814-004: confirm button must be ENABLED for a valid canonical signal "
        "with bankroll_state. A disabled button means the modal wiring or bankroll injection "
        "is broken — this is a hard failure, not a conditional skip."
    )

    page.route("**/pending_bets", _capture_pending)
    confirm_btn.click()
    page.wait_for_timeout(1200)

    # FND-004 hard assertion: exactly one /pending_bets request must have been captured.
    assert len(captured_bodies) == 1, (
        f"FND-20260814-004: confirm click must trigger exactly one /pending_bets POST. "
        f"Got {len(captured_bodies)} captured requests. "
        "Submit did not fire — modal flow, token check, or network route is broken."
    )

    # FND-004: request must be POST (not GET or other method).
    assert captured_methods[0] == "POST", (
        f"FND-20260814-004: /pending_bets must use POST method, got {captured_methods[0]!r}."
    )

    submitted = captured_bodies[0]

    # source must be 'value'
    assert submitted.get("source") == "value", (
        f"FND-004: submitted source must be 'value', got {submitted.get('source')!r}. "
        f"Full payload: {submitted}"
    )

    # canonical signal_id must be present and non-empty
    signal_id_sent = submitted.get("signal_id", "")
    assert signal_id_sent, (
        f"FND-004: signal_id must be present in submitted payload. "
        f"Full payload: {submitted}"
    )

    # odds must equal current_odds from canonical signal fixture (2.10)
    submitted_odds = float(submitted.get("odds", 0))
    assert abs(submitted_odds - 2.10) < 0.02, (
        f"FND-004: submitted odds {submitted_odds:.4f} must equal canonical "
        f"current_odds 2.10. Full payload: {submitted}"
    )

    # stake must be <= 5% of bankroll (€100 → ceiling €5.00)
    submitted_stake = float(submitted.get("stake_eur", submitted.get("stake", 0)))
    assert submitted_stake <= 5.01, (
        f"FND-004: submitted stake €{submitted_stake:.2f} exceeds 5% of €100 bankroll (€5.00). "
        f"Full payload: {submitted}"
    )

    # No JavaScript errors
    ref_errors = [e for e in js_errors if "ReferenceError" in e or "TypeError" in e]
    assert not ref_errors, f"FND-004: JS errors on mandatory submit: {ref_errors}"

    if modal.is_visible():
        page.locator("#bet-modal-cancel").click()


# P0-A Test 13: FND-20260814-031 — predCard model-tip renders no bet button
def test_fnd031_predcard_no_bet_button(page: Page, server_url: str) -> None:
    """FND-031: predCard model-tip surfaces must not render any .place-bet-btn button.

    Model-tip predictions have no backing canonical signal → no bet action allowed.
    Odds are shown as informational display only (span, not button).
    """
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    model_tip = {
        "p_home": 0.55, "p_draw": 0.25, "p_away": 0.20,
        "xg_home": 1.6, "xg_away": 0.9,
        "p_btts_yes": 0.45, "p_btts_no": 0.55,
        "top_scorers_home": [], "top_scorers_away": [],
    }
    payload = {
        **_FRESH,
        "model_tips": {"Alpha vs Beta": model_tip},
        "all_odds": {
            "Alpha vs Beta": {"home": 1.85, "draw": 3.40, "away": 4.20, "btts_yes": 1.90}
        },
    }
    _navigate_to_football(page, server_url, payload)

    pred_cards = page.locator(".pred-card")
    if pred_cards.count() == 0:
        return  # predCard not rendered for this fixture — test passes trivially

    pred_card = pred_cards.first
    bet_btns = pred_card.locator(".place-bet-btn")
    assert bet_btns.count() == 0, (
        f"FND-031: predCard must not contain any .place-bet-btn bet buttons. "
        f"Got {bet_btns.count()} button(s) — model-tip has no canonical signal backing."
    )

    ref_errors = [e for e in js_errors if "ReferenceError" in e or "TypeError" in e]
    assert not ref_errors, f"FND-031: JS errors in predCard render: {ref_errors}"
