"""Match-History-Loader mit Sackmann-Primary, XLSX-Fallback (Roadmap J2-I).

Sackmann-GitHub-Repos (tennis_atp/tennis_wta) sind seit 2026-06 nicht mehr
öffentlich erreichbar. Wenn Sackmann leer/down ist, fällt der Loader auf
tennis-data.co.uk-XLSX zurück und konvertiert in Sackmann-Schema, sodass
existierender Elo-Code (`compute_tennis_elo`) unverändert läuft.

Public API:
    load_match_history()            → (DataFrame, source_tag)
    build_live_rolling_state(df)    → RollingState (FND-MODEL1-001)
    extract_player_ranks(df)        → dict[player_name → rank] (FND-MODEL1-013)
"""
from __future__ import annotations

import logging
import re as _re
from typing import TYPE_CHECKING

import pandas as pd

from src.data.tennis_data import fetch_atp_matches, fetch_wta_matches
from src.data.tennis_odds import fetch_full_tour_odds

if TYPE_CHECKING:
    from src.tennis.features import RollingState

_log = logging.getLogger("sportsbrain.tennis.elo_source")


_SURFACE_NORM = {"hard": "Hard", "clay": "Clay", "grass": "Grass", "carpet": "Carpet"}


def _xlsx_to_sackmann(odds_df: pd.DataFrame) -> pd.DataFrame:
    """Convert tennis-data XLSX schema → Sackmann column names.

    Passes through Wsets/Lsets (as winner_sets/loser_sets) and per-set
    game scores so build_live_rolling_state() can populate sets_dropped /
    tiebreak features — same columns used by tennis_train.py.
    """
    if odds_df.empty:
        return pd.DataFrame()
    rename_map = {
        "Date": "tourney_date",
        "Tournament": "tourney_name",
        "Winner": "winner_name",
        "Loser": "loser_name",
        "WRank": "winner_rank",
        "LRank": "loser_rank",
        "Round": "round",
        "Wsets": "winner_sets",
        "Lsets": "loser_sets",
    }
    # Per-set game scores W1..W5 / L1..L5 for tiebreak detection
    for i in range(1, 6):
        rename_map[f"W{i}"] = f"w{i}"
        rename_map[f"L{i}"] = f"l{i}"
    out = odds_df.rename(columns=rename_map).copy()
    surf_lower = out.get("surface_std", pd.Series(["hard"] * len(out))).astype(str).str.lower()
    out["surface"] = surf_lower.map(_SURFACE_NORM).fillna("Hard")
    out["tourney_level"] = "A"
    out["score"] = ""
    keep = [
        "tourney_date", "tourney_name", "tourney_level", "surface",
        "winner_name", "loser_name", "score", "round",
        "winner_rank", "loser_rank",
        "winner_sets", "loser_sets",
        "w1", "l1", "w2", "l2", "w3", "l3", "w4", "l4", "w5", "l5",
    ]
    return out[[c for c in keep if c in out.columns]].dropna(subset=["winner_name", "loser_name"])


def load_match_history() -> tuple[pd.DataFrame, str]:
    """Return (combined ATP+WTA matches, source-tag).

    source-tag ∈ {"sackmann", "xlsx-fallback", "empty"} — for logging.
    """
    try:
        atp = fetch_atp_matches()
    except Exception as exc:
        _log.warning("fetch_atp_matches failed, skipping: %s", exc)
        atp = pd.DataFrame()
    try:
        wta = fetch_wta_matches()
    except Exception as exc:
        _log.warning("fetch_wta_matches failed, skipping: %s", exc)
        wta = pd.DataFrame()

    if not atp.empty or not wta.empty:
        frames = [df for df in (atp, wta) if not df.empty]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values("tourney_date").reset_index(drop=True)
        return combined, "sackmann"

    # Fallback: tennis-data.co.uk XLSX (2019-2025 cached)
    try:
        xlsx = fetch_full_tour_odds(tours=["atp", "wta"])
    except Exception as exc:
        _log.warning("fetch_full_tour_odds failed, match history empty: %s", exc)
        xlsx = pd.DataFrame()
    if xlsx.empty:
        return pd.DataFrame(), "empty"

    sack_like = _xlsx_to_sackmann(xlsx)
    sack_like = sack_like.sort_values("tourney_date").reset_index(drop=True)
    return sack_like, "xlsx-fallback"


def build_live_rolling_state(matches: pd.DataFrame) -> RollingState:
    """Build a populated RollingState from historical match data (FND-MODEL1-001).

    Walk-forward semantics mirror tennis_train.py exactly:
      - matches processed strictly chronologically
      - state.update() called AFTER features would be extracted for each match
      - no future result can enter the state for any upcoming match

    Name contract: winner_name/loser_name must be in the same canonical format
    as used for Elo storage (Sackmann / XLSX "Surname I." schema). The scanner
    passes elo_key_a/elo_key_b (already normalized) to build_match_features so
    state lookups and state keys use the same string.

    Returns an empty RollingState (not None) if matches is empty; callers that
    need the fail-safe path must check is_empty() or compare to the sentinel.
    """
    from src.tennis.features import RollingState

    state = RollingState(window=10)
    if matches is None or matches.empty:
        _log.warning("build_live_rolling_state: empty match corpus → default priors")
        return state

    df = matches.sort_values("tourney_date").reset_index(drop=True)
    n_processed = 0

    for _, row in df.iterrows():
        try:
            winner = str(row["winner_name"])
            loser = str(row["loser_name"])
        except (KeyError, TypeError):
            continue
        if not winner or not loser or winner == "nan" or loser == "nan":
            continue

        surface = str(row.get("surface", "hard") or "hard").lower().strip()
        date = row.get("tourney_date")

        winner_rank: float | None = None
        loser_rank: float | None = None
        try:
            rw = row.get("winner_rank")
            if pd.notna(rw) and rw:
                winner_rank = float(rw)
        except (TypeError, ValueError):
            pass
        try:
            rl = row.get("loser_rank")
            if pd.notna(rl) and rl:
                loser_rank = float(rl)
        except (TypeError, ValueError):
            pass

        # Set counts (winner_sets/loser_sets from _xlsx_to_sackmann, or Wsets/Lsets direct)
        sets_w: int | None = None
        sets_l: int | None = None
        for col_w, col_l in (("winner_sets", "loser_sets"), ("Wsets", "Lsets")):
            try:
                rw = row.get(col_w)
                rl = row.get(col_l)
                if pd.notna(rw) and pd.notna(rl):
                    sets_w = int(rw)
                    sets_l = int(rl)
                    break
            except (TypeError, ValueError, KeyError):
                pass

        # Tiebreak detection from per-set scores (w1..w5 / l1..l5)
        tb_w = tb_l = 0
        for i in range(1, 6):
            try:
                gw = row.get(f"w{i}") or row.get(f"W{i}")
                gl = row.get(f"l{i}") or row.get(f"L{i}")
                if pd.notna(gw) and pd.notna(gl):
                    gw_i, gl_i = int(gw), int(gl)
                    if gw_i == 7 and gl_i == 6:
                        tb_w += 1
                    elif gl_i == 7 and gw_i == 6:
                        tb_l += 1
            except (TypeError, ValueError):
                pass

        # Fallback: parse tiebreaks from score string (Sackmann: "6-3 7-6(4) 6-1")
        if tb_w == 0 and tb_l == 0:
            score = str(row.get("score", "") or "")
            for set_score in score.split():
                m = _re.match(r'^(\d+)-(\d+)', set_score)
                if m:
                    gw_s, gl_s = int(m.group(1)), int(m.group(2))
                    if gw_s == 7 and gl_s == 6:
                        tb_w += 1
                    elif gl_s == 7 and gw_s == 6:
                        tb_l += 1

        state.update(
            winner, loser, surface, date=date,
            winner_rank=winner_rank, loser_rank=loser_rank,
            sets_w=sets_w, sets_l=sets_l,
            tiebreaks_won_by_winner=tb_w, tiebreaks_won_by_loser=tb_l,
        )
        n_processed += 1

    _log.info(
        "build_live_rolling_state: %d matches → %d players with form history",
        n_processed,
        sum(1 for v in state.form.values() if len(v) > 0),
    )
    return state


def extract_player_ranks(matches: pd.DataFrame) -> dict[str, float]:
    """Extract latest ATP/WTA ranking per player from historical match data.

    Returns {elo_player_name → latest_rank_number}.  Uses the player's most
    recent match in the corpus to approximate current ranking.  Callers use
    the neutral prior (1500) for any player not in the returned dict.

    Validity contract: positive ranking numbers only; values above 3000 are
    rejected as implausible (same semantics as training-time _MIN_RANK=1500
    used for missing rows).  Player names use the Elo-key format already
    present in the historical corpus (Sackmann / XLSX "Surname I." schema).
    """
    _RANK_MAX_VALID = 3000.0
    records = []
    for name_col, rank_col in (("winner_name", "winner_rank"), ("loser_name", "loser_rank")):
        if name_col not in matches.columns or rank_col not in matches.columns:
            continue
        sub = matches[["tourney_date", name_col, rank_col]].rename(
            columns={name_col: "player", rank_col: "rank"}
        )
        records.append(sub)
    if not records:
        return {}
    combined = pd.concat(records, ignore_index=True).dropna(subset=["rank"])
    try:
        combined = combined.copy()
        combined["rank"] = combined["rank"].astype(float)
    except (TypeError, ValueError):
        return {}
    combined = combined[(combined["rank"] > 0) & (combined["rank"] <= _RANK_MAX_VALID)]
    if combined.empty:
        return {}
    combined["tourney_date"] = pd.to_datetime(combined["tourney_date"], errors="coerce")
    combined = combined.dropna(subset=["tourney_date"])
    latest = combined.sort_values("tourney_date").groupby("player").last().reset_index()
    return {str(row["player"]): float(row["rank"]) for _, row in latest.iterrows()}
