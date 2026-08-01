"""J8-B3: canonical Match-Key verhindert dauer-„pending" bei Namens-Drift.

Regression-Guard für Bosnia-Analog: Whitespace, Unicode-Marks, Case-Drift zwischen
Score-Fetcher-Payload und Ledger-Zeilen dürfen Settlement nicht mehr blockieren.
"""
from __future__ import annotations

from src.data.tennis_scores import canonical_match_key


def test_case_and_whitespace_drift_matches():
    assert canonical_match_key("Jannik Sinner", "Carlos Alcaraz") == \
           canonical_match_key("  jannik   sinner  ", "CARLOS ALCARAZ")


def test_order_swap_matches():
    a = canonical_match_key("Sinner", "Alcaraz")
    b = canonical_match_key("Alcaraz", "Sinner")
    assert a == b


def test_unicode_marks_stripped():
    assert canonical_match_key("Stéfanos Tsitsipás", "Novak Djoković") == \
           canonical_match_key("Stefanos Tsitsipas", "Novak Djokovic")


def test_hyphen_and_dot_ignored():
    assert canonical_match_key("J. Sinner", "C. Alcaraz") == \
           canonical_match_key("J Sinner", "C-Alcaraz")


def test_distinct_matches_stay_distinct():
    assert canonical_match_key("Sinner", "Alcaraz") != canonical_match_key("Sinner", "Djokovic")


def test_empty_input_safe():
    assert canonical_match_key("", "") == "|"
    assert canonical_match_key("A", "") == "|a"


def test_settle_uses_canonical_fallback(monkeypatch, tmp_path):
    """End-to-End-Style: Ledger hat Namen mit Whitespace; Score-Dict nur canonical key."""
    import csv
    from scripts import tennis_settle

    # Fake-Ledger
    ledger = tmp_path / "ledger_testuser.csv"
    with ledger.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "match_id", "home", "away", "market", "decimal_odds", "stake_amount",
            "status", "pnl", "closing_odds", "clv", "source", "stake_reason",
        ])
        w.writeheader()
        w.writerow({
            "match_id": "unknown_id",
            "home": "  Carlos   Alcaraz  ",
            "away": "JANNIK SINNER",
            "market": "first_set_a",
            "decimal_odds": "2.0",
            "stake_amount": "10",
            "status": "open",
            "pnl": "",
            "closing_odds": "",
            "clv": "",
            "source": "tennis",
            "stake_reason": "",
        })

    monkeypatch.setattr(tennis_settle, "ledger_path_for", lambda u: ledger)
    # Score-Dict nur unter canonical key
    scores = {
        canonical_match_key("Carlos Alcaraz", "Jannik Sinner"): {
            "player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
            "status": "completed", "sets": [(6, 4), (6, 3)],
            "winner": "a", "retired_by": None, "best_of": 3,
        }
    }
    # Kein Push
    n = tennis_settle._settle_user_ledger("testuser", scores, set(), dry_run=True, no_push=True)
    assert n == 1  # Match wurde gefunden dank canonical fallback
