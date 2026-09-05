"""FLAGSHIP-BL1-001 — Raw CSV downloader (hybrid direct + archive.org fallback).

football-data.co.uk was 503'ing under load 2026-09-06. archive.org has
usable captures of the same D1.csv files. Try direct first; fall back to
the most recent archive.org capture for that season on 503/timeout.

Isolated worktree. No production writes.
"""
from __future__ import annotations

import io
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from src.config import DATA_CACHE, FBDATA_BASE, canonical_name  # noqa: E402

SEASONS = ["1617", "1718", "1819", "1920", "2021",
           "2122", "2223", "2324", "2425", "2526"]

RAW_DIR = ROOT / "research" / "bl1" / "data"
RAW_DIR.mkdir(parents=True, exist_ok=True)
DATA_CACHE.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
KEEP = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG",
        "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA"}
RENAME = {
    "Date": "date", "HomeTeam": "home_team", "AwayTeam": "away_team",
    "FTHG": "home_score", "FTAG": "away_score",
    "PSH": "ps_open_home", "PSD": "ps_open_draw", "PSA": "ps_open_away",
    "PSCH": "ps_close_home", "PSCD": "ps_close_draw", "PSCA": "ps_close_away",
}


def _direct_url(season: str) -> str:
    return f"{FBDATA_BASE}/{season}/D1.csv"


def _archive_url(season: str) -> str:
    # Redirect endpoint — chooses the nearest capture. We prefer a snapshot
    # near season-end (May+1) to maximise closing-odds completeness.
    year_end = int("20" + season[2:4]) if int(season[:2]) < 90 else int("19" + season[2:4])
    stamp = f"{year_end + 1}0601"  # e.g. season 2324 -> 20240601
    return f"https://web.archive.org/web/{stamp}/{_direct_url(season)}"


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


def _download(season: str, session: requests.Session) -> bytes | None:
    # Try direct with a couple of retries; if still 503, fall back to archive.
    for attempt in range(2):
        print(f"[{season}] direct attempt {attempt+1}/2 ...", flush=True)
        content = _try_get(_direct_url(season), session)
        if content is not None:
            print(f"[{season}] direct OK {len(content):,}b", flush=True)
            return content
        time.sleep(8)
    print(f"[{season}] direct exhausted -> archive.org", flush=True)
    content = _try_get(_archive_url(season), session)
    if content is not None:
        print(f"[{season}] archive OK {len(content):,}b", flush=True)
        return content
    return None


def _to_frame(content: bytes) -> pd.DataFrame:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    df = pd.read_csv(io.StringIO(text), low_memory=False)
    existing = KEEP & set(df.columns)
    df = df[list(existing)].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.rename(columns={k: v for k, v in RENAME.items() if k in df.columns})
    df["home_team"] = df["home_team"].map(canonical_name)
    df["away_team"] = df["away_team"].map(canonical_name)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def main() -> None:
    session = requests.Session()
    ok, missing = [], []
    for i, season in enumerate(SEASONS):
        content = _download(season, session)
        if content is None:
            missing.append(season)
            continue
        raw_path = RAW_DIR / f"D1_{season}.csv"
        raw_path.write_bytes(content)
        df = _to_frame(content)
        cache_path = DATA_CACHE / f"fd_D1_{season}.pkl"
        with open(cache_path, "wb") as f:
            pickle.dump(df, f)
        print(f"[{season}] rows={len(df)} cache={cache_path}", flush=True)
        ok.append(season)
        if i < len(SEASONS) - 1:
            time.sleep(3)
    print(f"\nOK: {ok}\nMISSING: {missing}", flush=True)


if __name__ == "__main__":
    main()
