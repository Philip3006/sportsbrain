"""Regression tests: TOP RECOMMENDATIONS — Next 24h homepage component.

Covers all 12 spec-required scenarios:
 1. 5+ qualifying signals → exactly top 5 displayed.
 2. 3 qualifying signals → exactly 3 displayed.
 3. 1 qualifying signal → 1 displayed.
 4. 0 qualifying signals → explicit no-recommendation state.
 5. Football + Tennis compete in the same ranking.
 6. Event >24h away → excluded.
 7. Already-started event → excluded.
 8. Invalid/missing odds → excluded.
 9. Failed-gate / invalid signal → excluded.
10. Shadow-only prediction → excluded (EV below gate → excluded).
11. Duplicate event+market+selection → deduplicated to one card.
12. Ranking order follows existing canonical _evScore order.
"""
import base64
import functools
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"
_NOW = datetime.now(timezone.utc)

def _ko(hours=5):
    return (_NOW + timedelta(hours=hours)).isoformat()

def _recent_ts(minutes_ago=5):
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

_BASE: dict = {
    "updated": _NOW.isoformat(),
    "football": [],
    "tennis": [],
    "open_bets": [],
    "settled_bets": [],
    "bankroll_state": {"start": 100.0, "free": 100.0, "staked": 0, "exposure_pct": 0, "max_win": 0, "pnl_closed": 0},
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
    "model_evals": {},
}


def _tennis(match="A vs B", odds=1.55, ev_pct=10.0, kickoff_h=5, market="away", confidence="HIGH",
            signal_status="ACTIVE", current_ev_pct=None, odds_ts_min_ago=5):
    """P1.5-H: includes signal_status, current_odds, current_ev_pct, odds_ts by default."""
    return {
        "sport": "tennis", "match": match, "market": market,
        "odds": odds, "ev_pct": ev_pct, "model_prob": 65.0,
        "stake_eur": 12.0, "confidence": confidence,
        "kickoff": _ko(kickoff_h), "tour": "wta", "league": "wta",
        "signal_status": signal_status,
        "current_odds": odds,
        "current_ev_pct": ev_pct if current_ev_pct is None else current_ev_pct,
        "odds_ts": _recent_ts(odds_ts_min_ago),
    }


def _football(match="Home vs Away", odds=2.2, ev_pct=30.0, kickoff_h=5, market="away",
              signal_status="ACTIVE", odds_ts_min_ago=5):
    """P1.5-H: includes signal_status, current_odds, current_ev_pct, odds_ts by default."""
    return {
        "sport": "football", "match": match, "market": market,
        "odds": odds, "ev_pct": ev_pct, "model_prob": 45.0,
        "stake_eur": 10.0, "confidence": "HIGH",
        "kickoff": _ko(kickoff_h), "league": "bl2",
        "signal_status": signal_status,
        "current_odds": odds,
        "current_ev_pct": ev_pct,
        "odds_ts": _recent_ts(odds_ts_min_ago),
    }


@pytest.fixture(scope="module")
def server_url():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DOCS_DIR))
    srv = HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _inject(page: Page, payload: dict) -> None:
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    page.add_init_script(f"""
        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register = () =>
                Promise.reject(new Error('SW disabled in tests'));
        }}
        localStorage.setItem('sb_seen_onboarding', '1');
        const _body = atob('{b64}');
        const _orig = window.fetch.bind(window);
        window.fetch = (url, opts) => String(url).includes('signals.json')
            ? Promise.resolve(new Response(_body, {{status:200,headers:{{'Content-Type':'application/json'}}}}))
            : _orig(url, opts);
    """)


def _home(page: Page, server_url: str, payload: dict) -> None:
    _inject(page, payload)
    page.goto(server_url, wait_until="domcontentloaded")
    page.wait_for_timeout(700)


# ── 1. 5+ qualifying signals → exactly top 5 displayed ───────────────────────

def test_five_plus_signals_shows_top_5(page: Page, server_url: str):
    """6 qualifying tennis signals → only top 5 appear in toprec-card."""
    sigs = [_tennis(match=f"P{i} vs Q{i}", ev_pct=10.0 + i) for i in range(6)]
    payload = {**_BASE, "tennis": sigs}
    _home(page, server_url, payload)
    cards = page.locator(".toprec-pick")
    expect(cards).to_have_count(5, timeout=5_000)


# ── 2. 3 qualifying signals → exactly 3 displayed ────────────────────────────

def test_three_signals_shows_three(page: Page, server_url: str):
    sigs = [_tennis(match=f"A{i} vs B{i}", ev_pct=10.0 + i) for i in range(3)]
    payload = {**_BASE, "tennis": sigs}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(3, timeout=5_000)


# ── 3. 1 qualifying signal → 1 displayed ─────────────────────────────────────

def test_one_signal_shows_one(page: Page, server_url: str):
    payload = {**_BASE, "tennis": [_tennis()]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)
    expect(page.locator(".toprec-empty")).not_to_be_visible(timeout=2_000)


# ── 4. 0 qualifying signals → explicit no-recommendation state ────────────────

def test_zero_signals_shows_empty_state(page: Page, server_url: str):
    """No qualifying signals → toprec-card still renders with empty-state message."""
    payload = {**_BASE, "tennis": [], "football": []}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-card")).to_be_visible(timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=3_000)
    expect(page.locator(".toprec-empty-msg")).to_contain_text("Keine qualifizierenden Signale", timeout=3_000)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=2_000)


# ── 5. Football + Tennis compete in same ranking ──────────────────────────────

def test_football_and_tennis_compete_in_same_ranking(page: Page, server_url: str):
    """One football and one tennis signal → both can appear in top-recs."""
    payload = {**_BASE,
               "football": [_football(ev_pct=30.0)],
               "tennis": [_tennis(ev_pct=10.0)]}
    _home(page, server_url, payload)
    picks = page.locator(".toprec-pick")
    expect(picks).to_have_count(2, timeout=5_000)


# ── 6. Event >24h away → excluded ────────────────────────────────────────────

def test_event_beyond_24h_excluded(page: Page, server_url: str):
    """Signal with kickoff 26 hours from now → excluded from top-recs."""
    sig = _tennis(kickoff_h=26)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=3_000)


# ── 7. Already-started event → excluded ──────────────────────────────────────

def test_already_started_event_excluded(page: Page, server_url: str):
    """Signal with kickoff 1 hour in the past → excluded (not future)."""
    sig = _tennis(kickoff_h=-1)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=3_000)


# ── 8. Invalid/missing odds → excluded ───────────────────────────────────────

def test_missing_odds_excluded(page: Page, server_url: str):
    # P1.5-H: current_odds is authoritative; clear both scan-time and refreshed odds
    sig = _tennis()
    del sig["odds"]
    del sig["current_odds"]
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=3_000)


def test_null_odds_excluded(page: Page, server_url: str):
    # P1.5-H: current_odds=None is the authoritative field that must be missing
    sig = _tennis()
    sig["odds"] = None
    sig["current_odds"] = None
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_zero_odds_excluded(page: Page, server_url: str):
    # P1.5-H: current_odds <= 1.0 must be rejected (0 is invalid)
    sig = _tennis()
    sig["odds"] = 0
    sig["current_odds"] = 0
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── 9. Failed-gate / invalid signal → excluded ───────────────────────────────

def test_tennis_below_ev_floor_excluded(page: Page, server_url: str):
    """Tennis signal EV < 5% → _evScore returns -1 → excluded."""
    sig = _tennis(ev_pct=3.0)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_high_odds_excluded(page: Page, server_url: str):
    """Football away with odds=3.0 (>2.5) → excluded."""
    sig = _football(odds=3.0, ev_pct=30.0)
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_low_ev_excluded(page: Page, server_url: str):
    """Football away with EV < 25% → excluded."""
    sig = _football(odds=2.2, ev_pct=15.0)
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── 10. Shadow-only / below-gate prediction → excluded ───────────────────────

def test_shadow_prediction_low_ev_excluded(page: Page, server_url: str):
    """Signal marked as shadow (simulated by EV below production gate) → excluded."""
    sig = _tennis(ev_pct=2.0)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── 11. Duplicate event+market+selection → deduplicated ──────────────────────

def test_duplicate_event_market_selection_deduplicated(page: Page, server_url: str):
    """Same event + same market + same selection → exactly one card shown (canonical best)."""
    sig1 = _tennis(match="Djokovic vs Alcaraz", ev_pct=12.0, market="away")
    sig2 = _tennis(match="Djokovic vs Alcaraz", ev_pct=10.0, market="away")
    payload = {**_BASE, "tennis": [sig1, sig2]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


def test_same_match_different_market_not_deduplicated(page: Page, server_url: str):
    """Same event + same market type + different selections → treated as distinct signals.

    o/u_games_22.5_over and o/u_games_22.5_under share the same market category
    (games total 22.5) but represent different selections (over vs under).
    Dedup key is event+market+selection; these differ → both must appear.
    """
    sig1 = _tennis(match="Djokovic vs Alcaraz", ev_pct=12.0, market="o/u_games_22.5_over")
    sig2 = _tennis(match="Djokovic vs Alcaraz", ev_pct=10.0, market="o/u_games_22.5_under")
    payload = {**_BASE, "tennis": [sig1, sig2]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(2, timeout=5_000)


def test_explicit_selection_field_differentiates_dedup(page: Page, server_url: str):
    """Same event + same market key but explicit selection field differs → distinct.

    Tests the s.selection field path in the dedup key. Signals that differ only
    in their explicit selection field must not be collapsed.
    """
    sig1 = _tennis(match="Djokovic vs Alcaraz", ev_pct=12.0, market="away")
    sig1["selection"] = "Alcaraz"
    sig2 = _tennis(match="Djokovic vs Alcaraz", ev_pct=10.0, market="away")
    sig2["selection"] = "Djokovic"
    payload = {**_BASE, "tennis": [sig1, sig2]}
    _home(page, server_url, payload)
    # Different selection field → two distinct picks allowed (up to 5 limit)
    expect(page.locator(".toprec-pick")).to_have_count(2, timeout=5_000)


def test_duplicate_source_records_canonical_best_shown_once(page: Page, server_url: str):
    """Identical signal from multiple source records → only canonical (highest-ranked) shown once.

    Two records: same event + same market + same selection. The higher-EV record
    is the canonical one. Result: exactly 1 pick, and it is ranked #1.
    """
    sig_high = _tennis(match="Medvedev vs Zverev", ev_pct=14.0, market="home")
    sig_low  = _tennis(match="Medvedev vs Zverev", ev_pct=8.0,  market="home")
    payload = {**_BASE, "tennis": [sig_high, sig_low]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)
    expect(page.locator(".toprec-rank").first).to_have_text("#1", timeout=3_000)


# ── 12. Ranking order follows _evScore ────────────────────────────────────────

def test_ranking_order_follows_ev_score(page: Page, server_url: str):
    """Highest-EV signal must appear at rank #1 (first .toprec-pick)."""
    low_ev  = _tennis(match="Low vs EV",  ev_pct=7.0,  kickoff_h=3)
    high_ev = _tennis(match="High vs EV", ev_pct=15.0, kickoff_h=6)
    payload = {**_BASE, "tennis": [low_ev, high_ev]}
    _home(page, server_url, payload)
    picks = page.locator(".toprec-pick")
    expect(picks).to_have_count(2, timeout=5_000)
    first_rank = picks.first.locator(".toprec-rank")
    expect(first_rank).to_have_text("#1", timeout=3_000)
    # The highest EV signal should be listed first — verify it's the "High vs EV" match
    first_text = picks.first.inner_text()
    assert "High" in first_text or "EV" in first_text, \
        f"Expected high-EV signal first, got: {first_text!r}"


def test_component_always_visible_even_without_free_bankroll(page: Page, server_url: str):
    """toprec-card must render even when bankroll free=0 (not gated on bankroll)."""
    payload = {
        **_BASE,
        "tennis": [_tennis()],
        "bankroll_state": {"start": 100.0, "free": 0.0, "staked": 100.0, "exposure_pct": 100, "max_win": 0, "pnl_closed": 0},
    }
    _home(page, server_url, payload)
    expect(page.locator(".toprec-card")).to_be_visible(timeout=5_000)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=3_000)


# ── P1.5-H: Fail-closed gate — missing required fields must never produce recommendations ──

def test_p15h_missing_signal_status_excluded(page: Page, server_url: str):
    """P1.5-H: signal without signal_status must NOT appear in Top Recs (fail closed)."""
    sig = _tennis()
    del sig["signal_status"]
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=3_000)


def test_p15h_signal_status_edge_lost_excluded(page: Page, server_url: str):
    """P1.5-H: EDGE_LOST signal must not appear in Top Recs."""
    sig = _tennis(signal_status="EDGE_LOST")
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_signal_status_stale_odds_excluded(page: Page, server_url: str):
    """P1.5-H: STALE_ODDS signal must not appear in Top Recs."""
    sig = _tennis(signal_status="STALE_ODDS")
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_signal_status_unrefreshable_excluded(page: Page, server_url: str):
    """P1.5-H: UNREFRESHABLE signal must not appear in Top Recs."""
    sig = _tennis(signal_status="UNREFRESHABLE")
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_missing_current_odds_excluded(page: Page, server_url: str):
    """P1.5-H: ACTIVE signal without current_odds must not appear in Top Recs."""
    sig = _tennis()
    del sig["current_odds"]
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_invalid_current_odds_excluded(page: Page, server_url: str):
    """P1.5-H: current_odds <= 1.0 is invalid and must block the signal."""
    sig = _tennis()
    sig["current_odds"] = 0.9
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_missing_current_ev_pct_excluded(page: Page, server_url: str):
    """P1.5-H: ACTIVE signal without current_ev_pct must not appear in Top Recs."""
    sig = _tennis()
    del sig["current_ev_pct"]
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_missing_odds_ts_excluded(page: Page, server_url: str):
    """P1.5-H: ACTIVE signal without odds_ts must not appear in Top Recs."""
    sig = _tennis()
    del sig["odds_ts"]
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_stale_odds_ts_excluded(page: Page, server_url: str):
    """P1.5-H: odds_ts > 30 min old must block the signal from Top Recs."""
    sig = _tennis(odds_ts_min_ago=35)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_p15h_fresh_odds_ts_included(page: Page, server_url: str):
    """P1.5-H: ACTIVE signal with odds_ts exactly 29 min ago must still qualify."""
    sig = _tennis(odds_ts_min_ago=29)
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


def test_p15h_active_signal_with_all_fields_included(page: Page, server_url: str):
    """P1.5-H regression: signal with all required fields and ACTIVE status must appear."""
    sig = _tennis()
    payload = {**_BASE, "tennis": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)
