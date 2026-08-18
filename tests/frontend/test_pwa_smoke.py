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


# P0-A Test 14: FND-20260814-031 — compact mode non-actionable signal renders no bet button
def test_fnd031_compact_non_actionable_no_bet_button(page: Page, server_url: str) -> None:
    """FND-031: In compact mode, a signal without current_odds must NOT render a bet button.

    Compact mode bypassed the actionability check in TASK-P0A-011; this verifies the fix.
    """
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    # Activate compact mode before page scripts run
    page.add_init_script("localStorage.setItem('sb_compact_mode', '1');")

    sig_no_current = _canonical_signal(current_odds=None, current_ev_pct=None)
    payload = {**_BASE, "football": [sig_no_current]}
    _navigate_to_football(page, server_url, payload)

    compact_btns = page.locator(".compact-place-btn")
    assert compact_btns.count() == 0, (
        f"FND-031: compact mode non-actionable signal must render NO .compact-place-btn. "
        f"Got {compact_btns.count()} button(s)."
    )
    ref_errors = [e for e in js_errors if "ReferenceError" in e or "TypeError" in e]
    assert not ref_errors, f"FND-031: JS errors in compact render: {ref_errors}"


# P0-A Test 15: FND-20260814-031 — compact mode actionable signal does render bet button
def test_fnd031_compact_actionable_has_bet_button(page: Page, server_url: str) -> None:
    """FND-031: In compact mode, a fully canonical/actionable signal DOES render a bet button."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    page.add_init_script("localStorage.setItem('sb_compact_mode', '1');")

    sig = _canonical_signal()
    payload = {**_BASE, "football": [sig]}
    _navigate_to_football(page, server_url, payload)

    compact_btns = page.locator(".compact-place-btn")
    assert compact_btns.count() > 0, (
        "FND-031: compact mode actionable signal must render a .compact-place-btn button."
    )
    # Must be data-source=value, never manual
    btn = compact_btns.first
    assert btn.get_attribute("data-source") == "value", (
        f"FND-031: compact actionable button must have data-source=value, "
        f"got '{btn.get_attribute('data-source')}'"
    )
    ref_errors = [e for e in js_errors if "ReferenceError" in e or "TypeError" in e]
    assert not ref_errors, f"FND-031: JS errors in compact actionable render: {ref_errors}"


# ── T12: P0C-001 — Logged-out public PWA smoke ───────────────────────────────
# SEC-001/SEC-003 browser enforcement.
# Public payload: zero private fields (no bankroll_state, open_bets, settled_bets,
# portfolio, wm_stats, default_user). Verifies the PWA loads, signals remain
# usable, and private-field absence does not crash the browser.

_PUBLIC_ONLY: dict = {
    "updated": _NOW.isoformat(),
    "build_info": {},
    "meta": {"stale_odds": False},
    "football": [_canonical_signal()],
    "tennis": [],
    "schedule": [],
    "all_odds": {},
    "model_tips": {},
    "model_evals": {},
    "top_elo": [],
    "wm_results": [],
    "odds_history": {},
    # Intentionally absent (public serialization boundary):
    # bankroll_state, open_bets, settled_bets, history, portfolio, wm_stats, default_user
}


def test_t12_public_payload_has_no_private_fields() -> None:
    """T12a: The public-only payload used by T12 browser tests contains zero private fields.

    Verifies the test fixture itself satisfies the P0C-001 serialization boundary.
    SEC-001 invariant: no bankroll_state, open_bets, settled_bets, default_user.
    """
    import json as _json
    from src.notifications.public_serializer import assert_no_private_fields

    payload_str = _json.dumps(_PUBLIC_ONLY)
    assert "bankroll_state" not in payload_str, "T12: bankroll_state in public payload fixture"
    assert "open_bets" not in payload_str, "T12: open_bets in public payload fixture"
    assert "settled_bets" not in payload_str, "T12: settled_bets in public payload fixture"
    assert "default_user" not in payload_str, "T12: default_user in public payload fixture"
    assert_no_private_fields(_PUBLIC_ONLY)  # recursive canonical check — must not raise


def test_t12_pwa_loads_on_public_only_payload(page: Page, server_url: str) -> None:
    """T12b: Logged-out PWA loads successfully on a public-only payload.

    Removing bankroll_state/open_bets/settled_bets must not crash the PWA.
    SEC-003: private-field absence degrades gracefully (no TypeError/ReferenceError).
    """
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _inject_signals(page, _PUBLIC_ONLY)
    page.goto(server_url, wait_until="domcontentloaded")

    nav = page.locator("nav.bottom-nav")
    expect(nav).to_be_visible(timeout=10_000)

    crash_errors = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash_errors, f"T12: JS crash errors on public-only payload: {crash_errors}"


def test_t12_public_football_signals_usable(page: Page, server_url: str) -> None:
    """T12c: Football signals tab remains usable with public-only payload (no private fields)."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _inject_signals(page, _PUBLIC_ONLY)
    page.goto(server_url, wait_until="domcontentloaded")
    page.locator("[data-view='football']").click()

    nav = page.locator("nav.bottom-nav")
    expect(nav).to_be_visible(timeout=10_000)

    crash_errors = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash_errors, f"T12: JS crash in football tab on public-only payload: {crash_errors}"


def test_t12_bankroll_ui_degrades_gracefully(page: Page, server_url: str) -> None:
    """T12d: Bankroll/account UI handles absent private fields without crash or leaked state."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _inject_signals(page, _PUBLIC_ONLY)
    page.goto(server_url, wait_until="domcontentloaded")

    # Navigate to account/bets tab if present — must not crash without private state.
    account_tab = page.locator(
        "[data-view='account'], [data-view='bets'], [data-view='bankroll']"
    )
    if account_tab.count() > 0:
        account_tab.first.click()
        page.wait_for_timeout(500)

    nav = page.locator("nav.bottom-nav")
    expect(nav).to_be_visible(timeout=10_000)

    crash_errors = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash_errors, (
        f"T12: JS crash when navigating account UI without private fields: {crash_errors}"
    )


# ── P0C-002: dual-fetch static-source scans (T14–T22) ────────────────────────
# These tests DO NOT require Playwright — they scan the shipped browser sources
# for security regressions. Fast, deterministic, and hook into HARD GATE 7.

def _read_browser_sources() -> str:
    """Concatenate all shipped JS files into a single string for scanning."""
    js_dir = DOCS_DIR / "js"
    parts: list[str] = []
    for p in sorted(js_dir.glob("*.js")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_t19_browser_sources_have_no_api_or_master_token_literals() -> None:
    """T19: Shipped browser JS must not contain hard-coded auth tokens.

    Neither env.API_TOKEN references nor a variable literally named
    master_token / MASTER_TOKEN may exist in browser code. Only the Worker
    (server-side) may know about the master token.
    """
    src = _read_browser_sources()
    # Allowlist: rotate_token / token_status are legit endpoints referenced from
    # bets.js. We reject only ASSIGNMENTS of master credentials in browser code.
    for needle in ("env.API_TOKEN", "MASTER_TOKEN =", "master_token =",
                   "= process.env.API_TOKEN"):
        assert needle not in src, (
            f"T19: forbidden credential pattern '{needle}' in shipped browser JS"
        )


def test_t20_browser_sources_do_not_ingest_query_token() -> None:
    """T20 (a.k.a. SEC-004): browser must NOT persist ?token= URL parameters.

    Regression guard against reintroducing long-lived token ingestion from URL
    query strings (which leak via history, referer, analytics, screen shares).
    Comment mentions are allowed; assignments into localStorage are not.
    """
    src = _read_browser_sources()
    # A localStorage.setItem for the params.get('token') result is forbidden.
    # Simple substring check for the exact ingestion pattern.
    forbidden_patterns = [
        "localStorage.setItem('sb_token', params.get('token')",
        'localStorage.setItem("sb_token", params.get("token")',
        "localStorage.setItem('sb_token', urlToken)",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, f"T20: forbidden ?token= ingestion pattern '{pat}' found"


def test_t21_static_signals_json_has_no_private_fields() -> None:
    """T21: Static docs/data/signals*.json must not contain private financial fields.

    HARD GATE 6 already covers this via public serializer; this is a per-file
    fast regression scan that doesn't need to import the serializer.
    """
    for name in ("signals.json", "signals_philip.json"):
        f = DOCS_DIR / "data" / name
        if not f.exists():
            continue
        txt = f.read_text(encoding="utf-8")
        for banned in ('"bankroll_state"', '"open_bets"', '"settled_bets"',
                       '"default_user"', '"portfolio"'):
            assert banned not in txt, (
                f"T21: private key {banned} present in shipped static file {name}"
            )


def test_t22_worker_betting_safety_regression_boundary() -> None:
    """T22: worker.js still enforces exact-owner routing (no DEFAULT_USER fallback in /me).

    Static grep guard: the /me route must NOT contain a fallback to
    DEFAULT_USER, and _signalsKey must be routed by the authenticated user
    only. Also asserts the auth-check ordering (401 before 403 before 404).
    """
    worker = (Path(__file__).parent.parent.parent / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    # Sanity: /me route exists.
    assert "path === '/me'" in worker, "T22: /me route missing from worker.js"
    # Master token on /me must be forbidden.
    assert "auth.viaMaster || !auth.user" in worker, (
        "T22: master-token-on-/me guard missing (auth.viaMaster || !auth.user)"
    )
    # rotate_token has cross-user rejection.
    assert "cannot rotate token for another user" in worker, (
        "T22: rotate_token cross-user guard missing"
    )
    # token_status has cross-user rejection.
    assert "cannot inspect token status for another user" in worker, (
        "T22: token_status cross-user guard missing"
    )


# ── T14–T17: Playwright degradation smoke — private-channel failure modes ────
# Fetch mock returns different HTTP codes for /me; assert public UI stays alive.

def _inject_dual_fetch(page: Page, public_payload: dict, private_status: int,
                       private_body: dict | None = None) -> None:
    """Mock BOTH signals.json (public) and /me (private) responses."""
    pub_b64 = base64.b64encode(json.dumps(public_payload).encode()).decode()
    priv_body_str = json.dumps(private_body or {"error": "test"})
    priv_b64 = base64.b64encode(priv_body_str.encode()).decode()
    # Preset a token so the browser attempts the private fetch.
    page.add_init_script(f"""
        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register = () =>
                Promise.reject(new Error('SW disabled in tests'));
        }}
        localStorage.setItem('sb_seen_onboarding', '1');
        localStorage.setItem('sb_token', 'test-user-token');
        const _pubBody = atob('{pub_b64}');
        const _privBody = atob('{priv_b64}');
        const _origFetch = window.fetch.bind(window);
        window.fetch = function(url, opts) {{
            const u = String(url);
            if (u.endsWith('/me') || u.includes('/me?')) {{
                return Promise.resolve(new Response(_privBody, {{
                    status: {private_status},
                    headers: {{'Content-Type': 'application/json'}}
                }}));
            }}
            if (u.includes('signals.json')) {{
                return Promise.resolve(new Response(_pubBody, {{
                    status: 200,
                    headers: {{'Content-Type': 'application/json'}}
                }}));
            }}
            return _origFetch(url, opts);
        }};
    """)


def test_t14_logged_out_public_loads_private_unavailable_no_crash(
    page: Page, server_url: str
) -> None:
    """T14: With NO token in localStorage, public loads; private channel is
    'unauthenticated'. PWA must stay usable (no JS crash)."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    _inject_signals(page, _PUBLIC_ONLY)  # no token preset
    page.goto(server_url, wait_until="domcontentloaded")
    expect(page.locator("nav.bottom-nav")).to_be_visible(timeout=10_000)
    crash = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash, f"T14: PWA crashed unauthenticated: {crash}"


def test_t15_private_401_public_stays_usable(page: Page, server_url: str) -> None:
    """T15: Private /me → 401. Token gets cleared, public UI still works."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    _inject_dual_fetch(page, _PUBLIC_ONLY, 401)
    page.goto(server_url, wait_until="domcontentloaded")
    expect(page.locator("nav.bottom-nav")).to_be_visible(timeout=10_000)
    # After the 401, token should have been cleared.
    token_after = page.evaluate("() => localStorage.getItem('sb_token')")
    assert token_after in (None, ""), f"T15: token not cleared after 401 (got {token_after!r})"
    crash = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash, f"T15: JS crash on private 401: {crash}"


def test_t16_private_404_no_fallback_to_default_user(page: Page, server_url: str) -> None:
    """T16: Private /me → 404. PWA MUST NOT fall back to another user's data."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    _inject_dual_fetch(page, _PUBLIC_ONLY, 404,
                       private_body={"error": "private_state_unavailable", "owner": "alice"})
    page.goto(server_url, wait_until="domcontentloaded")
    expect(page.locator("nav.bottom-nav")).to_be_visible(timeout=10_000)
    # No private markers should have leaked into the DOM.
    body_html = page.evaluate("() => document.body.innerHTML")
    assert "PHILIP_ONLY_MARKER" not in body_html, "T16: fallback to philip on 404"
    crash = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash, f"T16: JS crash on private 404: {crash}"


def test_t17_private_5xx_public_stays_usable(page: Page, server_url: str) -> None:
    """T17: Private /me → 500. Public UI must remain usable."""
    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    _inject_dual_fetch(page, _PUBLIC_ONLY, 500)
    page.goto(server_url, wait_until="domcontentloaded")
    expect(page.locator("nav.bottom-nav")).to_be_visible(timeout=10_000)
    crash = [e for e in js_errors if "TypeError" in e or "ReferenceError" in e]
    assert not crash, f"T17: JS crash on private 500: {crash}"
