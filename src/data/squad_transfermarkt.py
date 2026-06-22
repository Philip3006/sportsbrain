"""Transfermarkt squad scraper (Playwright, JS-rendered) and FBRef stub."""
from __future__ import annotations

import pandas as pd

from .squad_models import (
    PlayerStatus,
    _TM_TEAMS, _UA, _POS_MAP, _RETURN_RE, _parse_market_value_m,
    _cache_path, _cache_fresh, _save_cache, _load_cache,
)


def fetch_transfermarkt_squad(
    team: str,
    match_date: pd.Timestamp,
    force: bool = False,
) -> list[PlayerStatus]:
    """
    Scrapes Transfermarkt kader page (JS-rendered) for a national team.
    Finds span.verletzt-table (injuries) and span.gesperrt-table (suspensions).
    Returns list of unavailable PlayerStatus. Caches 24h.
    Returns [] on any failure → caller falls back to default_report.
    """
    if team not in _TM_TEAMS:
        return []

    cache_file = _cache_path(team)
    if not force and _cache_fresh(cache_file):
        return _load_cache(cache_file)

    slug, team_id = _TM_TEAMS[team]
    # Try current year first (WM 2026 season), then previous year as fallback.
    # National team pages use different saison_id conventions than clubs.
    players = []
    for saison in (str(match_date.year), str(match_date.year - 1)):
        url = f"https://www.transfermarkt.com/{slug}/kader/verein/{team_id}/saison_id/{saison}"
        players = _scrape_kader_playwright(url, match_date)
        if players:
            break

    if players:
        _save_cache(cache_file, players)
    else:
        print(f"  [squad] {team}: 0 players returned (TM blocked or page structure changed) — not caching")
    return players


def _scrape_kader_playwright(url: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Uses Playwright to load the full kader page.
    Extracts ALL squad members (fit + unavailable) so availability_score is accurate.
    Injury/suspension status detected via span.verletzt-table / span.gesperrt-table.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [squad] Playwright not installed — run: pip3 install playwright && "
              "python3 -m playwright install chromium")
        return []

    players = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=_UA,
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                },
            )
            page = ctx.new_page()
            # Remove navigator.webdriver flag that TM uses to detect headless bots
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)

            # Select main player rows: those with the jersey-number td (rueckennummer).
            # This td only appears in the outer player row, not in inline-table sub-rows,
            # avoiding duplicates from the nested inline-table structure.
            rows = page.query_selector_all("table.items tbody tr:has(td.rueckennummer)")
            if not rows:
                # Fallback: some TM national team pages use a different layout.
                # Wait longer for lazy-loaded content and try a broader selector.
                page.wait_for_timeout(2000)
                rows = page.query_selector_all("table.items tbody tr:has(td.rueckennummer)")
            if not rows:
                # Second fallback: try without :has() — filter by hauptlink presence.
                rows = [r for r in page.query_selector_all("table.items tbody tr")
                        if r.query_selector("td.hauptlink a")]
            for row in rows:
                name_a = row.query_selector("td.hauptlink a")
                if not name_a:
                    continue

                # Extract text nodes only (excludes span nbsp; text)
                name = name_a.evaluate(
                    "el => Array.from(el.childNodes)"
                    "      .filter(n => n.nodeType === 3)"
                    "      .map(n => n.textContent.trim())"
                    "      .join('')"
                ).strip()
                if not name:
                    continue

                # Position: jersey number td has title="Goalkeeper" / "Centre-Back" etc.
                pos_td = row.query_selector("td.zentriert.rueckennummer[title]")
                pos_raw = pos_td.get_attribute("title") if pos_td else ""
                position = _map_position(pos_raw or "")

                # Status from embedded injury/suspension spans
                status = "fit"
                availability = 1.0

                verletzt = row.query_selector("span.verletzt-table")
                gesperrt = row.query_selector("span.gesperrt-table")

                if verletzt:
                    title_text = verletzt.get_attribute("title") or ""
                    until_date = _parse_return_date(title_text, match_date)
                    if until_date is None or until_date >= match_date:
                        status = "injured"
                        availability = 0.0
                elif gesperrt:
                    status = "suspended"
                    availability = 0.0

                # Market value: TM kader page has a right-aligned hauptlink cell
                # at the end of each row with values like "€10.00m" / "€500k".
                mv_eur_m = 0.0
                mv_td = row.query_selector("td.rechts.hauptlink") or row.query_selector("td.rechts:last-child")
                if mv_td:
                    mv_eur_m = _parse_market_value_m(mv_td.inner_text() or "")

                players.append(PlayerStatus(
                    name=name,
                    position=position,
                    availability=availability,
                    status=status,
                    key_player=True,  # marked after full squad is loaded
                    market_value_eur_m=mv_eur_m,
                ))

            browser.close()
    except Exception as exc:
        print(f"  [squad] Playwright scrape failed: {exc}")

    return players


def _parse_return_date(title: str, match_date: pd.Timestamp) -> pd.Timestamp | None:
    """
    Extracts return date from Transfermarkt span title.
    Format: 'Ankle injury - Return expected on 20/07/2026'
    Returns None when unknown (= treat as still unavailable).
    """
    m = _RETURN_RE.search(title)
    if not m:
        return None
    try:
        return pd.to_datetime(m.group(1), dayfirst=True)
    except Exception:
        return None


def _map_position(text: str) -> str:
    lower = text.lower()
    for key, mapped in _POS_MAP.items():
        if key in lower:
            return mapped
    return "unknown"


# ---------------------------------------------------------------------------
# FBRef stub (future integration)
# ---------------------------------------------------------------------------

def fetch_fbref_player_form(
    team: str,
    before_date: pd.Timestamp,
    n_matches: int = 8,
) -> dict[str, float]:
    """
    STUB: Fetch player-level xG/xA from FBref.
    Returns empty dict until implemented (soccerdata package required).
    """
    return {}
