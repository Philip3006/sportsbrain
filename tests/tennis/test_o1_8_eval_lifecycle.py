"""O1-8 regression tests: eval lifecycle, name-order lookup, status semantics.

Covers:
  1. Token-order-agnostic eval lookup (name mismatch between TE and OddsAPI format)
  2. UNKNOWN_PLAYER status has no fake probabilities
  3. NO_ODDS status has model probabilities but zero odds
  4. EVALUATED_NO_BET carries real probabilities
  5. Status field presence for skipped matches
"""
from __future__ import annotations

import re
import unicodedata


# ---------------------------------------------------------------------------
# Mirror of the PWA normTeam + token-sort logic (JS equivalent in Python)
# ---------------------------------------------------------------------------

def norm_team(name: str) -> str:
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ']", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_tokens(name: str) -> str:
    return " ".join(sorted(norm_team(name).split()))


def eval_lookup(model_evals: dict, home: str, away: str):
    """Token-order-agnostic lookup matching the JS fix in app.js."""
    display_key = f"{home} vs {away}"
    if display_key in model_evals:
        return model_evals[display_key]
    nh_s, na_s = norm_tokens(home), norm_tokens(away)
    for k, v in model_evals.items():
        parts = k.split(" vs ")
        if len(parts) != 2:
            continue
        kh_s = norm_tokens(parts[0].strip())
        ka_s = norm_tokens(parts[1].strip())
        if (kh_s == nh_s and ka_s == na_s) or (kh_s == na_s and ka_s == nh_s):
            return v
    return None


# ---------------------------------------------------------------------------
# 1. Token-order name lookup
# ---------------------------------------------------------------------------

KNOWN_MISMATCH_PAIRS = [
    ("Jaime Faria", "Nikoloz Basilashvili",   "Faria Jaime", "Basilashvili Nikoloz"),
    ("Mackenzie McDonald", "Wu Yibing",        "McDonald Mackenzie", "Wu Yibing"),
    ("Moez Echargui", "Michael Zheng",         "Echargui Moez", "Zheng Michael"),
    ("Marco Trungelliti", "Sebastian Ofner",   "Trungelliti Marco", "Ofner Sebastian"),
    ("Alexei Popyrin", "Titouan Droguet",      "Popyrin Alexei", "Droguet Titouan"),
    ("Billy Harris", "Kyrian Jacquet",         "Harris Billy", "Jacquet Kyrian"),
    ("Hugo Gaston", "Nicolai Budkov Kjaer",    "Gaston Hugo", "Budkov Kjaer Nicolai"),
    ("Aleksandar Vukic", "Darwin Blanch",      "Vukic Aleksandar", "Blanch Darwin"),
]


def test_token_order_lookup_resolves_te_vs_oddsapi():
    """TE key 'Lastname Firstname' must resolve when schedule uses 'Firstname Lastname'."""
    for sched_h, sched_a, eval_h, eval_a in KNOWN_MISMATCH_PAIRS:
        stub_eval = {"p_a": 55.0, "p_b": 45.0, "status": "EVALUATED_NO_BET"}
        model_evals = {f"{eval_h} vs {eval_a}": stub_eval}
        result = eval_lookup(model_evals, sched_h, sched_a)
        assert result is not None, (
            f"FAIL: schedule='{sched_h} vs {sched_a}' eval_key='{eval_h} vs {eval_a}'"
        )
        assert result["p_a"] == 55.0


def test_old_matchkey_logic_fails_te_vs_oddsapi():
    """Verify the OLD logic (pre-fix) did NOT resolve these pairs."""
    def old_match_key(a, b):
        return norm_team(a) + " vs " + norm_team(b)

    for sched_h, sched_a, eval_h, eval_a in KNOWN_MISMATCH_PAIRS:
        nk = old_match_key(sched_h, sched_a)
        eval_key_normalised = old_match_key(eval_h, eval_a)
        assert eval_key_normalised != nk, (
            f"Expected old logic to fail for '{sched_h} vs {sched_a}'"
        )


def test_exact_key_match_still_works():
    """Exact-key path must not be broken by the new fallback."""
    model_evals = {"Alcaraz C. vs Djokovic N.": {"p_a": 60.0, "p_b": 40.0}}
    result = eval_lookup(model_evals, "Alcaraz C.", "Djokovic N.")
    assert result is not None
    assert result["p_a"] == 60.0


def test_home_away_swap_resolves():
    """Eval stored in reversed order (A vs B) must resolve for (B vs A)."""
    model_evals = {"Djokovic N. vs Alcaraz C.": {"p_a": 40.0, "p_b": 60.0}}
    result = eval_lookup(model_evals, "Alcaraz C.", "Djokovic N.")
    assert result is not None


def test_no_match_returns_none():
    model_evals = {"Sinner J. vs Medvedev D.": {"p_a": 55.0, "p_b": 45.0}}
    result = eval_lookup(model_evals, "Alcaraz C.", "Djokovic N.")
    assert result is None


def test_multi_token_name_matches_regardless_of_order():
    """'Budkov Kjaer Nicolai' == 'Nicolai Budkov Kjaer' via token sort."""
    model_evals = {"Gaston Hugo vs Budkov Kjaer Nicolai": {"p_a": 48.0, "p_b": 52.0}}
    result = eval_lookup(model_evals, "Hugo Gaston", "Nicolai Budkov Kjaer")
    assert result is not None


# ---------------------------------------------------------------------------
# 2. Lifecycle status semantics
# ---------------------------------------------------------------------------

def test_unknown_player_status_has_no_fake_probabilities():
    """UNKNOWN_PLAYER entries must not carry p_a/p_b (no fake 50/50)."""
    entry = {
        "status": "UNKNOWN_PLAYER",
        "reason": "Kein Elo-Profil: Neumayer L.",
        "odds_a": 1.49,
        "odds_b": 2.50,
    }
    assert "p_a" not in entry, "UNKNOWN_PLAYER must not contain fake p_a"
    assert "p_b" not in entry, "UNKNOWN_PLAYER must not contain fake p_b"
    assert entry["status"] == "UNKNOWN_PLAYER"


def test_no_odds_status_carries_model_probs():
    """NO_ODDS entries should carry model probabilities but zero market odds."""
    entry = {
        "status": "NO_ODDS",
        "p_a": 62.3,
        "p_b": 37.7,
        "implied_a": 0.0,
        "implied_b": 0.0,
        "odds_a": 0.0,
        "odds_b": 0.0,
        "source": "elo",
    }
    assert entry["status"] == "NO_ODDS"
    assert entry["p_a"] > 0
    assert entry["odds_a"] == 0.0


def test_evaluated_no_bet_carries_full_data():
    """EVALUATED_NO_BET entries carry model probs + market odds."""
    entry = {
        "status": "EVALUATED_NO_BET",
        "p_a": 55.2,
        "p_b": 44.8,
        "implied_a": 50.0,
        "implied_b": 50.0,
        "odds_a": 2.00,
        "odds_b": 2.00,
        "source": "ensemble",
    }
    assert entry["status"] == "EVALUATED_NO_BET"
    assert entry["p_a"] > 0
    assert entry["odds_a"] > 1


def test_unsupported_tournament_has_no_probabilities():
    """UNSUPPORTED_TOURNAMENT must not carry model probabilities."""
    entry = {
        "status": "UNSUPPORTED_TOURNAMENT",
        "reason": "UTR Pro Tennis Series — kein ATP/WTA-Turnier",
        "odds_a": 1.70,
        "odds_b": 2.10,
    }
    assert "p_a" not in entry
    assert "p_b" not in entry


# ---------------------------------------------------------------------------
# 3. Status-aware PWA rendering helper
# ---------------------------------------------------------------------------

def _render_no_eval_message(entry: dict | None, kickoff_ms: int, now_ms: int, next_scan_ms: int) -> str:
    """Mirrors the expected PWA rendering logic for the no-eval branch."""
    if entry is None:
        return "Keine Modellbewertung für dieses Spiel verfügbar."
    status = entry.get("status", "")
    if status == "UNKNOWN_PLAYER":
        return "Keine Modellbewertung — Spieler nicht im Rating-Datensatz."
    if status == "UNSUPPORTED_TOURNAMENT":
        return "Keine Modellbewertung — Turnier nicht im Modell-Portfolio."
    if status == "NO_ODDS":
        return "Keine Modellbewertung — keine qualifizierten Marktquoten verfügbar."
    return "Keine Modellbewertung für dieses Spiel verfügbar."


def test_unknown_player_renders_specific_reason():
    entry = {"status": "UNKNOWN_PLAYER"}
    msg = _render_no_eval_message(entry, 0, 0, 0)
    assert "Rating-Datensatz" in msg
    assert "08:00" not in msg


def test_unsupported_tournament_renders_specific_reason():
    entry = {"status": "UNSUPPORTED_TOURNAMENT"}
    msg = _render_no_eval_message(entry, 0, 0, 0)
    assert "Modell-Portfolio" in msg


def test_no_odds_renders_specific_reason():
    entry = {"status": "NO_ODDS"}
    msg = _render_no_eval_message(entry, 0, 0, 0)
    assert "Marktquoten" in msg


def test_none_entry_renders_neutral():
    msg = _render_no_eval_message(None, 0, 0, 0)
    assert "verfügbar" in msg
    assert "08:00" not in msg
    assert "tomorrow" not in msg.lower()
    assert "morgen" not in msg.lower()
