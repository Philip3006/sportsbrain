"""FLAGSHIP-BL1 continuation — Refetch with ALL bookmaker columns.

The Phase-A loader keeps only Pinnacle columns. For the market-benchmark
hierarchy design we need Avg/Max across all bookies at both pre-closing
and closing snapshots. This script parses the raw CSVs saved by
00_download_raw.py and produces a richer consolidated frame.

Keeps all existing per-bookmaker + Avg/Max columns.
"""
from __future__ import annotations

import io
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.config import canonical_name  # noqa: E402

SEASONS = ["1617", "1718", "1819", "1920", "2021",
           "2122", "2223", "2324", "2425", "2526"]

RAW_DIR = ROOT / "research" / "bl1" / "data"
OUT_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"


def _read(season: str) -> pd.DataFrame | None:
    p = RAW_DIR / f"D1_{season}.csv"
    if not p.exists():
        return None
    content = p.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    df = pd.read_csv(io.StringIO(text), low_memory=False)
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])
    df["season"] = season
    df["home_team"] = df["HomeTeam"].map(canonical_name)
    df["away_team"] = df["AwayTeam"].map(canonical_name)
    return df.rename(columns={"Date": "date", "FTHG": "home_score", "FTAG": "away_score"})


def main() -> None:
    frames = [d for d in (_read(s) for s in SEASONS) if d is not None]
    df = pd.concat(frames, ignore_index=True).sort_values(["date", "home_team"]).reset_index(drop=True)
    OUT_PKL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PKL, "wb") as f:
        pickle.dump(df, f)
    print(f"Wrote {len(df)} rows × {df.shape[1]} cols to {OUT_PKL}")
    cols_pre = [c for c in df.columns if c[-1] in "HDA" and "C" not in c[:-1] and len(c) <= 5]
    cols_close = [c for c in df.columns if len(c) >= 3 and c[-1] in "HDA" and c[-2] == "C"]
    print(f"Pre-closing 1X2 cols detected: {sorted(set(cols_pre))}")
    print(f"Closing 1X2 cols detected: {sorted(set(cols_close))}")


if __name__ == "__main__":
    main()
