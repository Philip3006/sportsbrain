"""Tests für Tennis-Namenskonvertierung."""
from src.tennis.name_norm import (
    to_elo_name, to_elo_name_from_te, to_elo_name_from_odds_api,
    is_probably_elo_format,
)


def test_elo_format_detection():
    assert is_probably_elo_format("Shelton B.")
    assert is_probably_elo_format("Alcaraz C.")
    assert is_probably_elo_format("De Minaur A.")
    assert not is_probably_elo_format("Ben Shelton")
    assert not is_probably_elo_format("Shelton Ben")


def test_elo_already_normalized_passthrough():
    assert to_elo_name("Shelton B.") == "Shelton B."
    assert to_elo_name("Alcaraz C.") == "Alcaraz C."
    assert to_elo_name("Shelton B") == "Shelton B."


def test_odds_api_format_conversion():
    assert to_elo_name_from_odds_api("Ben Shelton") == "Shelton B."
    assert to_elo_name_from_odds_api("Carlos Alcaraz") == "Alcaraz C."
    assert to_elo_name_from_odds_api("Alex de Minaur") == "De Minaur A."
    assert to_elo_name_from_odds_api("Novak Djokovic") == "Djokovic N."


def test_te_format_conversion():
    assert to_elo_name_from_te("Shelton Ben") == "Shelton B."
    assert to_elo_name_from_te("Alcaraz Carlos") == "Alcaraz C."
    assert to_elo_name_from_te("De Minaur Alex") == "De Minaur A."
    assert to_elo_name_from_te("Fritz Taylor") == "Fritz T."


def test_te_strips_bracket_suffix():
    assert to_elo_name_from_te("Ivanov Ivan (2008)") == "Ivanov I."


def test_odds_api_strips_bracket_suffix():
    assert to_elo_name_from_odds_api("Ivan Ivanov (2008)") == "Ivanov I."


def test_empty_input():
    assert to_elo_name("") == ""
    assert to_elo_name_from_te(None) == ""
    assert to_elo_name_from_odds_api("") == ""


def test_single_token():
    assert to_elo_name_from_te("Nadal") == "Nadal"
    assert to_elo_name_from_odds_api("Nadal") == "Nadal"
