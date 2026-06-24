"""Tests für tournament_meta in signals.json (Roadmap J2-E)."""
from __future__ import annotations

import json

from src.betting.value_detector import BetSignal
from src.notifications.web_dashboard import _signal_to_dict, write_signals_json


def _make_signal(match_id="m1", market="home"):
    return BetSignal(
        match_id=match_id, home="Alcaraz", away="Sinner",
        market=market, model_prob=0.55, fair_prob=0.50,
        decimal_odds=2.00, ev=0.10, kelly_f=0.05,
        stake_pct=0.05, confidence="MEDIUM", stake_eur=5.0,
    )


def test_signal_to_dict_without_tournament_meta_no_extra_keys():
    """Football-Signals (kein meta) bekommen keine tennis-spezifischen Felder."""
    d = _signal_to_dict(_make_signal(), sport="football")
    assert "tournament" not in d
    assert "category" not in d
    assert "surface" not in d
    assert "best_of" not in d


def test_signal_to_dict_with_tournament_meta_adds_fields():
    meta = {"name": "Wimbledon", "category": "grand_slam",
            "surface": "grass", "best_of": 5}
    d = _signal_to_dict(_make_signal(), sport="tennis", tour="atp",
                         tournament_meta=meta)
    assert d["tournament"] == "Wimbledon"
    assert d["category"] == "grand_slam"
    assert d["surface"] == "grass"
    assert d["best_of"] == 5
    assert d["tour"] == "atp"


def test_signal_to_dict_partial_meta_handles_missing_keys():
    """Robust gegen unvollständige Meta-Dicts."""
    meta = {"name": "Mock Event"}
    d = _signal_to_dict(_make_signal(), sport="tennis", tournament_meta=meta)
    assert d["tournament"] == "Mock Event"
    assert d["category"] == ""    # Default
    assert d["surface"] == ""
    assert d["best_of"] == 0


def test_write_signals_json_persists_tournament_meta(tmp_path, monkeypatch):
    """End-to-End: write_signals_json schreibt tournament_meta in JSON."""
    # ROOT umbiegen, damit json_path in tmp landet
    import src.notifications.web_dashboard as wd
    monkeypatch.setattr(wd, "ROOT", tmp_path)
    (tmp_path / "docs" / "data").mkdir(parents=True)

    sig = _make_signal("alc_sin")
    write_signals_json(
        tennis=[sig],
        tennis_tour_map={"alc_sin": "atp"},
        tennis_tournament_map={
            "alc_sin": {"name": "Wimbledon", "category": "grand_slam",
                        "surface": "grass", "best_of": 5}
        },
        user="philip",
    )

    out = tmp_path / "docs" / "data" / "signals_philip.json"
    assert out.exists()
    data = json.loads(out.read_text())
    tennis_signals = data.get("tennis", [])
    assert len(tennis_signals) == 1
    s = tennis_signals[0]
    assert s["tournament"] == "Wimbledon"
    assert s["category"] == "grand_slam"
    assert s["surface"] == "grass"
    assert s["best_of"] == 5
