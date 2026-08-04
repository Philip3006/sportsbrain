"""Player Biometrics Loader (Roadmap Phase 3 Detail-Daten).

Zieht `atp_players.csv` / `wta_players.csv` aus Sackmann-Repos.
Nutzt Player-Name → Bio-Lookup (height cm, hand L/R/U, geburtsjahr) für
Feature-Engineering (age_diff, height_diff, hand_matchup).

Cache: 30 Tage (Bios ändern sich selten).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from scripts._http_retry import retry_request
from src.data.cache import disk_cache

_ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv"
_WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_players.csv"


@dataclass(frozen=True)
class PlayerBio:
    name: str
    height_cm: Optional[int]
    hand: str            # 'L' | 'R' | 'U' (unknown)
    dob: Optional[str]   # YYYYMMDD
    ioc: str = "UNK"     # IOC 3-letter country code (Sackmann CSV)


def _empty_bio(name: str) -> PlayerBio:
    return PlayerBio(name=name, height_cm=None, hand="U", dob=None, ioc="UNK")


def _canon(name: str) -> str:
    return " ".join(str(name).lower().split())


def _fetch_bios(url: str) -> dict[str, PlayerBio]:
    try:
        resp = retry_request("GET", url, timeout=15)
        if not resp.ok:
            return {}
        df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    except Exception:
        return {}

    # Sackmann-Schema: player_id, name_first, name_last, hand, dob, ioc, height, wikidata_id
    out: dict[str, PlayerBio] = {}
    for _, row in df.iterrows():
        first = str(row.get("name_first", "")).strip()
        last = str(row.get("name_last", "")).strip()
        if not first or not last:
            continue
        full = f"{first} {last}"
        h = row.get("height")
        try:
            height = int(h) if pd.notna(h) and h else None
        except Exception:
            height = None
        hand = str(row.get("hand", "U")).strip().upper() or "U"
        if hand not in ("L", "R", "U"):
            hand = "U"
        dob_val = row.get("dob")
        dob = None
        if pd.notna(dob_val):
            try:
                dob = str(int(dob_val))
            except Exception:
                dob = str(dob_val)
        ioc_raw = row.get("ioc", "")
        ioc = str(ioc_raw).strip().upper() if pd.notna(ioc_raw) and ioc_raw else "UNK"
        bio = PlayerBio(name=full, height_cm=height, hand=hand, dob=dob, ioc=ioc)
        out[_canon(full)] = bio
        out[_canon(f"{last} {first}")] = bio  # WTA/TE-Format
    return out


@disk_cache("tennis_atp_bios", max_age_hours=720.0)
def fetch_atp_bios() -> dict[str, PlayerBio]:
    return _fetch_bios(_ATP_URL)


@disk_cache("tennis_wta_bios", max_age_hours=720.0)
def fetch_wta_bios() -> dict[str, PlayerBio]:
    return _fetch_bios(_WTA_URL)


def lookup_bio(name: str, tour: str = "atp") -> PlayerBio:
    bios = fetch_wta_bios() if tour.lower() == "wta" else fetch_atp_bios()
    return bios.get(_canon(name), _empty_bio(name))


def age_years(bio: PlayerBio, reference: Optional[date] = None) -> Optional[float]:
    if not bio.dob or len(bio.dob) < 8:
        return None
    try:
        yyyy = int(bio.dob[:4]); mm = int(bio.dob[4:6]); dd = int(bio.dob[6:8])
        born = date(yyyy, mm, dd)
    except Exception:
        return None
    ref = reference or date.today()
    return round((ref - born).days / 365.25, 2)


def hand_matchup_code(a: str, b: str) -> int:
    """RR=0, RL=1, LR=2, LL=3, U*=−1. Simple ordinal for LGBM."""
    order = {"R": 0, "L": 1, "U": 2}
    if a not in order or b not in order:
        return -1
    if a == "U" or b == "U":
        return -1
    return order[a] * 2 + order[b]  # {RR:0, RL:1, LR:2, LL:3}
