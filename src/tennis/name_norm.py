"""Tennis-Spielernamen kanonisieren.

Drei Formate in unserem Stack:
  1. TheOddsAPI:     "Ben Shelton"           (Vorname Nachname)
  2. Tennisexplorer: "Shelton Ben"           (Nachname Vorname)
  3. tennis-data.co.uk (Elo-Storage): "Shelton B."   (Nachname Initial.)

Kanonische Form fürs Elo-Lookup: `"Shelton B."` (Format der Trainings-Daten).

Public API:
    to_elo_name(name: str, format_hint: str | None = None) -> str
    is_probably_elo_format(name: str) -> bool
"""
from __future__ import annotations

import re

_MULTI_WORD_LASTNAMES = {
    # Nachnamen mit Präfix (bleiben zusammen).
    "de", "del", "della", "di", "van", "von", "der", "den", "la", "le",
    "da", "dos", "santos", "st",
}

_INITIAL_RE = re.compile(r"^([A-Z])\.?$")


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"\s+", (name or "").strip()) if t]


def is_probably_elo_format(name: str) -> bool:
    """True wenn Name im 'Nachname X.'-Format ist (letztes Token = Initial)."""
    toks = _tokens(name)
    if len(toks) < 2:
        return False
    return bool(_INITIAL_RE.match(toks[-1].rstrip(".") + "."))


def to_elo_name(name: str) -> str:
    """Normalisiert beliebigen Spielernamen zum Elo-Storage-Format 'Nachname X.'.

    Regeln:
      - Wenn schon Elo-Format → unverändert
      - "Ben Shelton" → "Shelton B."
      - "Shelton Ben" → "Shelton B." (letztes Token = Vorname wenn Länge > 1)
      - Multi-Word-Nachnamen: "Alex de Minaur" → "De Minaur A.",
        "De Minaur Alex" → "De Minaur A."
      - Klammer-Zusätze wie "(2008)" gestrippt
    """
    if not name:
        return ""
    cleaned = re.sub(r"\([^)]*\)", "", name).strip()
    if not cleaned:
        return ""
    if is_probably_elo_format(cleaned):
        toks = _tokens(cleaned)
        # Sicherstellen dass letzter Token als "X." formatiert ist
        toks[-1] = toks[-1].rstrip(".") + "."
        return " ".join(toks)

    toks = _tokens(cleaned)
    if len(toks) == 1:
        return toks[0]

    # Heuristik: Vorname vs. Nachname erkennen.
    # Wenn ERSTER Token länger UND letzter Token kurz (typisch Vorname) → "Nachname Vorname"
    # Wenn LETZTER Token länger → "Vorname Nachname"
    first_is_short = len(toks[0]) <= 2
    last_is_short = len(toks[-1]) <= 2

    # Multi-word lastname handling
    lower_toks = [t.lower() for t in toks]
    # "Alex de Minaur" (VN Präfix NN) → firstname=Alex, lastname=de Minaur
    if len(toks) >= 3 and lower_toks[1] in _MULTI_WORD_LASTNAMES:
        firstname = toks[0]
        lastname = " ".join(toks[1:])
        return f"{_titlecase_lastname(lastname)} {firstname[0].upper()}."

    # "De Minaur Alex" (Präfix NN VN) → lastname="De Minaur", firstname="Alex"
    if len(toks) >= 3 and lower_toks[0] in _MULTI_WORD_LASTNAMES:
        firstname = toks[-1]
        lastname = " ".join(toks[:-1])
        return f"{_titlecase_lastname(lastname)} {firstname[0].upper()}."

    # 2-Token-Fall
    # TE-Format "Shelton Ben": letztes Token = Vorname (typischerweise 2-8 Zeichen)
    # TheOddsAPI "Ben Shelton": erstes Token = Vorname
    # Heuristik: wenn beide etwa gleich lang, nutze das Token mit mehr Vokalen als Vorname
    # Einfachere Regel: Vorname ist typischerweise kürzer, Nachname länger. Wenn ambig →
    # nimm ERSTES Token als Nachname (TheOddsAPI-Fall am häufigsten inzwischen).
    if last_is_short and not first_is_short:
        # "Shelton B." wäre schon Elo-Format (oben abgefangen). Also z.B. "Shelton Al"
        firstname = toks[-1]
        lastname = " ".join(toks[:-1])
    elif first_is_short and not last_is_short:
        firstname = toks[0]
        lastname = " ".join(toks[1:])
    else:
        # Beide Tokens "normal". Default: TE-Format (Nachname zuerst), weil TheOddsAPI
        # Format bereits vor to_elo_name konvertiert werden sollte via format_hint.
        # Ohne Hint: TheOddsAPI-Convention (Vorname zuerst) — sicherer default.
        firstname = toks[0]
        lastname = " ".join(toks[1:])

    return f"{_titlecase_lastname(lastname)} {firstname[0].upper()}."


def _titlecase_lastname(lastname: str) -> str:
    """'de minaur' → 'De Minaur', 'DE MINAUR' → 'De Minaur'."""
    return " ".join(w.capitalize() for w in lastname.split())


def to_elo_name_from_te(te_name: str) -> str:
    """Explizit für TE-Format 'Nachname Vorname'. Nutzt Position statt Heuristik."""
    cleaned = re.sub(r"\([^)]*\)", "", te_name or "").strip()
    if not cleaned:
        return ""
    toks = _tokens(cleaned)
    if len(toks) == 1:
        return toks[0]

    lower = [t.lower() for t in toks]
    # Multi-Word-Nachname am Anfang: "De Minaur Alex"
    if len(toks) >= 3 and lower[0] in _MULTI_WORD_LASTNAMES:
        firstname = toks[-1]
        lastname = " ".join(toks[:-1])
    else:
        # Standard TE: letztes Token = Vorname
        firstname = toks[-1]
        lastname = " ".join(toks[:-1])
    return f"{_titlecase_lastname(lastname)} {firstname[0].upper()}."


def to_elo_name_from_odds_api(name: str) -> str:
    """Explizit für TheOddsAPI-Format 'Vorname Nachname'. Nutzt Position."""
    cleaned = re.sub(r"\([^)]*\)", "", name or "").strip()
    if not cleaned:
        return ""
    toks = _tokens(cleaned)
    if len(toks) == 1:
        return toks[0]
    lower = [t.lower() for t in toks]
    # Multi-Word-Nachname nach Vorname: "Alex de Minaur"
    if len(toks) >= 3 and lower[1] in _MULTI_WORD_LASTNAMES:
        firstname = toks[0]
        lastname = " ".join(toks[1:])
    else:
        firstname = toks[0]
        lastname = " ".join(toks[1:])
    return f"{_titlecase_lastname(lastname)} {firstname[0].upper()}."
