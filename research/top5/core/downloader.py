"""Top-5 — Generic Football-Data.co.uk downloader.

Parameterized on the FDU league code (D1, E0, SP1, I1, F1).

Direct fetch first; archive.org fallback on 503/timeout (mirrors BL1's
00_download_raw.py behavior).
"""
from __future__ import annotations

import hashlib
import io
import pickle
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.config import FBDATA_BASE, canonical_name  # noqa: E402

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Columns we keep from the raw FDU CSV. Includes pre-close + closing bookmaker
# aggregate + Bet365 + Pinnacle, plus post-match scores + in-match stats.
KEEP_MIN = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
KEEP_FULL_EXTRAS = {
    # Pinnacle
    "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA",
    # Bookmaker aggregates
    "AvgH", "AvgD", "AvgA", "AvgCH", "AvgCD", "AvgCA",
    "MaxH", "MaxD", "MaxA", "MaxCH", "MaxCD", "MaxCA",
    # Bet365
    "B365H", "B365D", "B365A", "B365CH", "B365CD", "B365CA",
    # Additional bookies (pre-close only; needed for M5 research benchmark)
    "BWH", "BWD", "BWA", "IWH", "IWD", "IWA", "WHH", "WHD", "WHA",
    "VCH", "VCD", "VCA", "LBH", "LBD", "LBA",
    # Post-match stats
    "FTR", "HTR", "HTHG", "HTAG", "HS", "AS", "HST", "AST",
}

_RENAME_MIN = {
    "Date": "date", "HomeTeam": "home_team", "AwayTeam": "away_team",
    "FTHG": "home_score", "FTAG": "away_score",
    "PSH": "ps_open_home", "PSD": "ps_open_draw", "PSA": "ps_open_away",
    "PSCH": "ps_close_home", "PSCD": "ps_close_draw", "PSCA": "ps_close_away",
}


def _direct_url(league_code: str, season: str) -> str:
    return f"{FBDATA_BASE}/{season}/{league_code}.csv"


def _archive_url(league_code: str, season: str) -> str:
    year_end = int("20" + season[2:4]) if int(season[:2]) < 90 else int("19" + season[2:4])
    stamp = f"{year_end + 1}0601"
    return f"https://web.archive.org/web/{stamp}/{_direct_url(league_code, season)}"


def _try_get(url: str, session: requests.Session) -> bytes | None:
    try:
        resp = session.get(url, headers={"User-Agent": UA, "Accept": "*/*"},
                           timeout=45, allow_redirects=True)
    except requests.RequestException as e:
        print(f"    err: {e}", flush=True)
        return None
    if resp.status_code == 200 and len(resp.content) > 500:
        return resp.content
    print(f"    http {resp.status_code} len={len(resp.content)}", flush=True)
    return None


def _download(league_code: str, season: str, session: requests.Session) -> bytes | None:
    for attempt in range(2):
        print(f"[{league_code}/{season}] direct attempt {attempt+1}/2", flush=True)
        c = _try_get(_direct_url(league_code, season), session)
        if c is not None:
            print(f"[{league_code}/{season}] direct OK {len(c):,}b", flush=True)
            return c
        time.sleep(6)
    print(f"[{league_code}/{season}] direct exhausted -> archive.org", flush=True)
    c = _try_get(_archive_url(league_code, season), session)
    if c is not None:
        print(f"[{league_code}/{season}] archive OK {len(c):,}b", flush=True)
        return c
    return None


def _decode(content: bytes) -> pd.DataFrame:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    return pd.read_csv(io.StringIO(text), low_memory=False)


def _clean_min(df: pd.DataFrame) -> pd.DataFrame:
    keep = KEEP_MIN & set(df.columns)
    df = df[list(keep)].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.rename(columns={k: v for k, v in _RENAME_MIN.items() if k in df.columns})
    df["home_team"] = df["home_team"].map(canonical_name)
    df["away_team"] = df["away_team"].map(canonical_name)
    return df.sort_values("date").reset_index(drop=True)


def _clean_full(df: pd.DataFrame) -> pd.DataFrame:
    """Preserve all FDU market + stats columns (not the truncated 'min' set)."""
    keep = (KEEP_MIN | KEEP_FULL_EXTRAS) & set(df.columns)
    df = df[list(keep)].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.rename(columns={k: v for k, v in _RENAME_MIN.items() if k in df.columns})
    df["home_team"] = df["home_team"].map(canonical_name)
    df["away_team"] = df["away_team"].map(canonical_name)
    return df.sort_values("date").reset_index(drop=True)


def download_league(league_code: str, seasons: tuple[str, ...],
                    raw_dir: Path, sleep_between: int = 3) -> dict:
    """Download every season for a league. Writes per-season CSVs + returns
    a summary dict with rows/hash/status per season."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    audit: dict = {"league_code": league_code, "seasons": {}}
    for i, season in enumerate(seasons):
        content = _download(league_code, season, session)
        entry: dict = {"season": season}
        if content is None:
            entry.update({"status": "MISSING", "rows": 0, "sha256": None})
            audit["seasons"][season] = entry
            continue
        raw_path = raw_dir / f"{league_code}_{season}.csv"
        raw_path.write_bytes(content)
        entry.update({
            "status": "OK",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "path": str(raw_path.relative_to(raw_dir.parent.parent)),
        })
        # Also try to parse rows count
        try:
            df = _decode(content)
            entry["rows_raw"] = int(len(df))
            entry["cols_raw"] = list(df.columns)
        except Exception as e:  # noqa: BLE001
            entry["parse_error"] = str(e)
        audit["seasons"][season] = entry
        if i < len(seasons) - 1:
            time.sleep(sleep_between)
    return audit


def build_pickles(league_code: str, seasons: tuple[str, ...],
                   raw_dir: Path, out_min: Path, out_full: Path) -> dict:
    """Read the per-season CSVs written by download_league, produce two
    pickles matching BL1's layout:
      - {key}_raw.pkl          minimal columns (M1/M2/M4-friendly)
      - {key}_raw_full.pkl     full market + stats columns (M5/M6/M7)
    Returns a build audit summary.
    """
    frames_min, frames_full, audit = [], [], {"seasons": {}}
    for season in seasons:
        csv_path = raw_dir / f"{league_code}_{season}.csv"
        if not csv_path.exists():
            audit["seasons"][season] = {"status": "MISSING"}
            continue
        raw = _decode(csv_path.read_bytes())
        d_min = _clean_min(raw)
        d_full = _clean_full(raw)
        d_min["season"] = season
        d_full["season"] = season
        entry = {
            "status": "OK",
            "rows_min": int(len(d_min)),
            "rows_full": int(len(d_full)),
            "cols_full": list(d_full.columns),
        }
        # Coverage per source
        for name, cols in (
            ("Pinnacle_pre", ("ps_open_home", "ps_open_draw", "ps_open_away")),
            ("Pinnacle_close", ("ps_close_home", "ps_close_draw", "ps_close_away")),
            ("Avg_pre", ("AvgH", "AvgD", "AvgA")),
            ("Avg_close", ("AvgCH", "AvgCD", "AvgCA")),
            ("Max_pre", ("MaxH", "MaxD", "MaxA")),
            ("Max_close", ("MaxCH", "MaxCD", "MaxCA")),
            ("B365_pre", ("B365H", "B365D", "B365A")),
            ("B365_close", ("B365CH", "B365CD", "B365CA")),
        ):
            if all(c in d_full.columns for c in cols):
                entry[f"cov_{name}"] = int(d_full[list(cols)].dropna().shape[0])
            else:
                entry[f"cov_{name}"] = 0
        audit["seasons"][season] = entry
        frames_min.append(d_min)
        frames_full.append(d_full)
    if not frames_min:
        raise RuntimeError(f"no seasons downloaded for {league_code}")
    all_min = pd.concat(frames_min, ignore_index=True).sort_values("date").reset_index(drop=True)
    all_full = pd.concat(frames_full, ignore_index=True).sort_values("date").reset_index(drop=True)
    out_min.parent.mkdir(parents=True, exist_ok=True)
    with open(out_min, "wb") as f:
        pickle.dump(all_min, f)
    with open(out_full, "wb") as f:
        pickle.dump(all_full, f)
    audit["total_rows_min"] = int(len(all_min))
    audit["total_rows_full"] = int(len(all_full))
    audit["out_min"] = str(out_min)
    audit["out_full"] = str(out_full)
    return audit
