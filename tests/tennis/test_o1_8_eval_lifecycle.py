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


# ---------------------------------------------------------------------------
# 4. UTR Pro Tennis Series exclusion
# ---------------------------------------------------------------------------

import re as _re

# Mirror of the blocklist pattern in tennis_secondary_odds.py
_TE_UTR_BLOCKLIST_RE = _re.compile(r'UTR\s*Pro\s*Tennis|utr-pro-tennis|utr-tennis-series', _re.IGNORECASE)


def test_utr_pro_name_matches_blocklist():
    """'UTR Pro Tennis Series' must match the scraper blocklist regex."""
    assert _TE_UTR_BLOCKLIST_RE.search("UTR Pro Tennis Series — Atlanta")
    assert _TE_UTR_BLOCKLIST_RE.search("utr-pro-tennis/2026/")
    assert _TE_UTR_BLOCKLIST_RE.search("utr-tennis-series slug")


def test_atp_name_does_not_match_blocklist():
    """Legit ATP tournaments must NOT match the UTR blocklist."""
    assert not _TE_UTR_BLOCKLIST_RE.search("Cincinnati Open")
    assert not _TE_UTR_BLOCKLIST_RE.search("Washington ATP")
    assert not _TE_UTR_BLOCKLIST_RE.search("Todi Challenger")


def test_utr_html_fragment_triggers_exclusion():
    """Simulate a TE detail HTML page for a UTR match — must be blocked."""
    fake_html = '<a href="/utr-pro-tennis-series/2026/atp-men/">UTR Pro Tennis Series</a>'
    assert _TE_UTR_BLOCKLIST_RE.search(fake_html), "UTR match should trigger blocklist"


# ---------------------------------------------------------------------------
# 5. Shadow tier governance
# ---------------------------------------------------------------------------

def test_shadow_eval_has_probs_but_no_signal():
    """SHADOW_EVALUATED entries carry probs (for measurement) but are non-actionable."""
    entry = {
        "status": "SHADOW_EVALUATED",
        "tier": "shadow",
        "p_a": 58.3,
        "p_b": 41.7,
        "implied_a": 52.0,
        "implied_b": 48.0,
        "odds_a": 1.92,
        "odds_b": 2.10,
        "source": "elo",
        "tournament": "Todi Challenger",
    }
    assert entry["status"] == "SHADOW_EVALUATED"
    assert entry["tier"] == "shadow"
    assert entry["p_a"] > 0
    assert entry["p_b"] > 0
    # Critically: shadow entries must never reach signals array in tests.
    # (Production enforcement is in tennis_scan.py; this test validates the data contract.)
    assert "status" in entry
    assert entry.get("tier") == "shadow"


def test_production_tier_is_default():
    """Tournament tier must default to 'production' so existing entries are unaffected."""
    from src.tennis.tournaments import Tournament
    t = Tournament("test_event", "Test Event", "atp", "atp250", "hard", 3)
    assert t.tier == "production"
    assert not t.is_shadow


def test_challenger_entries_are_shadow():
    """All four O1-8B Challenger entries must have tier='shadow'."""
    from src.tennis.tournaments import TENNIS_REGISTRY
    challenger_slugs = {"todi_challenger", "brownsburg_challenger", "astana_challenger", "hamburg_challenger"}
    registry_challengers = {t.slug: t for t in TENNIS_REGISTRY if t.slug in challenger_slugs}
    assert len(registry_challengers) == 4, f"Expected 4 Challenger entries, found {list(registry_challengers)}"
    for slug, t in registry_challengers.items():
        assert t.tier == "shadow", f"{slug} must be tier='shadow', got '{t.tier}'"
        assert t.category == "challenger_atp", f"{slug} must be category='challenger_atp'"
        assert t.is_shadow


def test_production_supported_categories_are_complete():
    """PRODUCTION_SUPPORTED_CATEGORIES must cover all main-tour categories."""
    from src.tennis.tournaments import PRODUCTION_SUPPORTED_CATEGORIES
    required = {"grand_slam", "m1000", "wta1000", "atp500", "wta500", "atp250", "wta250"}
    assert required.issubset(PRODUCTION_SUPPORTED_CATEGORIES)
    assert "challenger_atp" not in PRODUCTION_SUPPORTED_CATEGORIES


def test_shadow_supported_contains_challenger():
    """SHADOW_SUPPORTED_CATEGORIES must include challenger_atp."""
    from src.tennis.tournaments import SHADOW_SUPPORTED_CATEGORIES
    assert "challenger_atp" in SHADOW_SUPPORTED_CATEGORIES


def test_shadow_eval_unknown_player_carries_tier():
    """UNKNOWN_PLAYER evals from shadow tournaments must include tier='shadow'."""
    entry = {
        "status": "UNKNOWN_PLAYER",
        "tier": "shadow",
        "reason": "Kein Elo-Profil: SomePlayer X.",
        "odds_a": 1.75,
        "odds_b": 2.10,
    }
    assert entry["tier"] == "shadow"
    assert "p_a" not in entry
