"""Tests für 2.BL Team-Name-Normierung (Umlaute, Abkürzungen, Bindestriche)."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config import canonical_name


ALIAS_CASES = [
    # Abkürzungen
    ("HSV", "Hamburg"),
    ("KSC", "Karlsruhe"),
    ("FCK", "Kaiserslautern"),
    ("SVS", "Sandhausen"),
    # Umlaute
    ("Greuther Fürth", "Greuther Furth"),
    ("SpVgg Greuther Fürth", "Greuther Furth"),
    ("Fürth", "Greuther Furth"),
    ("Fortuna Düsseldorf", "Fortuna Dusseldorf"),
    ("Düsseldorf", "Fortuna Dusseldorf"),
    ("1. FC Köln", "FC Koln"),
    ("Köln", "FC Koln"),
    ("FC Köln", "FC Koln"),
    # Vollnamen
    ("1. FC Kaiserslautern", "Kaiserslautern"),
    ("1. FC Magdeburg", "Magdeburg"),
    ("FC St. Pauli", "St Pauli"),
    ("Hamburger SV", "Hamburg"),
    # Kleinschreibung (case-insensitive fallback)
    ("hamburger sv", "Hamburg"),
    ("greuther fürth", "Greuther Furth"),
    ("ksc", "Karlsruhe"),
]


@pytest.mark.parametrize("raw,expected", ALIAS_CASES)
def test_alias(raw: str, expected: str) -> None:
    assert canonical_name(raw) == expected, f"canonical_name({raw!r}) sollte {expected!r} sein"


def test_idempotent() -> None:
    """canonical_name zweimal angewendet = gleicher Wert."""
    for raw, expected in ALIAS_CASES:
        first = canonical_name(raw)
        assert canonical_name(first) == first, f"Nicht idempotent: {raw!r} → {first!r} → {canonical_name(first)!r}"


def test_unknown_team_passthrough() -> None:
    """Unbekanntes Team wird unverändert durchgereicht."""
    result = canonical_name("Unbekannter FC")
    assert result == "Unbekannter FC"


def test_empty_string() -> None:
    assert canonical_name("") == ""
