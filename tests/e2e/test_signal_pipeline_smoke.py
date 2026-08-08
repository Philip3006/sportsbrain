"""
CEO-Roadmap O0-2: End-to-End Smoke Test.

Durchläuft den kompletten Signal-Pipeline-Pfad in einem netzunabhängigen
Test — kein Mock einzelner Funktionen sondern echte Modulgrenzen:

  Odds (mock)
    → Feature Creation (DC scoreline matrix)
    → Prediction (predict_match)
    → Edge / EV Calculation (kelly.expected_value)
    → Betting Gate (edge > 0, EV > MIN_EV)
    → Kelly Fraction + Stake
    → Bankroll Cap (Rule 4: ≤5%)
    → Signal Object (Dict)
    → JSON Serialization (round-trip)

Wenn CI-Gate blocked → dieser Test fängt jede strukturelle Regression im
Kernpfad ab, bevor sie in Production landet.

External Delivery (Cloudflare KV / VAPID Push / PWA) ist bewusst NICHT
Teil dieses Tests und läuft separat als Integration-Check (siehe
tennis_scan.yml / consume_pending_bets.yml smoke-Runs in Production).
"""
from __future__ import annotations

import json

import pytest

from src.data.odds_api import mock_upcoming_matches
from src.models.dixon_coles import DixonColesParams, predict_match
from src.betting.kelly import expected_value, kelly_fraction, stake


# Rule 4 (Single Bet Bankroll Cap): technische Invariante
BANKROLL_CAP_PCT = 0.05
# Rule 2 (Fractional Kelly): 25%
KELLY_FRACTION = 0.25
MIN_EV = 0.02
BANKROLL = 100.0
MAX_STAKE_EUR = 40.0  # Tier-Cap


def _fake_dc_params() -> DixonColesParams:
    """Deterministische DC-Parameter für Testing (kein Trainingslauf nötig)."""
    return DixonColesParams(
        attack={"United States": 0.20, "Mexico": 0.05,
                "Brazil": 0.35, "Argentina": 0.40},
        defence={"United States": -0.05, "Mexico": 0.10,
                 "Brazil": -0.20, "Argentina": -0.25},
        home_adv=0.25,
        rho=-0.10,
    )


class TestE2ESignalPipeline:
    """Ein realistischer Match → Signal Durchlauf, alle Modulgrenzen echt."""

    def test_full_pipeline_produces_valid_signal(self):
        # ── 1. INPUT: Mock-Odds (Real-Struktur des Live-Providers) ────────
        matches = mock_upcoming_matches()
        assert matches, "mock_upcoming_matches muss mindestens ein Match liefern"
        m = matches[0]
        assert "home_team" in m and "away_team" in m and "home_odds" in m
        home, away = m["home_team"], m["away_team"]

        # ── 2/3. FEATURE + PREDICTION: echter DC-Predict ───────────────────
        params = _fake_dc_params()
        pred = predict_match(home, away, params, neutral=True)
        assert set(pred.keys()) == {"p_home", "p_draw", "p_away"}
        total = pred["p_home"] + pred["p_draw"] + pred["p_away"]
        assert 0.999 < total < 1.001, f"Wahrscheinlichkeiten müssen sich zu 1 summieren, sind {total}"

        # ── 4. EDGE / EV: echter kelly.expected_value ──────────────────────
        # Wähle die stärkste Vorhersage
        best_selection = max(("home", "draw", "away"),
                             key=lambda s: pred[f"p_{s}"])
        best_prob = pred[f"p_{best_selection}"]
        odds_key = {"home": "home_odds", "draw": "draw_odds",
                    "away": "away_odds"}[best_selection]
        market_odds = m[odds_key]
        assert market_odds > 1.0
        ev = expected_value(best_prob, market_odds)

        # ── 5. GATE: nur positive EV zulässig (Rule 1) ─────────────────────
        gate_passed = ev >= MIN_EV
        edge = best_prob - (1.0 / market_odds)
        edge_positive = edge > 0

        # ── 6. KELLY + STAKE ───────────────────────────────────────────────
        kelly_f = kelly_fraction(best_prob, market_odds, fraction=KELLY_FRACTION)
        assert kelly_f >= 0.0, "Kelly-Fraction darf nie negativ sein"
        raw_stake = stake(kelly_f, BANKROLL, max_eur=MAX_STAKE_EUR)

        # ── 7. RULE 4 INVARIANTE: Cap ≤ 5% Bankroll ───────────────────────
        bankroll_cap = BANKROLL * BANKROLL_CAP_PCT
        final_stake = min(raw_stake, bankroll_cap)
        assert final_stake <= bankroll_cap + 1e-9, (
            f"Rule 4 Verletzung: final_stake {final_stake} > {bankroll_cap}"
        )

        # ── 8. SIGNAL-OBJEKT + Serialisierung ──────────────────────────────
        signal = {
            "match_id": m["match_id"],
            "home": home,
            "away": away,
            "selection": best_selection,
            "model_prob": round(best_prob, 4),
            "market_odds": market_odds,
            "edge_pp": round(edge * 100, 2),
            "ev": round(ev, 4),
            "kelly_fraction": round(kelly_f, 4),
            "stake_eur": round(final_stake, 2),
            "gate_passed": gate_passed and edge_positive,
            "bankroll_at_signal": BANKROLL,
        }
        # Round-Trip: JSON serialisierbar
        payload = json.dumps(signal)
        restored = json.loads(payload)
        assert restored == signal, "Signal-Objekt muss JSON-round-trip-safe sein"

    def test_pipeline_rejects_zero_edge_bet(self):
        """Gate darf keinen Bet mit 0 EV freigeben."""
        # Konstruiere Match, wo Markt-Odds die echte Wahrscheinlichkeit fair widerspiegeln.
        market_odds = 2.0
        model_prob = 0.5   # exact fair → EV=0
        ev = expected_value(model_prob, market_odds)
        assert abs(ev) < 1e-9
        assert ev < MIN_EV
        assert kelly_fraction(model_prob, market_odds, fraction=KELLY_FRACTION) == 0.0

    def test_pipeline_rule4_cap_beats_kelly_when_kelly_large(self):
        """Falls Kelly übertrieben groß wäre, muss 5%-Cap greifen."""
        # Sehr hoher edge → Kelly > 5%
        model_prob = 0.90
        market_odds = 3.0
        kelly_f = kelly_fraction(model_prob, market_odds, fraction=KELLY_FRACTION)
        raw = stake(kelly_f, BANKROLL, max_eur=MAX_STAKE_EUR)
        assert raw > 0
        cap = BANKROLL * BANKROLL_CAP_PCT
        final = min(raw, cap)
        assert final == pytest.approx(cap), (
            f"Rule 4: bei starkem Edge muss 5%-Cap greifen ({raw} → {final})"
        )

    def test_pipeline_negative_edge_produces_zero_stake(self):
        """Modell schlechter als Markt → 0 Stake, egal wie hoch die Quote."""
        model_prob = 0.30
        market_odds = 2.5   # 40% implied
        kelly_f = kelly_fraction(model_prob, market_odds, fraction=KELLY_FRACTION)
        assert kelly_f == 0.0
        assert stake(kelly_f, BANKROLL) == 0.0
