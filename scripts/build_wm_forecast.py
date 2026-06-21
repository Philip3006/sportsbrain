"""WM 2026 Monte-Carlo Forecast.

Liest gespielte Resultate + DC-Params + Elo, simuliert die verbleibenden
Gruppen- und KO-Spiele N-mal und schreibt P(Stage) pro Team in
docs/data/wm_forecast.json.

Nutzung:
    python3 scripts/build_wm_forecast.py            # N=2000 (default)
    python3 scripts/build_wm_forecast.py --n 5000

Wird vom täglichen Scan via daily_scan.py oder eigenständig aufgerufen.

Vereinfachungen (v1):
- KO-Bracket: zufällige Paarungen pro Trial (FIFA-Bracket-Mapping nicht exakt).
  Beeinflusst Champion-% leicht, bleibt aber innerhalb der Modellunsicherheit.
- Tiebreaker: Pts > GD > GF (keine direkte H2H, FairPlay-Punkte).
- KO-Unentschieden: 50/50-Münzwurf (Elfmeterschießen-Approximation).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import canonical_name, MODELS_DIR  # noqa: E402
from src.models import dixon_coles as dc  # noqa: E402
from src.models.dixon_coles import predict_scoreline, get_stage_rho  # noqa: E402

# WM 2026 Gruppen (Display-Namen wie im Frontend)
WM_GROUPS: dict[str, list[str]] = {
    "A": ["Mexico", "South Africa", "South Korea", "Czechia"],
    "B": ["Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"],
    "C": ["Brazil", "Morocco", "Haiti", "Scotland"],
    "D": ["United States", "Paraguay", "Australia", "Turkey"],
    "E": ["Germany", "Curacao", "Cote d'Ivoire", "Ecuador"],
    "F": ["Netherlands", "Japan", "Sweden", "Tunisia"],
    "G": ["Belgium", "Egypt", "Iran", "New Zealand"],
    "H": ["Spain", "Cape Verde", "Saudi Arabia", "Uruguay"],
    "I": ["France", "Senegal", "Iraq", "Norway"],
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
    "L": ["England", "Croatia", "Ghana", "Panama"],
}

# KO-Stages (in Reihenfolge)
KO_STAGES = ["r32", "r16", "qf", "sf", "final"]


def _load_latest_dc_params():
    snap_dir = MODELS_DIR / "dixon_coles"
    files = sorted(snap_dir.glob("params_*.pkl"))
    if not files:
        return None
    return dc.load(files[-1])


def _load_elo() -> dict[str, float]:
    p = MODELS_DIR / "dixon_coles" / "current_elo.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _load_results() -> list[dict]:
    sig = ROOT / "docs" / "data" / "signals.json"
    if not sig.exists():
        return []
    try:
        return json.loads(sig.read_text()).get("wm_results", [])
    except Exception:
        return []


def _pair_key(a: str, b: str) -> frozenset:
    return frozenset([canonical_name(a), canonical_name(b)])


def _all_group_pairs() -> list[tuple[str, str, str]]:
    """Liefert alle (group_letter, team_i, team_j) Paare für alle 12 Gruppen."""
    out = []
    for grp, teams in WM_GROUPS.items():
        for i in range(len(teams)):
            for j in range(i + 1, len(teams)):
                out.append((grp, teams[i], teams[j]))
    return out


def _sample_score(matrix: np.ndarray, rng: random.Random) -> tuple[int, int]:
    """Zieht ein Endergebnis (h, a) aus der DC-Wahrscheinlichkeitsmatrix."""
    flat = matrix.flatten()
    n = len(flat)
    r = rng.random()
    cum = 0.0
    for k in range(n):
        cum += flat[k]
        if r <= cum:
            return divmod(k, matrix.shape[1])
    return divmod(n - 1, matrix.shape[1])


def _build_matrix_cache(params, elo: dict, stage: str,
                       teams: list[str], neutral: bool = True) -> dict:
    """Baut DC-Matrizen für alle Paare aus `teams`.
    Keyed by frozenset({canonical_home, canonical_away}).
    Wir cachen die Matrix mit Heim-Vorteil = neutral (True für WM).
    """
    cache: dict = {}
    rho = get_stage_rho(params, stage=stage, is_knockout=(stage != "group"))
    for i in range(len(teams)):
        for j in range(len(teams)):
            if i == j:
                continue
            home, away = teams[i], teams[j]
            ch, ca = canonical_name(home), canonical_name(away)
            if ch not in params.attack or ca not in params.attack:
                continue
            key = (ch, ca)
            if key in cache:
                continue
            try:
                m = predict_scoreline(
                    ch, ca, params, neutral=neutral,
                    rho_override=rho,
                    elo_home=elo.get(ch), elo_away=elo.get(ca),
                )
                cache[key] = m
            except Exception:
                continue
    return cache


def _simulate_group_stage(remaining: list[tuple[str, str, str]],
                          fixed_results: dict[frozenset, tuple[int, int, str, str]],
                          matrix_cache: dict, rng: random.Random) -> dict:
    """Simuliert verbleibende Gruppenspiele und liefert die Endplatzierung.

    fixed_results: {pair_key: (home_score, away_score, home_name, away_name)}
    Returns: {team: {pts, gd, gf, group}}
    """
    standings: dict[str, dict] = {}
    for grp, teams in WM_GROUPS.items():
        for t in teams:
            standings[t] = {"pts": 0, "gd": 0, "gf": 0, "group": grp}

    # Fest gespielte Ergebnisse
    for pk, (hs, as_, hname, aname) in fixed_results.items():
        if hname not in standings or aname not in standings:
            continue
        _apply(standings, hname, aname, hs, as_)

    # Verbleibende Spiele sampeln
    for grp, h, a in remaining:
        ch, ca = canonical_name(h), canonical_name(a)
        m = matrix_cache.get((ch, ca))
        if m is None:
            m = matrix_cache.get((ca, ch))
            if m is None:
                continue
            sa, sh = _sample_score(m, rng)  # gespiegelt
        else:
            sh, sa = _sample_score(m, rng)
        _apply(standings, h, a, sh, sa)

    return standings


def _apply(standings: dict, home: str, away: str, hs: int, as_: int) -> None:
    if home not in standings or away not in standings:
        return
    if hs > as_:
        standings[home]["pts"] += 3
    elif hs < as_:
        standings[away]["pts"] += 3
    else:
        standings[home]["pts"] += 1
        standings[away]["pts"] += 1
    standings[home]["gf"] += hs
    standings[home]["gd"] += hs - as_
    standings[away]["gf"] += as_
    standings[away]["gd"] += as_ - hs


def _qualifiers(standings: dict) -> list[str]:
    """Top 2 jeder Gruppe + 8 beste Drittplatzierte."""
    by_group: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for team, st in standings.items():
        by_group[st["group"]].append((team, st))
    qualified: list[str] = []
    thirds: list[tuple[str, dict]] = []
    for grp, rows in by_group.items():
        rows.sort(key=lambda x: (-x[1]["pts"], -x[1]["gd"], -x[1]["gf"]))
        qualified.append(rows[0][0])
        qualified.append(rows[1][0])
        thirds.append((rows[2][0], rows[2][1]))
    thirds.sort(key=lambda x: (-x[1]["pts"], -x[1]["gd"], -x[1]["gf"]))
    qualified.extend(t for t, _ in thirds[:8])
    return qualified  # 32 teams


def _build_ko_matrix_cache(params, elo: dict, all_teams: list[str]) -> dict:
    """Vorberechnung aller paarweisen KO-Matrizen (rho identisch für r16..final).

    Returns {(canonical_home, canonical_away): matrix}.
    Ein Lookup, dann Sampling — vermeidet O(N_trials × 31 KO-Matches) DC-Aufrufe.
    """
    cache: dict = {}
    rho = get_stage_rho(params, stage="r16", is_knockout=True)
    for h in all_teams:
        ch = canonical_name(h)
        if ch not in params.attack:
            continue
        for a in all_teams:
            if h == a:
                continue
            ca = canonical_name(a)
            if ca not in params.attack:
                continue
            key = (ch, ca)
            if key in cache:
                continue
            try:
                cache[key] = predict_scoreline(
                    ch, ca, params, neutral=True, rho_override=rho,
                    elo_home=elo.get(ch), elo_away=elo.get(ca),
                )
            except Exception:
                continue
    return cache


def _simulate_ko(qualifiers: list[str], ko_matrix_cache: dict, rng: random.Random) -> dict:
    """Simuliert KO-Bracket. Returns {team: stage_name} — höchster erreichter Stage.

    Logik: Vor jeder Runde haben die noch lebenden Teams diese Runde *erreicht*.
    Verlierer behalten ihren letzten Stage; der Final-Gewinner wird "champion".
    """
    reached: dict[str, str] = {}
    current = qualifiers[:]
    rng.shuffle(current)
    for stage in KO_STAGES:  # r32, r16, qf, sf, final
        for t in current:
            reached[t] = stage
        next_round: list[str] = []
        for i in range(0, len(current), 2):
            if i + 1 >= len(current):
                next_round.append(current[i])
                continue
            h, a = current[i], current[i + 1]
            ch, ca = canonical_name(h), canonical_name(a)
            m = ko_matrix_cache.get((ch, ca))
            if m is None:
                m = ko_matrix_cache.get((ca, ch))
                if m is None:
                    winner = rng.choice([h, a])
                    next_round.append(winner)
                    continue
                sa, sh = _sample_score(m, rng)
            else:
                sh, sa = _sample_score(m, rng)
            if sh > sa:
                winner = h
            elif sa > sh:
                winner = a
            else:
                winner = rng.choice([h, a])  # PEN ≈ 50/50
            next_round.append(winner)
        current = next_round
    if current:
        reached[current[0]] = "champion"
    return reached


STAGE_LABELS = {
    "group_out": "Gruppen-Aus",
    "r32":       "Achtelfinale (R32)",
    "r16":       "Sechzehntelfinale (R16)",
    "qf":        "Viertelfinale",
    "sf":        "Halbfinale",
    "final":     "Finale",
    "champion":  "Weltmeister",
}
STAGE_ORDER = ["group_out", "r32", "r16", "qf", "sf", "final", "champion"]


def _build_xpoints(params, elo: dict, results: list[dict]) -> list[dict]:
    """Liefert Über-/Unter-Performance pro Team: realisierte Punkte vs DC-Erwartung.

    Für jedes gespielte WM-Spiel:
      xPts_h = P(home_win)*3 + P(draw)*1   |   actual_h = 3/1/0
    Aggregation: Σ xPts vs Σ actual → Diff signalisiert Über-/Unterperformance.
    Liefert auch Goals_for/against vs xG (DC predict_xg).
    """
    from src.models.dixon_coles import predict_match_staged, predict_xg
    name_lookup: dict[str, str] = {}
    for teams in WM_GROUPS.values():
        for t in teams:
            name_lookup[canonical_name(t)] = t
    rows: dict[str, dict] = {t: {"team": t, "n": 0, "pts": 0, "xpts": 0.0,
                                  "gf": 0, "ga": 0, "xgf": 0.0, "xga": 0.0,
                                  "group": next(g for g, ts in WM_GROUPS.items() if t in ts)}
                              for ts in WM_GROUPS.values() for t in ts}
    for r in results:
        h = name_lookup.get(canonical_name(r.get("home", "")))
        a = name_lookup.get(canonical_name(r.get("away", "")))
        if not h or not a:
            continue
        hs, as_ = r.get("home_score"), r.get("away_score")
        if hs is None or as_ is None:
            continue
        ch, ca = canonical_name(h), canonical_name(a)
        if ch not in params.attack or ca not in params.attack:
            continue
        try:
            probs = predict_match_staged(
                ch, ca, params, is_knockout=False, stage="group", neutral=True,
                elo_home=elo.get(ch), elo_away=elo.get(ca),
            )
            xg_h, xg_a = predict_xg(ch, ca, params, neutral=True)
        except Exception:
            continue
        xpts_h = probs["p_home"] * 3 + probs["p_draw"] * 1
        xpts_a = probs["p_away"] * 3 + probs["p_draw"] * 1
        pts_h = 3 if hs > as_ else (1 if hs == as_ else 0)
        pts_a = 3 if as_ > hs else (1 if as_ == hs else 0)
        rows[h]["n"] += 1
        rows[h]["pts"] += pts_h
        rows[h]["xpts"] += xpts_h
        rows[h]["gf"] += hs
        rows[h]["ga"] += as_
        rows[h]["xgf"] += xg_h
        rows[h]["xga"] += xg_a
        rows[a]["n"] += 1
        rows[a]["pts"] += pts_a
        rows[a]["xpts"] += xpts_a
        rows[a]["gf"] += as_
        rows[a]["ga"] += hs
        rows[a]["xgf"] += xg_a
        rows[a]["xga"] += xg_h
    out = []
    for t, d in rows.items():
        if d["n"] == 0:
            continue
        out.append({
            "team":      t,
            "group":     d["group"],
            "n":         d["n"],
            "pts":       d["pts"],
            "xpts":      round(d["xpts"], 2),
            "diff":      round(d["pts"] - d["xpts"], 2),
            "gf":        d["gf"],
            "ga":        d["ga"],
            "xgf":       round(d["xgf"], 2),
            "xga":       round(d["xga"], 2),
            "gf_diff":   round(d["gf"] - d["xgf"], 2),
            "ga_diff":   round(d["ga"] - d["xga"], 2),
        })
    out.sort(key=lambda x: -x["diff"])
    return out


def run_forecast(n: int = 2000, seed: int = 42) -> dict:
    params = _load_latest_dc_params()
    if params is None:
        return {"error": "DC params not found"}
    elo = _load_elo()
    results = _load_results()
    rng = random.Random(seed)
    np.random.seed(seed)

    # Map gespielte Resultate auf Display-Namen aus WM_GROUPS (canonical for matching)
    name_lookup: dict[str, str] = {}
    for teams in WM_GROUPS.values():
        for t in teams:
            name_lookup[canonical_name(t)] = t

    fixed: dict[frozenset, tuple[int, int, str, str]] = {}
    for r in results:
        h = name_lookup.get(canonical_name(r.get("home", "")))
        a = name_lookup.get(canonical_name(r.get("away", "")))
        if not h or not a:
            continue
        hs = r.get("home_score")
        as_ = r.get("away_score")
        if hs is None or as_ is None:
            continue
        fixed[_pair_key(h, a)] = (int(hs), int(as_), h, a)

    # Verbleibende Gruppenspiele
    all_pairs = _all_group_pairs()
    remaining = [(g, h, a) for (g, h, a) in all_pairs if _pair_key(h, a) not in fixed]

    # Matrix-Cache: Gruppenspiele (rho_group) + KO-Spiele (rho_ko, alle KO-Stages identisch)
    matrix_cache: dict = {}
    for grp, teams in WM_GROUPS.items():
        matrix_cache.update(_build_matrix_cache(params, elo, "group", teams, neutral=True))
    teams_all = [t for ts in WM_GROUPS.values() for t in ts]
    ko_matrix_cache = _build_ko_matrix_cache(params, elo, teams_all)
    print(f"Matrix-Cache: {len(matrix_cache)} Gruppen + {len(ko_matrix_cache)} KO")

    # Aggregations-Buckets
    counts: dict[str, dict[str, int]] = {
        t: {s: 0 for s in STAGE_ORDER} for t in teams_all
    }
    group_finish: dict[str, dict[int, int]] = {t: {1: 0, 2: 0, 3: 0, 4: 0} for t in teams_all}

    for trial in range(n):
        standings = _simulate_group_stage(remaining, fixed, matrix_cache, rng)
        # Tabellenplatz pro Team in seiner Gruppe
        by_group: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for team, st in standings.items():
            by_group[st["group"]].append((team, st))
        for grp, rows in by_group.items():
            rows.sort(key=lambda x: (-x[1]["pts"], -x[1]["gd"], -x[1]["gf"]))
            for pos, (team, _) in enumerate(rows, start=1):
                group_finish[team][pos] += 1
        qualifiers = _qualifiers(standings)
        eliminated = set(teams_all) - set(qualifiers)
        for t in eliminated:
            counts[t]["group_out"] += 1
        reached = _simulate_ko(qualifiers, ko_matrix_cache, rng)
        for t, st in reached.items():
            counts[t][st] += 1

    # Wahrscheinlichkeiten + kumulative P(advance)
    teams_out: list[dict] = []
    for t in teams_all:
        row = counts[t]
        gf = group_finish[t]
        p_advance = (n - row["group_out"]) / n  # R32 oder besser
        p_r16 = sum(row[s] for s in ["r16", "qf", "sf", "final", "champion"]) / n
        p_qf = sum(row[s] for s in ["qf", "sf", "final", "champion"]) / n
        p_sf = sum(row[s] for s in ["sf", "final", "champion"]) / n
        p_final = sum(row[s] for s in ["final", "champion"]) / n
        p_champ = row["champion"] / n
        teams_out.append({
            "team":       t,
            "group":      next(g for g, teams in WM_GROUPS.items() if t in teams),
            "p_first":    round(gf[1] / n * 100, 1),
            "p_second":   round(gf[2] / n * 100, 1),
            "p_third":    round(gf[3] / n * 100, 1),
            "p_fourth":   round(gf[4] / n * 100, 1),
            "p_advance":  round(p_advance * 100, 1),
            "p_r16":      round(p_r16 * 100, 1),
            "p_qf":       round(p_qf * 100, 1),
            "p_sf":       round(p_sf * 100, 1),
            "p_final":    round(p_final * 100, 1),
            "p_champion": round(p_champ * 100, 2),
        })
    teams_out.sort(key=lambda x: -x["p_champion"])
    xpoints = _build_xpoints(params, elo, results)
    bracket = _build_bracket_preview(teams_out, params, elo)
    return {
        "updated":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_trials":   n,
        "n_played":   len(fixed),
        "n_remaining_group": len(remaining),
        "teams":      teams_out,
        "xpoints":    xpoints,
        "bracket":    bracket,
    }


def _build_bracket_preview(teams_out: list[dict], params, elo: dict) -> dict:
    """Deterministische Bracket-Vorschau für den aktuellen Stand.

    Methode:
      1. Wähle Top-2 jeder Gruppe (nach p_first+p_second) und die 8 besten
         Drittplatzierten (nach p_advance unter Nicht-Top-2) → 32 Qualifizierte.
      2. Setze sie nach p_qf absteigend (1=stärkster Pfad-Erwartungswert).
      3. Klassisches Seeded-Bracket: seed1 vs seed32, seed2 vs seed31, ...
      4. Pro Paarung: DC-Vorhersage neutral, höhere P(Sieg) gewinnt (deterministisch).
      5. Wiederhole für R32 → R16 → QF → SF → Final.

    Vereinfachung: Kein offizielles FIFA-Bracket-Mapping (folgt nach Auslosung).
    Daher als "Approximation" im Frontend markieren.
    """
    from src.models.dixon_coles import predict_match_staged

    by_grp: dict[str, list[dict]] = defaultdict(list)
    for t in teams_out:
        by_grp[t["group"]].append(t)

    qual: list[dict] = []
    third_cands: list[dict] = []
    for grp, rows in by_grp.items():
        rows_sorted = sorted(rows, key=lambda x: -(x["p_first"] + x["p_second"]))
        top2 = rows_sorted[:2]
        for t in top2:
            qual.append(t)
        # 3.: most-likely non-top2 to be in best-3rds bucket
        rest = [r for r in rows if r not in top2]
        rest_sorted = sorted(rest, key=lambda x: -x["p_advance"])
        if rest_sorted:
            third_cands.append(rest_sorted[0])

    third_cands.sort(key=lambda x: -x["p_advance"])
    qual.extend(third_cands[:8])

    if len(qual) != 32:
        return {"error": f"need 32 qualifiers, got {len(qual)}", "rounds": []}

    qual.sort(key=lambda x: -x["p_qf"])  # seed 1 = strongest path
    bracket_seeds = qual[:]

    def _predict_winner(home_team: str, away_team: str) -> dict:
        ch, ca = canonical_name(home_team), canonical_name(away_team)
        if ch not in params.attack or ca not in params.attack:
            return {"home": home_team, "away": away_team,
                    "p_home": 0.5, "p_draw": 0.0, "p_away": 0.5,
                    "winner": home_team, "p_winner": 0.5}
        try:
            probs = predict_match_staged(
                ch, ca, params, is_knockout=True, stage="r16", neutral=True,
                elo_home=elo.get(ch), elo_away=elo.get(ca),
            )
        except Exception:
            return {"home": home_team, "away": away_team,
                    "p_home": 0.5, "p_draw": 0.0, "p_away": 0.5,
                    "winner": home_team, "p_winner": 0.5}
        ph, pa, pd = probs["p_home"], probs["p_away"], probs["p_draw"]
        # KO: draw aufteilen 50/50 → effektive Sieg-Wkt.
        p_h_eff = ph + pd / 2
        p_a_eff = pa + pd / 2
        winner = home_team if p_h_eff >= p_a_eff else away_team
        return {
            "home": home_team, "away": away_team,
            "p_home": round(p_h_eff * 100, 1),
            "p_draw": round(pd * 100, 1),
            "p_away": round(p_a_eff * 100, 1),
            "winner": winner,
            "p_winner": round(max(p_h_eff, p_a_eff) * 100, 1),
        }

    rounds_out: list[dict] = []
    stage_labels = [("r32", "Sechzehntelfinale (R32)"),
                    ("r16", "Achtelfinale (R16)"),
                    ("qf",  "Viertelfinale"),
                    ("sf",  "Halbfinale"),
                    ("final", "Finale")]
    current = bracket_seeds[:]
    for stage_key, stage_lbl in stage_labels:
        matches = []
        next_round = []
        # Classic seeded pairing within current list
        n_cur = len(current)
        for i in range(n_cur // 2):
            h, a = current[i], current[n_cur - 1 - i]
            m = _predict_winner(h["team"], a["team"])
            m["home_group"] = h["group"]
            m["away_group"] = a["group"]
            m["home_seed"] = i + 1
            m["away_seed"] = n_cur - i
            matches.append(m)
            winner_obj = h if m["winner"] == h["team"] else a
            next_round.append(winner_obj)
        rounds_out.append({"stage": stage_key, "label": stage_lbl, "matches": matches})
        current = next_round

    champion = current[0] if current else None
    return {
        "rounds": rounds_out,
        "champion": {"team": champion["team"], "group": champion["group"]} if champion else None,
        "note": "Vereinfachte Approximation — offizielles FIFA-Bracket-Mapping folgt nach Auslosung. "
                "Seeded-Pairing nach P(QF) Σ, deterministische Auswahl je Match (höhere DC-Sieg-Wkt.).",
    }


def main():
    parser = argparse.ArgumentParser(description="WM 2026 Monte Carlo Forecast")
    parser.add_argument("--n", type=int, default=2000, help="Anzahl Trials")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=str,
                        default=str(ROOT / "docs" / "data" / "wm_forecast.json"))
    args = parser.parse_args()
    out = run_forecast(n=args.n, seed=args.seed)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    if "error" in out:
        print(f"ERROR: {out['error']}")
        sys.exit(1)
    print(f"Forecast geschrieben: {args.output}")
    print(f"  Trials: {out['n_trials']}, gespielt: {out['n_played']}, "
          f"verbleibende Gruppenspiele: {out['n_remaining_group']}")
    print("  Top 5 Champion-Chancen:")
    for row in out["teams"][:5]:
        print(f"    {row['team']:<25} {row['p_champion']:>5.2f}%  "
              f"P(SF)={row['p_sf']:>5.1f}%  P(R16)={row['p_r16']:>5.1f}%")


if __name__ == "__main__":
    main()
