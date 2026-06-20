"""
WM 2026 Sperren-Tracking — Multi-Source-Scraper mit Confidence-Score.

Quellen:
  - FIFA.com /news                 (Gewicht 3 — offiziell)
  - UEFA.com /news                 (Gewicht 2 — offiziell europäisch)
  - BBC Sport Football             (Gewicht 1)
  - ESPN Soccer                    (Gewicht 1)

Confidence-Score pro Spieler:
  + Source-Gewicht (siehe oben)
  + 2  falls Spielername in WM-Squad-Cache verifiziert
  + 2  falls ≥ 2 unabhängige Quellen denselben Spieler nennen
  − 1  falls Artikel älter als 14 Tage

Auto-Merge ab Score ≥ 5 → data/suspensions.json
Sonst → data/suspensions_candidates.json (manuelle Review via add_suspension.py)

Aufruf:
    python scripts/scrape_suspensions.py                   # Live-Modus (Push + Merge)
    python scripts/scrape_suspensions.py --dry-run         # Kein Schreiben, kein Push
    python scripts/scrape_suspensions.py --threshold 7     # Score-Schwelle überschreiben
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Project root on sys.path so this can be run directly
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._http_retry import retry_request  # noqa: E402

_SUSPENSIONS_FILE = _ROOT / "data" / "suspensions.json"
_CANDIDATES_FILE = _ROOT / "data" / "suspensions_candidates.json"
_SQUAD_CACHE_DIR = _ROOT / "data" / "cache" / "squad"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SportsBrain/1.0; "
        "+https://github.com/Philip3006/sportsbrain) "
        "suspension-tracker"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Schlüsselwörter, die auf eine Sperre hindeuten (case-insensitive)
_SUSPENSION_KEYWORDS = [
    r"\bsuspend(?:ed|s|ing|ion)?\b",
    r"\bban(?:ned|s|ning)?\b",
    r"\bdisciplinar(?:y|ily)\b",
    r"\bsent off\b",
    r"\bred card(?:s|ed)?\b",
    r"\btwo yellow cards?\b",
    r"\byellow card accumulation\b",
    r"\bwill miss\b",
    r"\bruled out\b.{0,50}\b(suspend|ban|card)\b",
]
_KW_RE = re.compile("|".join(_SUSPENSION_KEYWORDS), re.IGNORECASE)

# Title-Case Name (2-3 Wörter), erlaubt á/é/ñ/etc.
_NAME_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÑÄÖÜČŠŽĆŚŁŃŻŘĐŞĞ][a-záéíóúñäöüčšžćśłńżřđşğ'’\-]+"
    r"(?:\s+[A-ZÁÉÍÓÚÑÄÖÜČŠŽĆŚŁŃŻŘĐŞĞ][a-záéíóúñäöüčšžćśłńżřđşğ'’\-]+){1,2})\b"
)

# Stopwords — Wörter, die wie Namen aussehen aber keine sind
_NAME_STOPWORDS = {
    "World Cup", "FIFA World", "World Cup 2026", "United States",
    "South Korea", "South Africa", "Cape Verde", "New Zealand",
    "Saudi Arabia", "Czech Republic", "Ivory Coast", "DR Congo",
    "FIFA Disciplinary", "Disciplinary Committee", "Red Card",
    "Yellow Card", "Group Stage", "Round Of", "Match Day",
    "First Half", "Second Half", "Premier League", "Champions League",
    "European Championship", "Football Association",
}


@dataclass
class Source:
    name: str
    url: str
    weight: int
    enabled: bool = True


_SOURCES: list[Source] = [
    Source("FIFA",  "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/news", 3),
    Source("UEFA",  "https://www.uefa.com/news/", 2),
    Source("BBC",   "https://www.bbc.com/sport/football/world-cup", 1),
    Source("ESPN",  "https://www.espn.com/soccer/fifa-world-cup", 1),
]


@dataclass
class Candidate:
    """Einzelner Sperren-Kandidat."""
    player: str
    team: str | None = None
    score: int = 0
    sources: set[str] = field(default_factory=set)
    snippets: list[str] = field(default_factory=list)
    squad_verified: bool = False


# ── Source Fetching ──────────────────────────────────────────────


def _fetch_source(src: Source, *, timeout: int = 15) -> str:
    """Holt HTML einer Quelle. Gibt '' bei Fehler zurück (graceful)."""
    try:
        resp = retry_request(
            "GET", src.url, headers=_HEADERS, timeout=timeout,
            log_prefix=f"[susp:{src.name}]",
        )
    except Exception as exc:
        print(f"  [susp:{src.name}] fetch failed — {exc}")
        return ""
    if resp.status_code != 200:
        print(f"  [susp:{src.name}] HTTP {resp.status_code}")
        return ""
    return resp.text


def _strip_html(html: str) -> str:
    """Schneller HTML-zu-Text-Strip ohne bs4-Dependency."""
    # Script/style entfernen
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Tags raus
    text = re.sub(r"<[^>]+>", " ", html)
    # HTML-Entities (minimal)
    text = (
        text.replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&nbsp;", " ").replace("&#39;", "'")
            .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", text).strip()


# ── Extraction ───────────────────────────────────────────────────


def _windows_with_keyword(text: str, *, window: int = 200) -> Iterable[str]:
    """Yields Text-Fenster um jedes Sperren-Keyword."""
    for m in _KW_RE.finditer(text):
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        yield text[start:end]


def _extract_names(snippet: str) -> set[str]:
    """Extrahiert Spielernamen-Kandidaten aus einem Snippet."""
    names: set[str] = set()
    for m in _NAME_RE.finditer(snippet):
        name = m.group(1).strip()
        if name in _NAME_STOPWORDS:
            continue
        # Häufige False-Positives: Monatsnamen, Wochentage, Continent-Namen
        first_word = name.split()[0]
        if first_word in {
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday", "North", "South", "Saudi", "United",
            "Czech", "Ivory", "Cape", "New", "Republic",
        }:
            continue
        names.add(name)
    return names


# ── Squad-Cache-Verifikation ─────────────────────────────────────


def _load_known_squad_players() -> dict[str, str]:
    """
    Lädt alle bekannten WM-Spieler aus dem Squad-Cache.
    Returns: dict[player_name -> team_canonical_name]
    """
    players: dict[str, str] = {}
    if not _SQUAD_CACHE_DIR.exists():
        return players
    for cache_file in _SQUAD_CACHE_DIR.glob("*_wiki.json"):
        team_slug = cache_file.stem.replace("_wiki", "")
        try:
            data = json.loads(cache_file.read_text())
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        team_label = team_slug.replace("_", " ").title()
        for entry in data:
            if isinstance(entry, dict) and entry.get("name"):
                players[entry["name"]] = team_label
    return players


# ── Aggregation & Scoring ────────────────────────────────────────


def collect_candidates(
    sources: list[Source] = _SOURCES,
    *,
    fetch: callable = _fetch_source,
) -> dict[str, Candidate]:
    """
    Holt alle Quellen, extrahiert Spielernamen aus Snippets mit Sperren-Keywords,
    aggregiert pro Spieler über alle Quellen.
    """
    squad_lookup = _load_known_squad_players()
    candidates: dict[str, Candidate] = {}

    for src in sources:
        if not src.enabled:
            continue
        html = fetch(src)
        if not html:
            continue
        text = _strip_html(html)
        for snippet in _windows_with_keyword(text):
            for name in _extract_names(snippet):
                cand = candidates.setdefault(name, Candidate(player=name))
                if src.name not in cand.sources:
                    cand.sources.add(src.name)
                    cand.score += src.weight
                if name in squad_lookup and not cand.squad_verified:
                    cand.squad_verified = True
                    cand.team = squad_lookup[name]
                    cand.score += 2
                # Snippet kurz für Audit speichern
                if len(cand.snippets) < 3:
                    cand.snippets.append(snippet[:300].strip())
        # Rate-Limit
        time.sleep(1.0)

    # Bonus: ≥ 2 unabhängige Quellen
    for cand in candidates.values():
        if len(cand.sources) >= 2:
            cand.score += 2

    return candidates


# ── Merge & Persistence ──────────────────────────────────────────


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"  [susp] {path}: load failed — {exc}")
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def split_by_threshold(
    candidates: dict[str, Candidate], threshold: int,
) -> tuple[list[Candidate], list[Candidate]]:
    """Returns (auto_merge, manual_review)."""
    auto, manual = [], []
    for cand in candidates.values():
        if cand.team and cand.score >= threshold:
            auto.append(cand)
        else:
            manual.append(cand)
    return auto, manual


def merge_into_suspensions(auto: list[Candidate]) -> list[Candidate]:
    """Trägt Auto-Merge-Kandidaten in suspensions.json ein. Returns die neu hinzugefügten."""
    data = _load_json(_SUSPENSIONS_FILE, default={})
    added: list[Candidate] = []
    for cand in auto:
        assert cand.team is not None
        team_entry = data.setdefault(cand.team, [])
        if cand.player not in team_entry:
            team_entry.append(cand.player)
            added.append(cand)
    if added:
        data["_injuries_last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _save_json(_SUSPENSIONS_FILE, data)
    return added


def persist_candidates(manual: list[Candidate]) -> None:
    """Schreibt Kandidaten mit niedrigem Score in suspensions_candidates.json."""
    payload = {
        "_updated": datetime.now(timezone.utc).isoformat(),
        "_note": "Spieler-Kandidaten unterhalb der Auto-Merge-Schwelle. Manuelle Review via scripts/add_suspension.py.",
        "candidates": [
            {
                "player": c.player,
                "team": c.team,
                "score": c.score,
                "sources": sorted(c.sources),
                "squad_verified": c.squad_verified,
                "snippets": c.snippets,
            }
            for c in sorted(manual, key=lambda x: -x.score)
        ],
    }
    _save_json(_CANDIDATES_FILE, payload)


# ── Push Notification ────────────────────────────────────────────


def _send_push(auto: list[Candidate], manual: list[Candidate]) -> None:
    try:
        from src.notifications.web_push import _send_notification
    except ImportError:
        print("  [susp] web_push nicht verfügbar — Push übersprungen.")
        return

    if not auto and not manual:
        return  # Stumm wenn nichts gefunden

    if auto:
        title = f"⚠️ {len(auto)} neue Sperre{'n' if len(auto) > 1 else ''} (auto-merged)"
        body_lines = [f"{c.team}: {c.player} (Score {c.score})" for c in auto[:5]]
        body = "\n".join(body_lines)
    else:
        title = f"🔍 {len(manual)} Sperren-Kandidat{'en' if len(manual) > 1 else ''} — Review nötig"
        top = sorted(manual, key=lambda x: -x.score)[:3]
        body = "\n".join(f"{c.player} (Score {c.score}, {','.join(sorted(c.sources))})" for c in top)

    try:
        _send_notification(title, body, url="/", kind="suspension", tag="suspension-scan")
    except Exception as exc:
        print(f"  [susp] push failed — {exc}")


# ── CLI ──────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="WM 2026 Sperren-Scraper")
    parser.add_argument("--dry-run", action="store_true",
                        help="Keine Datei-Schreibvorgänge, kein Push.")
    parser.add_argument("--threshold", type=int, default=5,
                        help="Auto-Merge-Score-Schwelle (Default 5).")
    parser.add_argument("--no-push", action="store_true",
                        help="Scrape & Merge, aber kein Push.")
    args = parser.parse_args()

    print(f"[susp] Sperren-Scan gestartet — {len(_SOURCES)} Quellen, Threshold={args.threshold}")
    candidates = collect_candidates()
    print(f"[susp] {len(candidates)} Spieler-Kandidaten extrahiert.")

    auto, manual = split_by_threshold(candidates, args.threshold)
    print(f"[susp] {len(auto)} Auto-Merge, {len(manual)} Review.")

    if args.dry_run:
        print("[susp] --dry-run: keine Persistierung.")
        for c in sorted(auto + manual, key=lambda x: -x.score)[:10]:
            print(f"  - {c.player} (team={c.team}, score={c.score}, sources={sorted(c.sources)})")
        return 0

    added = merge_into_suspensions(auto)
    persist_candidates(manual)

    if added:
        print(f"[susp] {len(added)} Sperren in suspensions.json gemergt:")
        for c in added:
            print(f"  + {c.team} → {c.player} (score={c.score})")

    if not args.no_push:
        _send_push(added, manual)

    return 0


if __name__ == "__main__":
    sys.exit(main())
