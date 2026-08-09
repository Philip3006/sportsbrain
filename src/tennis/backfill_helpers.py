from __future__ import annotations

from datetime import date, timedelta

from src.data.tennis_scores import canonical_match_key, fetch_tennis_scores_espn

_NICKNAME_MAP: dict[str, str] = {
    "caty": "catherine",
    "cat": "catherine",
    "nacho": "ignacio",
    "rafa": "rafael",
    "feli": "felipe",
}

GHOST_AGE_DAYS = 14


def name_variants(name: str) -> list[str]:
    parts = name.strip().split()
    seen: set[str] = set()
    variants: list[str] = []

    def _add(v: str) -> None:
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    _add(name)
    if parts and parts[0].lower() in _NICKNAME_MAP:
        parts_nn = [_NICKNAME_MAP[parts[0].lower()]] + parts[1:]
        _add(" ".join(parts_nn))
    suffixes = {"jr.", "jr", "sr.", "sr", "ii", "iii"}
    cleaned = [p for p in parts if p.lower().rstrip(".") not in suffixes]
    if cleaned != parts:
        _add(" ".join(cleaned))
        parts = cleaned
    n = len(parts)
    if n >= 3:
        _add(" ".join(parts[1:]))
        _add(" ".join(parts[:-1]))
        _add(" ".join(parts[1:-1]))
    if n == 2:
        _add(f"{parts[1]} {parts[0]}")
    return variants


def lookup_tennis_score(home: str, away: str, mid: str, tennis_scores: dict) -> dict | None:
    sc_id = tennis_scores.get(mid)
    sc_name = tennis_scores.get(f"{home} vs {away}")
    sc_variant: dict | None = None
    for h_var in name_variants(home):
        for a_var in name_variants(away):
            sc_variant = (
                tennis_scores.get(f"{h_var} vs {a_var}")
                or tennis_scores.get(f"{a_var} vs {h_var}")
                or tennis_scores.get(canonical_match_key(h_var, a_var))
            )
            if sc_variant:
                break
        if sc_variant:
            break
    candidates = [s for s in (sc_name, sc_id, sc_variant) if s]
    return next((s for s in candidates if s.get("sets")), None) or (candidates[0] if candidates else None)


def build_espn_date_window(scan_dates_raw: list[str], window_days: int = 7) -> set[str]:
    fetch_dates: set[str] = set()
    today = date.today()
    for sd in scan_dates_raw:
        try:
            d = date.fromisoformat(sd)
            for offset in range(window_days + 1):
                candidate = d + timedelta(days=offset)
                if candidate <= today:
                    fetch_dates.add(candidate.strftime("%Y%m%d"))
        except ValueError:
            pass
    return fetch_dates


def fetch_espn_window(scan_dates_raw: list[str], existing: dict, window_days: int = 7) -> tuple[dict, int]:
    fetch_dates = build_espn_date_window(scan_dates_raw, window_days)
    tennis_scores = dict(existing)
    for date_str in sorted(fetch_dates):
        for tour in ("atp", "wta"):
            hist = fetch_tennis_scores_espn(tour=tour, dates=date_str)
            for k, v in hist.items():
                if v.get("sets") and (k not in tennis_scores or not tennis_scores[k].get("sets")):
                    tennis_scores[k] = v
    return tennis_scores, len(fetch_dates)
