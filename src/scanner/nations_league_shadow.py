"""Read-only, non-betting UEFA Nations League shadow inference.

This module deliberately does not route through ``run_daily_scan`` or its
betting, ledger, notification, publication, or scheduler integrations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from src.betting.odds_utils import remove_margin_shin
from src.config import (
    COMPETITIVE_TOURNAMENTS,
    DATA_CACHE,
    MODELS_DIR,
    ODDS_API_URL,
    canonical_name,
)
from src.ensemble.stacking import build_stacker_features
from src.features.builder import build_feature_row
from src.models import dixon_coles as dc

SPORT_KEY = "soccer_uefa_nations_league"
TOURNAMENT = "UEFA Nations League"
SCHEMA = "nations-league-shadow-v1"
SNAPSHOT_DIR = MODELS_DIR / "snapshots" / "wm2026"
ACTIVE_START = datetime(2026, 9, 24, tzinfo=timezone.utc)
ACTIVE_END_EXCLUSIVE = datetime(2026, 11, 18, tzinfo=timezone.utc)
MAX_HISTORY_AGE = timedelta(hours=24)
SNAPSHOT_FILES = frozenset(
    {
        "README.md",
        "anchor.json",
        "calibrators.pkl",
        "cluster_calibrators.pkl",
        "conformal.pkl",
        "dc_current_elo.json",
        "dc_lifecycle.json",
        "dc_params_final.pkl",
        "feature_columns.json",
        "gate.json",
        "metadata.json",
        "model.pkl",
        "stacker.pkl",
        "stacker_features.json",
    }
)
SOURCE_FILES = (
    "scripts/nations_league_shadow_scan.py",
    "src/betting/odds_utils.py",
    "src/config.py",
    "src/data/odds_api.py",
    "src/ensemble/stacking.py",
    "src/features/builder.py",
    "src/features/squad_context.py",
    "src/models/dixon_coles.py",
    "src/models/elo.py",
    "src/models/lgbm_model.py",
    "src/runtime/paths.py",
    "src/scanner/nations_league_shadow.py",
    "src/scanner/scoring.py",
    "src/signals/provider_budget.py",
)


class NationsLeagueShadowError(RuntimeError):
    """A fail-closed error in shadow setup or execution."""


@dataclass(frozen=True)
class FrozenSnapshot:
    root: Path
    identity: str
    digest: str
    file_digests: dict[str, str]
    feature_columns: tuple[str, ...]
    stacker_columns: tuple[str, ...]
    dc_params: Any
    elo_ratings: dict[str, float]
    gbt_model: Any
    stacker: Any
    anchor: dict[str, Any]

    def provenance(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "digest": self.digest,
            "files": self.file_digests,
            "feature_column_count": len(self.feature_columns),
            "stacker_feature_column_count": len(self.stacker_columns),
            "used_components": [
                "dc_params_final.pkl",
                "dc_current_elo.json",
                "model.pkl",
                "stacker.pkl",
                "feature_columns.json",
                "stacker_features.json",
            ],
            "hashed_but_not_used_by_this_stack": [
                "README.md",
                "anchor.json",
                "calibrators.pkl",
                "cluster_calibrators.pkl",
                "conformal.pkl",
                "dc_lifecycle.json",
                "gate.json",
                "metadata.json",
            ],
            "stacker_input_contract": "frozen training contract: DC + margin-free h2h market; GBT is reported separately and is not fed into a stacker trained with the DC fallback",
            "market_anchor": {
                "status": "not_applied",
                "reason": "anchor.json describes a DC plus calibrated-GBT blend, not this frozen stacker contract; it is not a valid bound anchor for these probabilities",
                "alpha_model_weight": self.anchor.get("alpha"),
                "use_anchor_in_source": self.anchor.get("use_anchor"),
            },
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_frozen_snapshot(snapshot_dir: Path = SNAPSHOT_DIR) -> FrozenSnapshot:
    """Load and cross-check the exact committed WM2026 model snapshot."""
    root = Path(snapshot_dir)
    if not root.is_dir():
        raise NationsLeagueShadowError("frozen WM2026 snapshot directory is missing")
    actual_files = {p.name for p in root.iterdir() if p.is_file()}
    if actual_files != SNAPSHOT_FILES:
        missing = sorted(SNAPSHOT_FILES - actual_files)
        extra = sorted(actual_files - SNAPSHOT_FILES)
        raise NationsLeagueShadowError(
            f"frozen snapshot file set mismatch (missing={missing}, extra={extra})"
        )

    file_digests: dict[str, str] = {}
    for name in sorted(SNAPSHOT_FILES):
        try:
            file_digests[name] = _sha256_bytes((root / name).read_bytes())
        except OSError as exc:
            raise NationsLeagueShadowError(f"snapshot file unreadable: {name}") from exc
    snapshot_digest = _sha256_bytes(_canonical_json(file_digests))

    try:
        metadata = json.loads((root / "metadata.json").read_text())
        feature_columns = tuple(json.loads((root / "feature_columns.json").read_text()))
        stacker_schema = json.loads((root / "stacker_features.json").read_text())
        stacker_columns = tuple(stacker_schema["feature_columns"])
        elo_raw = json.loads((root / "dc_current_elo.json").read_text())
        anchor = json.loads((root / "anchor.json").read_text())
        with (root / "dc_params_final.pkl").open("rb") as stream:
            dc_params = pickle.load(stream)
        with (root / "model.pkl").open("rb") as stream:
            gbt_model = pickle.load(stream)
        with (root / "stacker.pkl").open("rb") as stream:
            stacker = pickle.load(stream)
    except Exception as exc:
        raise NationsLeagueShadowError(
            "frozen snapshot component failed to load"
        ) from exc

    if metadata.get("tournament") != "FIFA World Cup 2026":
        raise NationsLeagueShadowError("snapshot tournament identity is not WM2026")
    frozen_at = str(metadata.get("frozen_at", ""))
    if not frozen_at:
        raise NationsLeagueShadowError("snapshot freeze timestamp is missing")
    try:
        datetime.fromisoformat(frozen_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueShadowError(
            "snapshot freeze timestamp is malformed"
        ) from exc

    attack = getattr(dc_params, "attack", None)
    defence = getattr(dc_params, "defence", None)
    if not isinstance(attack, dict) or not isinstance(defence, dict):
        raise NationsLeagueShadowError(
            "snapshot Dixon-Coles parameter shape is invalid"
        )
    if set(attack) != set(defence):
        raise NationsLeagueShadowError("DC attack/defence team contracts disagree")
    if not isinstance(elo_raw, dict):
        raise NationsLeagueShadowError("snapshot Elo payload is not a team mapping")
    elo_ratings: dict[str, float] = {}
    for team, rating in elo_raw.items():
        try:
            canonical = canonical_name(str(team))
            value = float(rating)
        except (TypeError, ValueError) as exc:
            raise NationsLeagueShadowError("snapshot Elo entry is malformed") from exc
        if not math.isfinite(value):
            raise NationsLeagueShadowError("snapshot Elo rating is not finite")
        if canonical in elo_ratings:
            raise NationsLeagueShadowError(
                "snapshot Elo aliases collide after normalization"
            )
        elo_ratings[canonical] = value
    if set(attack) != set(elo_ratings):
        raise NationsLeagueShadowError("frozen DC and Elo team contracts disagree")

    if not feature_columns or len(feature_columns) != len(set(feature_columns)):
        raise NationsLeagueShadowError(
            "GBT feature-column contract is empty or duplicated"
        )
    model_columns = tuple(str(c) for c in getattr(gbt_model, "feature_names_in_", ()))
    if model_columns != feature_columns:
        raise NationsLeagueShadowError(
            "GBT model and frozen feature-column order disagree"
        )
    if int(getattr(gbt_model, "n_features_in_", -1)) != len(feature_columns):
        raise NationsLeagueShadowError(
            "GBT model feature count disagrees with frozen contract"
        )
    if tuple(int(c) for c in getattr(gbt_model, "classes_", ())) != (0, 1, 2):
        raise NationsLeagueShadowError("GBT output class order is not away/draw/home")

    if not stacker_columns or len(stacker_columns) != len(set(stacker_columns)):
        raise NationsLeagueShadowError(
            "stacker feature-column contract is empty or duplicated"
        )
    if tuple(getattr(stacker, "feature_columns", ())) != stacker_columns:
        raise NationsLeagueShadowError(
            "stacker pickle and frozen stacker schema disagree"
        )
    if getattr(stacker, "model", None) is None:
        raise NationsLeagueShadowError("frozen stacker has no fitted model")
    if tuple(int(c) for c in getattr(stacker.model, "classes_", ())) != (0, 1, 2):
        raise NationsLeagueShadowError(
            "stacker output class order is not away/draw/home"
        )

    return FrozenSnapshot(
        root=root,
        identity=f"wm2026-frozen-{frozen_at[:10]}",
        digest=snapshot_digest,
        file_digests=file_digests,
        feature_columns=feature_columns,
        stacker_columns=stacker_columns,
        dc_params=dc_params,
        elo_ratings=elo_ratings,
        gbt_model=gbt_model,
        stacker=stacker,
        anchor=anchor,
    )


def load_cached_history(
    cache_path: Path = DATA_CACHE / "international_results.pkl",
    *,
    now: datetime | None = None,
    max_age: timedelta = MAX_HISTORY_AGE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read only the existing results cache; never fetch or refresh it."""
    path = Path(cache_path)
    if not path.is_file():
        raise NationsLeagueShadowError("local international-results cache is missing")
    captured = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = captured - modified
    if age < timedelta(0) or age > max_age:
        raise NationsLeagueShadowError("local international-results cache is stale")
    try:
        with path.open("rb") as stream:
            history = pickle.load(stream)
    except Exception as exc:
        raise NationsLeagueShadowError(
            "local international-results cache is unreadable"
        ) from exc
    if not isinstance(history, pd.DataFrame):
        raise NationsLeagueShadowError(
            "local international-results cache has the wrong type"
        )
    required = {
        "date",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "tournament",
    }
    if not required.issubset(history.columns):
        raise NationsLeagueShadowError(
            "local international-results columns are incomplete"
        )
    history = history.copy()
    history["date"] = pd.to_datetime(
        history["date"], errors="coerce", utc=True
    ).dt.tz_localize(None)
    history["home_score"] = pd.to_numeric(history["home_score"], errors="coerce")
    history["away_score"] = pd.to_numeric(history["away_score"], errors="coerce")
    history = history.dropna(subset=["date", "home_score", "away_score"])
    if "neutral" not in history:
        history["neutral"] = False
    history["home_team"] = history["home_team"].astype(str).map(canonical_name)
    history["away_team"] = history["away_team"].astype(str).map(canonical_name)
    eligible = (
        history["tournament"]
        .astype(str)
        .map(lambda name: any(token in name for token in COMPETITIVE_TOURNAMENTS))
    )
    history = history[eligible].sort_values("date").reset_index(drop=True)
    if history.empty:
        raise NationsLeagueShadowError(
            "local international-results cache has no competitive history"
        )
    return history, {
        "path": str(path),
        "sha256": _sha256_bytes(path.read_bytes()),
        "captured_at": modified.isoformat(),
        "age_seconds": int(age.total_seconds()),
        "match_count": len(history),
    }


def _snapshot_elo_series(snapshot: FrozenSnapshot) -> pd.DataFrame:
    """Expose the frozen Elo values to the feature builder without recomputing them."""
    metadata = json.loads((snapshot.root / "metadata.json").read_text())
    as_of = pd.Timestamp(metadata["frozen_at"])
    if as_of.tzinfo is not None:
        as_of = as_of.tz_convert("UTC").tz_localize(None)
    rows = [
        {
            "date": as_of,
            "home_team": team,
            "away_team": None,
            "elo_home_post": rating,
            "elo_away_post": np.nan,
        }
        for team, rating in snapshot.elo_ratings.items()
    ]
    return pd.DataFrame(rows)


def _probability_map(values: Any, *, order: tuple[str, str, str]) -> dict[str, float]:
    array = np.asarray(values, dtype=float).reshape(-1)
    if array.shape != (3,) or not np.isfinite(array).all():
        raise NationsLeagueShadowError(
            "model did not return three finite 1X2 probabilities"
        )
    if (
        (array < 0).any()
        or (array > 1).any()
        or not math.isclose(float(array.sum()), 1.0, rel_tol=0.0, abs_tol=1e-6)
    ):
        raise NationsLeagueShadowError(
            "model probabilities are outside the normalized 1X2 simplex"
        )
    return {key: float(array[index]) for index, key in enumerate(order)}


def _fair_market_probabilities(odds: tuple[float, float, float]) -> dict[str, float]:
    try:
        home, draw, away = remove_margin_shin(odds)
    except Exception as exc:
        raise NationsLeagueShadowError("h2h odds could not be margin-adjusted") from exc
    return _probability_map((home, draw, away), order=("home", "draw", "away"))


def predict_fixture(
    *,
    event: Mapping[str, Any],
    odds: dict[str, Any],
    snapshot: FrozenSnapshot,
    historical: pd.DataFrame,
    captured_at: datetime,
) -> dict[str, Any]:
    raw_home = str(event["home_team"])
    raw_away = str(event["away_team"])
    home = canonical_name(raw_home)
    away = canonical_name(raw_away)
    if home == away:
        raise NationsLeagueShadowError("home and away normalize to the same team")
    for team in (home, away):
        if team not in snapshot.dc_params.attack or team not in snapshot.elo_ratings:
            raise NationsLeagueShadowError(
                "team is absent from the exact frozen model snapshot"
            )

    kickoff = pd.Timestamp(event["commence_time"])
    if kickoff.tzinfo is None:
        raise NationsLeagueShadowError("provider kickoff has no timezone")
    match_ts_naive = kickoff.tz_convert("UTC").tz_localize(None)
    past = historical[historical["date"] < match_ts_naive]
    if past.empty:
        raise NationsLeagueShadowError(
            "no causal historical results exist before fixture kickoff"
        )

    try:
        dc_result = dc.predict_match(home, away, snapshot.dc_params, neutral=False)
    except Exception as exc:
        raise NationsLeagueShadowError("frozen Dixon-Coles inference failed") from exc
    dc_probs = _probability_map(
        (dc_result["p_home"], dc_result["p_draw"], dc_result["p_away"]),
        order=("home", "draw", "away"),
    )

    market_probs = _fair_market_probabilities(
        (float(odds["home"]), float(odds["draw"]), float(odds["away"]))
    )
    elo_series = _snapshot_elo_series(snapshot)
    try:
        features = build_feature_row(
            home=home,
            away=away,
            match_date=match_ts_naive,
            historical=past,
            elo_series=elo_series,
            dc_params=snapshot.dc_params,
            neutral=False,
            tournament=TOURNAMENT,
            market_odds=None,
            statsbomb_xg=None,
            player_xg_df=None,
            fotmob_ratings_df=pd.DataFrame(),
            ppda_df=None,
        )
        if int(features.get("is_neutral", 1)) != 0:
            raise NationsLeagueShadowError(
                "feature contract marked a normal NL fixture neutral"
            )
        if int(features.get("is_knockout", 1)) != 0:
            raise NationsLeagueShadowError(
                "league-phase feature contract marked fixture knockout"
            )
        if int(features.get("is_group_stage", 0)) != 1:
            raise NationsLeagueShadowError(
                "league-phase feature contract is not identified as group stage"
            )
        X = pd.DataFrame([features]).reindex(
            columns=snapshot.feature_columns, fill_value=0.0
        )
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        gbt_array = np.asarray(snapshot.gbt_model.predict_proba(X), dtype=float)[0]
    except NationsLeagueShadowError:
        raise
    except Exception as exc:
        raise NationsLeagueShadowError(
            "frozen GBT feature/model inference failed"
        ) from exc
    gbt_probs = _probability_map(gbt_array, order=("away", "draw", "home"))

    # The frozen stacker was trained with lgbm_probs=None in train_stacker.py.
    # Keep that DC-fallback feature contract instead of injecting an out-of-
    # contract GBT vector; the raw GBT result is retained separately above.
    stacker_features = build_stacker_features(
        dc_probs=dc_result,
        lgbm_probs=None,
        shin_probs=(market_probs["home"], market_probs["draw"], market_probs["away"]),
        is_knockout=False,
        is_neutral=False,
    )
    stacker_array = np.asarray(
        snapshot.stacker.predict_proba(stacker_features.reshape(1, -1)), dtype=float
    )[0]
    stacker_probs = _probability_map(stacker_array, order=("away", "draw", "home"))
    stacker_probs = {
        "home": stacker_probs["home"],
        "draw": stacker_probs["draw"],
        "away": stacker_probs["away"],
    }

    market_by_outcome = {k: market_probs[k] for k in ("home", "draw", "away")}
    edges_pp = {
        outcome: round((stacker_probs[outcome] - market_by_outcome[outcome]) * 100.0, 6)
        for outcome in ("home", "draw", "away")
    }
    return {
        "provider_event_id": str(event["id"]),
        "kickoff": kickoff.tz_convert("UTC").isoformat(),
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        "home_team": home,
        "away_team": away,
        "neutral": False,
        "tournament": TOURNAMENT,
        "market": {
            "bookmaker": odds["bookmaker"],
            "odds_decimal": {
                "home": float(odds["home"]),
                "draw": float(odds["draw"]),
                "away": float(odds["away"]),
            },
            "margin_free_probabilities": market_by_outcome,
        },
        "probabilities": {
            "raw_dixon_coles": dc_probs,
            "raw_gbt": {
                "home": gbt_probs["home"],
                "draw": gbt_probs["draw"],
                "away": gbt_probs["away"],
            },
            "canonical_stacker": stacker_probs,
            "final_ensemble": stacker_probs,
            "market_anchored": None,
        },
        "market_anchor_status": "not_applied_unbound_to_frozen_stacker_contract",
        "model_vs_market_edge_percentage_points": edges_pp,
    }


def _event_identity(event: Mapping[str, Any]) -> tuple[bool, str | None]:
    sport_key = event.get("sport_key")
    sport_title = event.get("sport_title")
    if sport_key != SPORT_KEY:
        return False, "non_target_sport_key"
    if not isinstance(sport_title, str):
        return False, "missing_tournament_identity"
    normalized_title = " ".join(sport_title.casefold().split())
    if normalized_title != TOURNAMENT.casefold():
        return False, "non_target_tournament"
    return True, None


def _event_market_odds(
    event: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    home_name = str(event.get("home_team", ""))
    away_name = str(event.get("away_team", ""))
    bookmakers = event.get("bookmakers")
    if not isinstance(bookmakers, list):
        return None, "missing_h2h_odds"
    found_h2h = False
    malformed = False
    candidates = sorted(
        (bm for bm in bookmakers if isinstance(bm, Mapping)),
        key=lambda bm: (
            str(bm.get("key", "")).casefold() != "pinnacle",
            str(bm.get("key", "")).casefold(),
        ),
    )
    for bookmaker in candidates:
        key = str(bookmaker.get("key", "")).strip()
        markets = bookmaker.get("markets", [])
        if not key or not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, Mapping) or market.get("key") != "h2h":
                continue
            found_h2h = True
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) != 3:
                malformed = True
                continue
            selected: dict[str, float] = {}
            valid = True
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    valid = False
                    break
                name = str(outcome.get("name", "")).strip()
                folded = name.casefold()
                if folded == "draw":
                    label = "draw"
                elif folded == home_name.casefold():
                    label = "home"
                elif folded == away_name.casefold():
                    label = "away"
                else:
                    valid = False
                    break
                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    valid = False
                    break
                if not math.isfinite(price) or price <= 1.0 or label in selected:
                    valid = False
                    break
                selected[label] = price
            if valid and set(selected) == {"home", "draw", "away"}:
                return {
                    "bookmaker": key,
                    "home": selected["home"],
                    "draw": selected["draw"],
                    "away": selected["away"],
                }, None
            malformed = True
    if malformed:
        return None, "malformed_h2h_odds"
    return None, "missing_h2h_odds" if not found_h2h else "malformed_h2h_odds"


def build_run_artifact(
    *,
    events: list[Mapping[str, Any]],
    snapshot: FrozenSnapshot,
    historical: pd.DataFrame,
    history_provenance: dict[str, Any],
    source_sha: str,
    captured_at: datetime,
    request_count: int,
    retry_count: int,
    request_descriptor: dict[str, Any],
) -> dict[str, Any]:
    captured_utc = captured_at.astimezone(timezone.utc)
    skipped: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    fixture_count = 0
    for event in events:
        event_id = str(event.get("id", ""))
        target, identity_reason = _event_identity(event)
        if not target:
            skipped.append(
                {"provider_event_id": event_id or None, "reason": identity_reason}
            )
            continue
        fixture_count += 1
        if not event_id:
            skipped.append(
                {"provider_event_id": None, "reason": "missing_provider_event_id"}
            )
            continue
        if event_id in seen_ids:
            skipped.append(
                {"provider_event_id": event_id, "reason": "duplicate_provider_event_id"}
            )
            continue
        seen_ids.add(event_id)
        try:
            kickoff = datetime.fromisoformat(
                str(event.get("commence_time", "")).replace("Z", "+00:00")
            )
            if kickoff.tzinfo is None:
                raise ValueError("timezone missing")
            kickoff = kickoff.astimezone(timezone.utc)
        except ValueError:
            skipped.append(
                {"provider_event_id": event_id, "reason": "malformed_kickoff"}
            )
            continue
        if not (ACTIVE_START <= kickoff < ACTIVE_END_EXCLUSIVE):
            skipped.append(
                {"provider_event_id": event_id, "reason": "outside_active_window"}
            )
            continue
        if kickoff <= captured_utc:
            skipped.append(
                {"provider_event_id": event_id, "reason": "kickoff_not_future"}
            )
            continue
        home_raw, away_raw = event.get("home_team"), event.get("away_team")
        if (
            not isinstance(home_raw, str)
            or not home_raw.strip()
            or not isinstance(away_raw, str)
            or not away_raw.strip()
        ):
            skipped.append(
                {"provider_event_id": event_id, "reason": "missing_team_identity"}
            )
            continue
        home, away = canonical_name(home_raw), canonical_name(away_raw)
        if (
            home == away
            or home not in snapshot.dc_params.attack
            or away not in snapshot.dc_params.attack
        ):
            skipped.append(
                {"provider_event_id": event_id, "reason": "unknown_model_team"}
            )
            continue
        market_odds, odds_error = _event_market_odds(event)
        if market_odds is None:
            skipped.append({"provider_event_id": event_id, "reason": odds_error})
            continue
        try:
            covered.append(
                predict_fixture(
                    event=event,
                    odds=market_odds,
                    snapshot=snapshot,
                    historical=historical,
                    captured_at=captured_utc,
                )
            )
        except NationsLeagueShadowError as exc:
            reason = (
                "unknown_model_team"
                if "absent from the exact frozen model" in str(exc)
                else "model_inference_failed"
            )
            skipped.append({"provider_event_id": event_id, "reason": reason})

    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": f"unl-shadow-{captured_utc.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}",
        "source_sha": source_sha,
        "model_snapshot": snapshot.provenance(),
        "input_data": {"international_results": history_provenance},
        "provider": "the_odds_api",
        "sport_key": SPORT_KEY,
        "captured_at": captured_utc.isoformat(),
        "request": request_descriptor,
        "request_count": int(request_count),
        "retry_count": int(retry_count),
        "provider_event_count": len(events),
        "fixture_count": fixture_count,
        "covered_fixture_count": len(covered),
        "skipped_fixtures": skipped,
        "fixtures": covered,
        "shadow": True,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
    }
    artifact["artifact_digest"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _default_api_key_loader(api_key: str | None) -> str:
    from dotenv import load_dotenv

    load_dotenv()
    key = api_key or os.getenv("ODDS_API_KEY", "")
    if not key:
        raise NationsLeagueShadowError("The Odds API credential is unavailable")
    return key


def _required_nonnegative_quota_header(headers: Mapping[str, str], name: str) -> int:
    raw_value = headers.get(name)
    if raw_value is None:
        raise NationsLeagueShadowError(
            f"The Odds API omitted required {name} quota evidence"
        )
    try:
        value = int(raw_value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise NationsLeagueShadowError(
            f"The Odds API returned malformed {name} quota evidence"
        ) from exc
    if value < 0:
        raise NationsLeagueShadowError(
            f"The Odds API returned negative {name} quota evidence"
        )
    return value


def fetch_provider_events(
    *,
    api_key: str | None = None,
    transport: Callable[..., Any] | None = None,
    budget_gate: Callable[[], bool] | None = None,
    credential_loader: Callable[[str | None], str] | None = None,
    success_recorder: Callable[[int | None], None] | None = None,
    error_recorder: Callable[[int, bool], None] | None = None,
    usage_logger: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int, int, dict[str, Any]]:
    """Fetch exactly The Odds API's Nations League h2h/eu endpoint after budget gate."""
    if budget_gate is None:
        from src.signals.provider_budget import is_provider_available

        budget_gate = lambda: is_provider_available("the_odds_api")
    try:
        budget_available = budget_gate()
    except Exception as exc:
        raise NationsLeagueShadowError(
            "The Odds API budget gate could not be checked"
        ) from exc
    if not budget_available:
        raise NationsLeagueShadowError(
            "The Odds API provider-budget circuit is unavailable"
        )

    key_loader = credential_loader or _default_api_key_loader
    key = key_loader(api_key)
    request_transport = transport or requests.get
    url = f"{ODDS_API_URL}/sports/{SPORT_KEY}/odds"
    params = {
        "apiKey": key,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    descriptor = {
        "method": "GET",
        "url": url,
        "query": {k: v for k, v in params.items() if k != "apiKey"},
        "credential": "apiKey (redacted)",
    }
    try:
        response = request_transport(
            url, params=params, timeout=30, allow_redirects=False
        )
    except Exception as exc:
        raise NationsLeagueShadowError(
            "The Odds API single h2h request failed"
        ) from exc

    try:
        status = int(response.status_code)
    except (AttributeError, TypeError, ValueError) as exc:
        raise NationsLeagueShadowError(
            "The Odds API returned an invalid HTTP status"
        ) from exc

    if status in (401, 403, 429):
        if error_recorder is None:
            from src.signals.provider_budget import record_error

            error_recorder = lambda code, opened: record_error(
                "the_odds_api", code, open_circuit=opened
            )
        try:
            error_recorder(status, True)
        except Exception as exc:
            raise NationsLeagueShadowError(
                "provider auth/quota failure could not be recorded"
            ) from exc
        raise NationsLeagueShadowError(
            f"The Odds API rejected the single request with HTTP {status}"
        )
    if 400 <= status < 500:
        raise NationsLeagueShadowError(
            f"The Odds API rejected the single request with HTTP {status}"
        )
    if 300 <= status < 400:
        raise NationsLeagueShadowError("The Odds API returned an unexpected redirect")
    if status >= 500:
        raise NationsLeagueShadowError(f"The Odds API returned HTTP {status}")
    if not 200 <= status < 300:
        raise NationsLeagueShadowError("The Odds API returned a non-success response")

    try:
        headers = {str(k).casefold(): str(v) for k, v in response.headers.items()}
    except Exception as exc:
        raise NationsLeagueShadowError(
            "The Odds API returned malformed quota headers"
        ) from exc
    used = _required_nonnegative_quota_header(headers, "x-requests-used")
    remaining = _required_nonnegative_quota_header(headers, "x-requests-remaining")

    try:
        if usage_logger is None:
            from src.data.odds_api import _log_usage

            usage_logger = lambda used_count, remaining_count: _log_usage(
                used_count, remaining_count
            )
        usage_logger(used, remaining)
    except Exception as exc:
        raise NationsLeagueShadowError(
            "successful provider response quota evidence could not be persisted"
        ) from exc

    try:
        events = response.json()
    except Exception as exc:
        raise NationsLeagueShadowError("The Odds API returned malformed JSON") from exc
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise NationsLeagueShadowError("The Odds API returned a malformed event list")

    try:
        if success_recorder is None:
            from src.signals.provider_budget import record_success

            success_recorder = lambda quota: record_success(
                "the_odds_api", quota_remaining=quota
            )
        success_recorder(remaining)
    except Exception as exc:
        raise NationsLeagueShadowError(
            "successful provider response could not be accounted for"
        ) from exc

    return events, 1, 0, descriptor


def current_source_sha(repository_root: Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", *SOURCE_FILES],
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
        diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", *SOURCE_FILES],
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NationsLeagueShadowError("cannot establish source release SHA") from exc
    if tracked.returncode != 0:
        raise NationsLeagueShadowError(
            "shadow inference source files are not all tracked"
        )
    if diff.returncode != 0:
        raise NationsLeagueShadowError(
            "shadow inference source changes make release provenance ambiguous"
        )
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise NationsLeagueShadowError("source release SHA is malformed")
    return revision


def write_immutable_artifact(path: Path, artifact: dict[str, Any]) -> Path:
    """Publish one private immutable JSON file without replacing any prior run."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(artifact) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".nations-league-shadow-", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise NationsLeagueShadowError(
            "immutable artifact target already exists"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def run_shadow_scan(
    *,
    api_key: str | None = None,
    snapshot_dir: Path = SNAPSHOT_DIR,
    history_path: Path = DATA_CACHE / "international_results.pkl",
    source_root: Path | None = None,
    output_path: Path | None = None,
    provider_transport: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Execute one shadow-only scan; history/snapshot validation precedes spend."""
    captured_preflight = datetime.now(timezone.utc)
    snapshot = load_frozen_snapshot(snapshot_dir)
    historical, history_provenance = load_cached_history(
        history_path, now=captured_preflight
    )
    root = Path(source_root) if source_root else Path(__file__).resolve().parents[2]
    source_sha = current_source_sha(root)
    events, requests_made, retries, request_descriptor = fetch_provider_events(
        api_key=api_key,
        transport=provider_transport,
    )
    captured_at = datetime.now(timezone.utc)
    artifact = build_run_artifact(
        events=events,
        snapshot=snapshot,
        historical=historical,
        history_provenance=history_provenance,
        source_sha=source_sha,
        captured_at=captured_at,
        request_count=requests_made,
        retry_count=retries,
        request_descriptor=request_descriptor,
    )
    if output_path is None:
        from src.runtime.paths import runtime_state_path

        output_path = runtime_state_path(
            f"data/nations-league-shadow/{artifact['run_id']}.json",
            require_external=True,
        )
    return artifact, write_immutable_artifact(output_path, artifact)
