"""
Scrapes historical 1X2 odds from Betexplorer for WC/EURO tournaments using Playwright.
Betexplorer is fully JS-rendered — requests alone cannot access match data.

Setup (one-time):
  pip3 install playwright
  python3 -m playwright install chromium

Run via:
  python scripts/fetch_tournament_odds.py
  python scripts/fetch_tournament_odds.py --dry-run   # 5 matches per tournament
"""
import re
from pathlib import Path

import pandas as pd

from src.config import DATA_RAW, canonical_name

TOURNAMENT_SLUGS = {
    "WC2018":   ("world",         "world-cup-2018"),
    "WC2022":   ("world",         "world-cup-2022"),
    "EURO2020": ("europe",        "euro-2020"),
    "EURO2024": ("europe",        "euro-2024"),
    "CA2024":   ("south-america", "copa-america-2024"),
}

_RESULTS_URL = "https://www.betexplorer.com/football/{region}/{slug}/results/"


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise ImportError(
            "\nPlaywright not installed. Run:\n"
            "  pip3 install playwright\n"
            "  python3 -m playwright install chromium\n"
        )


def _slug_to_teams(match_slug: str) -> tuple[str, str] | None:
    """
    Extracts team names from URL slug like 'argentina-france' or 'south-korea-ghana'.
    Uses canonical_name() for normalization. Returns (home, away) or None.
    """
    # Common two-word country names to protect hyphen
    protected = [
        "south-korea", "north-korea", "saudi-arabia", "costa-rica",
        "united-states", "new-zealand", "ivory-coast", "burkina-faso",
        "sierra-leone", "trinidad-tobago", "cape-verde", "guinea-bissau",
        "equatorial-guinea", "central-africa", "south-africa",
        "bosnia-herzegovina", "north-macedonia",
    ]
    slug = match_slug.lower()

    # Try protected patterns first
    for pw in protected:
        if slug.startswith(pw + "-"):
            home_slug = pw
            away_slug = slug[len(pw) + 1:]
            home_raw = home_slug.replace("-", " ").title()
            away_raw = away_slug.replace("-", " ").title()
            return canonical_name(home_raw), canonical_name(away_raw)

    parts = slug.split("-")
    if len(parts) < 2:
        return None
    # Away team check from right side
    for away_slug in protected:
        if slug.endswith("-" + away_slug):
            home_slug = slug[: -(len(away_slug) + 1)]
            home_raw = home_slug.replace("-", " ").title()
            away_raw = away_slug.replace("-", " ").title()
            return canonical_name(home_raw), canonical_name(away_raw)

    # Default: first word = home, rest = away
    home_raw = parts[0].title()
    away_raw = " ".join(parts[1:]).title()
    return canonical_name(home_raw), canonical_name(away_raw)


def scrape_tournament(
    tournament: str,
    max_matches: int | None = None,
    headless: bool = True,
) -> pd.DataFrame:
    """
    Scrapes all match odds for one tournament from a single results page.
    Odds (1X2) are embedded directly in the table — no per-match API calls.
    Returns DataFrame: tournament, match_id, home_team, away_team,
                       home_odds, draw_odds, away_odds.
    """
    sync_playwright = _require_playwright()

    if tournament not in TOURNAMENT_SLUGS:
        raise ValueError(f"Unknown tournament '{tournament}'. Options: {list(TOURNAMENT_SLUGS)}")

    region, slug = TOURNAMENT_SLUGS[tournament]
    url = _RESULTS_URL.format(region=region, slug=slug)

    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()

        print(f"  Loading {url} ...")
        page.goto(url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(2000)

        # Match links: <a class="in-match" href=".../{home}-{away}/{match-slug}/">
        match_links = page.query_selector_all("a.in-match")
        print(f"  Found {len(match_links)} matches.")

        if max_matches:
            match_links = match_links[:max_matches]

        for link in match_links:
            href = link.get_attribute("href") or ""

            # Extract match URL slug from href: .../home-away/matchid/
            m = re.search(r"/([a-z0-9-]+)/([a-zA-Z0-9]+)/?$", href)
            if not m:
                continue
            teams_slug = m.group(1)    # e.g. "argentina-france"

            team_pair = _slug_to_teams(teams_slug)
            if not team_pair:
                continue
            home, away = team_pair

            # The parent <tr> contains the odds in td.table-main__odds cells
            row_el = link.evaluate_handle("el => el.closest('tr')")
            if not row_el:
                continue

            odds_cells = row_el.query_selector_all("td.table-main__odds")
            if len(odds_cells) < 3:
                continue

            try:
                h_odds = float(odds_cells[0].inner_text().strip())
                d_odds = float(odds_cells[1].inner_text().strip() or
                               odds_cells[1].query_selector("span").inner_text().strip())
                a_odds = float(odds_cells[2].inner_text().strip())
            except (ValueError, AttributeError):
                continue

            if any(o <= 1.0 for o in (h_odds, d_odds, a_odds)):
                continue

            formatted_id = f"{tournament}_{home}_vs_{away}"
            rows.append({
                "tournament": tournament,
                "match_id":   formatted_id,
                "home_team":  home,
                "away_team":  away,
                "home_odds":  h_odds,
                "draw_odds":  d_odds,
                "away_odds":  a_odds,
                "bookmaker":  "betexplorer_default",
            })

        browser.close()

    df = pd.DataFrame(rows)
    print(f"  {tournament}: {len(df)} matches with odds scraped.")
    return df


def scrape_all_tournaments(
    tournaments: list[str] | None = None,
    cache_path: Path | None = None,
    max_matches: int | None = None,
) -> pd.DataFrame:
    """
    Scrapes all configured tournaments. Skips already-cached ones.
    Saves incrementally after each tournament.
    """
    if tournaments is None:
        tournaments = list(TOURNAMENT_SLUGS.keys())
    if cache_path is None:
        cache_path = DATA_RAW / "tournament_odds.csv"

    existing = pd.DataFrame()
    if cache_path.exists():
        existing = pd.read_csv(cache_path)
        print(f"  Loaded {len(existing)} cached rows from {cache_path.name}")

    already_done = set(existing["tournament"].unique()) if not existing.empty else set()
    frames = [existing] if not existing.empty else []

    for t in tournaments:
        if t in already_done:
            print(f"  {t}: already cached, skipping.")
            continue
        df = scrape_tournament(t, max_matches=max_matches)
        if not df.empty:
            frames.append(df)
            combined = pd.concat(frames, ignore_index=True)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            combined.to_csv(cache_path, index=False)
            print(f"  Saved to {cache_path.name}")

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_odds_lookup(path: Path | None = None) -> pd.DataFrame:
    """Loads the cached tournament odds CSV as odds_lookup DataFrame."""
    if path is None:
        path = DATA_RAW / "tournament_odds.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)
