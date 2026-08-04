from src.betting.gates import DEFAULT_GATE, GOALSCORER_GATE, TENNIS_GATE, gate_for


def test_default_gate_matches_config():
    from src.config import MIN_EDGE, MAX_EV
    assert DEFAULT_GATE.min_edge == MIN_EDGE
    assert DEFAULT_GATE.max_ev == MAX_EV


def test_tennis_gate_stricter_than_default():
    assert TENNIS_GATE.min_prob >= DEFAULT_GATE.min_prob
    assert TENNIS_GATE.max_odds <= DEFAULT_GATE.max_odds


def test_goalscorer_gate_higher_edge():
    assert GOALSCORER_GATE.min_edge >= DEFAULT_GATE.min_edge * 2


def test_gate_for_dispatch():
    assert gate_for("football") is DEFAULT_GATE
    assert gate_for("tennis") is TENNIS_GATE
    assert gate_for("goalscorer") is GOALSCORER_GATE
    assert gate_for("football", "scorer_neymar") is GOALSCORER_GATE


def test_tennis_category_override():
    g = gate_for("tennis", category="atp")
    assert g.max_odds == TENNIS_GATE.max_odds
    # category-override wenn TENNIS_MIN_EDGE_BY_CATEGORY einen Eintrag hat
    from src.config import TENNIS_MIN_EDGE_BY_CATEGORY
    if "atp" in TENNIS_MIN_EDGE_BY_CATEGORY:
        assert g.min_edge == TENNIS_MIN_EDGE_BY_CATEGORY["atp"]


def test_gate_immutable():
    import dataclasses
    try:
        DEFAULT_GATE.min_edge = 0.5  # type: ignore[misc]
        assert False, "gate should be frozen"
    except dataclasses.FrozenInstanceError:
        pass
