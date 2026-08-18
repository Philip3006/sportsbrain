import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_CACHE = ROOT / "data" / "cache"
MODELS_DIR = ROOT / "models"
RESULTS_DIR = ROOT / "results"

INTL_CSV_URL = (
    "https://raw.githubusercontent.com/martj42/"
    "international_results/master/results.csv"
)
FBDATA_BASE = "https://www.football-data.co.uk/mmz4281"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

# I11: Whitelist der Ligen nach WM. Nur diese Keys werden vom Football-Discovery-Modul
# zurückgegeben — schützt vor unbekannten oder unkalibrierten Soccer-Sport-Keys.
FOOTBALL_LEAGUES_WHITELIST: set[str] = {
    "soccer_germany_bundesliga",
    "soccer_epl",                         # Premier League
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_1",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_sudamericana",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_mexico_ligamx",
    "soccer_brazil_campeonato",
    "soccer_austria_bundesliga",
    "soccer_france_ligue_2",
    "soccer_germany_bundesliga2",
    "soccer_turkey_super_league",
    "soccer_belgium_first_div",
    "soccer_scotland_premier_league",
    "soccer_usa_mls",
}

# ----------------------------------------------------------------------
# LEAGUE_REGISTRY — zentrale Quelle für Liga-spezifische Config.
# Ersetzt hardcoded WM-Date-Guards in daily_scan / ledger settlement.
# Neue Ligen hier registrieren; scanner/settlement lesen den Eintrag.
#   name              : Display-Name für PWA + Push (deutsch).
#   short             : Kurz-Code für Ledger-Kolumne `league` (persistent, nie ändern).
#   sport             : "football" | "tennis" | "basketball".
#   start_date/end_date: Saison-Fenster (ISO). None = ganzjährig (Turnier-Format).
#   result_source     : Routing-Key für results_router (martj42 | football_data | tennis_scores).
#   result_source_arg : Zweit-Parameter für den Loader (D2, WC, ATP, ...).
#   fbdata_code       : football-data.co.uk-Code (nur wenn result_source=football_data).
#   min_edge          : Liga-spezifischer Edge-Floor (überschreibt globalen MIN_EDGE).
#   elo_k             : Elo-K-Faktor beim Update (Turnier 32-40, Club 20).
#   tournament_weight : Gewicht für DC-NLL-Training.
#   category_mode     : "live" | "shadow" — Live schreibt Ledger, Shadow nur Signal-Archive.
# ----------------------------------------------------------------------
LEAGUE_REGISTRY: dict[str, dict] = {
    "soccer_fifa_world_cup": {
        "name": "FIFA WM 2026",
        "short": "wm2026",
        "sport": "football",
        "start_date": "2026-06-11",
        "end_date":   "2026-07-19",
        "result_source": "martj42",
        "result_source_arg": "FIFA World Cup",
        "fbdata_code": None,
        "min_edge": 0.03,
        "elo_k": 40.0,
        "tournament_weight": 1.0,
        "category_mode": "live",
    },
    "soccer_germany_bundesliga2": {
        "name": "2. Bundesliga",
        "short": "bl2",
        "sport": "football",
        "start_date": "2026-08-01",
        "end_date":   "2027-05-31",
        "result_source": "football_data",
        "result_source_arg": "D2",
        "fbdata_code": "D2",
        "min_edge": 0.06,   # Direct-Live-Modus; alle Märkte offen; recalibrate nach 30 settled
        "elo_k": 20.0,
        "tournament_weight": 1.0,
        "category_mode": "live",
    },
}


def league_config(sport_key: str) -> dict | None:
    """Registry-Lookup mit sauberem None-Return, damit Aufrufer defensiv testen können."""
    return LEAGUE_REGISTRY.get(sport_key)


def active_leagues(today=None) -> list[str]:
    """Alle sport_keys deren Saisonfenster heute aktiv ist (start_date ≤ today ≤ end_date+1d).
    None-Bounds gelten als 'immer aktiv'. Nimmt date, datetime oder None entgegen."""
    from datetime import date as _date, datetime as _dt, timedelta as _td
    if today is None:
        t = _date.today()
    elif isinstance(today, _dt):
        t = today.date()
    elif isinstance(today, _date):
        t = today
    else:
        t = _date.today()
    out: list[str] = []
    for key, cfg in LEAGUE_REGISTRY.items():
        s = cfg.get("start_date")
        e = cfg.get("end_date")
        if s and t < _date.fromisoformat(s):
            continue
        if e and t > _date.fromisoformat(e) + _td(days=1):
            continue
        out.append(key)
    return out

DC_PHI = 0.0065
KELLY_FRAC = 0.25
MIN_EDGE = 0.03
MIN_STAKE_EUR = 5.0
MAX_STAKE_EUR = 15.0
MAX_ACTIVE_BETS = 3
# Hard risk ceiling: final recommended stake must not exceed this fraction of bankroll.
# Stake tier / Kelly = theoretical generator; this cap = final authoritative limit.
BANKROLL_RISK_CAP_PCT = 0.05
LGBM_BL2_DC_WEIGHT = 0.70   # 70% DC / 30% LGBM blend für 2.BL-Predictions
MAX_SIGNALS_DISPLAY = 5         # top N signals shown prominently per sport in PWA
MAX_EV = 0.40           # signals with EV > 40% are almost always model artifacts

# I8: Market-Performance-Feedback-Loop
MARKET_PERF_MIN_BETS = 10           # minimum settled bets before applying penalty/bonus
MARKET_PERF_ROI_PENALTY_THRESHOLD = -0.20   # ROI below -20% → penalty
MARKET_PERF_PENALTY_PP = 0.05       # +5pp to min_edge for bad markets

# N6: Bankroll-Meilenstein Push — multiples of starting bankroll
BANKROLL_MILESTONES: list[float] = [1.10, 1.20, 1.50, 2.00]

# N8: Odds-Freshness Warnung — badge wenn Odds >7% gegen Position bewegt
ODDS_MOVE_WARN_PCT: float = 0.07

# Dynamic stake tiers: each entry = (lower_bankroll_threshold, min_stake_eur, max_stake_eur).
# Lookup picks the first row whose threshold ≤ bankroll (sorted high → low).
# MAX-% of bankroll falls with growth (defensive Kelly clamp) while absolute MAX grows.
STAKE_TIERS: list[tuple[float, float, float]] = [
    (500.0, 12.0, 40.0),
    (400.0, 10.0, 35.0),
    (300.0,  8.0, 30.0),
    (200.0,  7.0, 25.0),
    (100.0,  6.0, 20.0),
    (  0.0,  5.0, 15.0),
]
# Goals-Range cap derives from the tier band: min + GOALS_RANGE_TIER_PCT × (max − min)
GOALS_RANGE_TIER_PCT = 0.2

# Stake-v2: Odds-Bucket-Cap. Stakes oberhalb dieser Quoten werden auf
# (factor × tier_hi) gedeckelt, da längere Quoten höhere Varianz haben.
# Tuple-Liste sortiert nach Quoten-Obergrenze (aufsteigend).
ODDS_BUCKET_CAPS: list[tuple[float, float]] = [
    (2.00, 1.00),
    (3.00, 0.75),
    (5.00, 0.55),
    (float("inf"), 0.35),
]

# Stake-v2: Korrelations-Adjustment je Match.
# MAX_MATCH_EXPOSURE_MULT: Σ stake aller Legs eines Matches ≤ tier_hi × diesem Faktor.
# NEG_CORR_DISCOUNT:       Underdog-Leg bei Heim/Auswärts-Konflikt × diesem Faktor.
# POS_CORR_DISCOUNT:       Beide Legs bei positiv-korrelierten Wetten × diesem Faktor.
MAX_MATCH_EXPOSURE_MULT = 1.5
NEG_CORR_DISCOUNT = 0.50
POS_CORR_DISCOUNT = 0.70

BANKROLL_SNAPSHOT_PATH = DATA_CACHE / "bankroll_snapshot.json"  # legacy single-user pfad (Migration)
BANKROLL_START = 100.0

# D3 — Multi-User-Schema (Roadmap Phase 7)
# Default-User; Frontend kann per localStorage 'sb_user' wechseln, Backend liest aktuell den Default.
DEFAULT_USER = "philip"

# P0D-002: Private ledger datastore — env var key and resolver.
LEDGER_DIR_ENV = "SPORTSBRAIN_LEDGER_DIR"


def _resolve_ledger_dir() -> Path:
    """Return the private ledger directory from SPORTSBRAIN_LEDGER_DIR env var.

    Fail-closed: if the env var is not set, raises EnvironmentError immediately.
    No mkdir, no validation — the caller must ensure the directory exists.
    In tests, set SPORTSBRAIN_LEDGER_DIR to tmp_path or use monkeypatch.setenv().
    """
    val = os.environ.get(LEDGER_DIR_ENV, "").strip()
    if not val:
        raise EnvironmentError(
            "SPORTSBRAIN_LEDGER_DIR is not set. Financial ledger operations require an explicit "
            "private ledger directory. Set to the path of the sportsbrain-ledger private "
            "repository checkout. In tests, use tmp_path or set the env var."
        )
    return Path(val)

# ------- J2-M Tennis Live-Stats --------
# Wenn True: predict_winner_ensemble ruft Tennis-Abstract-Aggregate per Match ab.
# Rule-based Adjustment ±3pp bei ausreichender Sample-Size (n≥10).
# Auf False setzen wenn TA-Verfügbarkeit sinkt oder Cache-Layer instabil wird.
TENNIS_USE_LIVE_STATS = True


def bankroll_snapshot_path_for(user: str = DEFAULT_USER) -> Path:
    """Per-User-Bankroll-Snapshot-Pfad — stored in the private ledger directory.

    Requires SPORTSBRAIN_LEDGER_DIR to be set; raises EnvironmentError otherwise.
    """
    return _resolve_ledger_dir() / f"bankroll_snapshot_{user}.json"


def ledger_path_for(user: str = DEFAULT_USER) -> Path:
    """Per-User-Ledger-Pfad — stored in the private ledger directory (Philip3006/sportsbrain-ledger).

    Requires SPORTSBRAIN_LEDGER_DIR to be set; raises EnvironmentError otherwise.
    """
    return _resolve_ledger_dir() / f"ledger_{user}.csv"


def betting_journal_path() -> Path:
    """Path to the betting journal Markdown file in the private ledger directory.

    Requires SPORTSBRAIN_LEDGER_DIR to be set; raises EnvironmentError otherwise.
    """
    return _resolve_ledger_dir() / "betting_journal.md"

# Phase 2 feature flags — keep new components off-by-default until the
# Backtest Gate (Brier vs current blend) has validated each on its own.
STACKER_ENABLED = True             # src/ensemble/stacking.py
CONFORMAL_ENABLED = True           # src/ensemble/conformal.py
HIERARCHICAL_DC_ENABLED = True     # src/models/dixon_coles.py confederation prior
PLAYER_XG_ENABLED = True           # src/features/player_rating.py — per-player shot quality

# Phase 3 new markets — guarded so a buggy detector doesn't pollute signals.json
HT_FT_ENABLED = False
CORRECT_SCORE_ENABLED = False
PLAYER_PROPS_ENABLED = False
# Tore Bereich 2-4: Backtest zeigt -9pp Kalibrierungslücke (goals_2_4_no falsche Richtung) → deaktiviert
GOALS_RANGE_ENABLED = False
GOALS_RANGE_MAX_STAKE = 7.0   # start konservativ; nach 30+ echten Wetten erhöhen
LINE_SHOPPING_REGIONS = ["eu", "us", "uk", "au"]  # best-price across all regions

# Roadmap J2 — Tennis Full-Tour-Ausbau.
# Min-Edge pro Kategorie. Slams/Top-Events haben enge Märkte → niedrige Edge reicht;
# kleinere ATP/WTA 250er haben höhere Margins → strikter Edge-Floor verhindert -ROI.
# Empirie aus Wimbledon-Backtest (WTA Grass: +8.5% ROI ab Edge 3%, ATP: -6.4% bei
# Edge 3% → 10% Floor nötig). Für Live-Schaltung pro Kategorie siehe Phase B-Gate.
TENNIS_MIN_EDGE_BY_CATEGORY: dict[str, float] = {
    "grand_slam":      0.05,   # ATP Slam: 5% floor (BO5 reduziert Varianz); WTA Slam: profitabler
    "m1000":           0.08,   # ATP Masters: enge Märkte, aber kürzeres Format
    "wta1000":         0.04,   # WTA-Märkte historisch +ROI bei niedrigerer Edge
    "atp500":          0.10,   # weniger Bookmaker-Coverage, höhere Vig
    "wta500":          0.06,
    "atp250":          0.12,   # tour-tiefe Events: konservativ, viele Lineup-Surprises
    "wta250":          0.08,
    "tour_final":      0.06,   # Top-8-only → starke Spieler, kalibrierte Märkte
    "challenger_atp":  0.12,   # Challenger: kein Elo-Coverage (shadow-only), konservativ wie ATP250
}
# Live-Gate pro Kategorie: 'live' = Bets in Ledger, 'shadow' = nur Logging.
# Wird via Phase-B-Backtest gesetzt; Default 'shadow' für Sicherheit.
TENNIS_CATEGORY_MODE: dict[str, str] = {
    # 2026-07-31 User-Override: alle Kategorien LIVE. Backtest-Gates bewusst deaktiviert;
    # BLACKLIST (Verlust-Kombis n≥30, ROI≤-5%) bleibt aktiv als harter Schutz.
    "atp250":          "live",
    "grand_slam":      "live",
    "m1000":           "live",
    "wta1000":         "live",
    "atp500":          "live",
    "wta500":          "live",
    "wta250":          "live",
    "tour_final":      "live",
    "challenger_atp":  "shadow",  # O1-8: Challenger = shadow tier — kein Elo-Coverage (Sackmann ATP main-tour only)
}

# J2-H: Surface-spezifisches Live-Gate — überschreibt TENNIS_CATEGORY_MODE wenn (category, surface) matcht.
# Backtest-Basis: Full-Tour Walk-forward 2020-2025 (tennis_full_backtest_2026-07-30.md, Sektion 1).
# Aktivierungsregel: LIVE bei ROI ≥ 3% & n ≥ 50 (oder ROI ≥ 5% & n ≥ 30). SHADOW-Override bei Verlust-Surface unter LIVE-Kategorie.
TENNIS_CATEGORY_SURFACE_MODE: dict[tuple[str, str], str] = {
    # 2026-07-31: Surface-Overrides deaktiviert (User: alles live). BLACKLIST wirkt weiterhin
    # als harter Cut für katastrophale Verlust-Kombinationen unten.
}

# BLACKLIST — Kombinationen mit ROI ≤ -5% bei n ≥ 30 aus Backtest-Sektion 1.
# Dokumentation: aktuell nicht gate-relevant (alle Einträge liegen unter SHADOW-Kategorien), aber wichtig für
# Signal-Archive-Audit und künftige Live-Gate-Reviews (scripts/tennis_gate_review.py).
TENNIS_CATEGORY_SURFACE_BLACKLIST: set[tuple[str, str, str]] = {
    ("atp500",    "ATP", "hard"),   #  -8.8% ROI, n=398
    ("grand_slam","ATP", "grass"),  #  -9.5% ROI, n=200 (Wimbledon Herren)
    ("m1000",     "ATP", "clay"),   #  -9.4% ROI, n=410
    ("tour_final","WTA", "hard"),   # -13.0% ROI, n=43
    ("wta1000",   "WTA", "hard"),   #  -5.3% ROI, n=1058
}

# I12: Umwelt-Faktoren — Höhenlage + Kunstrasen (analog Tennis Surface-Elo).
# ALTITUDE_BOOST_MAP: Heim-Team-Name → (lh_factor, la_factor).
#   lh_factor: Heim-Lambda Multiplikator (>1 = Heimvorteil stärker in der Höhe).
#   la_factor: Auswärts-Lambda Multiplikator (<1 = Auswärtsteam ermüdet, weniger Tore).
#   Kalibrierung: CONMEBOL-Hochland-Matches Bogotá/Quito/Mexico (~2500-2800m).
#   Literatur-Konsens: +10-15% Heim-Vorteil, -8% Auswärts-Tore (Pollard & Armatas 2017).
ALTITUDE_BOOST_MAP: dict[str, tuple[float, float]] = {
    # Colombia (Bogotá 2625m, Medellín 1495m)
    "Millonarios":          (1.12, 0.92),
    "Santa Fe":             (1.12, 0.92),
    "America de Cali":      (1.06, 0.95),  # Cali 1000m — geringerer Effekt
    "Atletico Nacional":    (1.08, 0.94),
    # Ecuador (Quito 2850m — höchste Liga-Stadt)
    "Liga de Quito":        (1.15, 0.90),
    "Deportivo Quito":      (1.15, 0.90),
    "El Nacional":          (1.15, 0.90),
    "Independiente del Valle": (1.10, 0.93),  # Sangolquí 2550m
    # Bolivia (La Paz 3600m — extremster Effekt weltweit)
    "Bolivar":              (1.20, 0.85),
    "The Strongest":        (1.20, 0.85),
    # Peru (Lima 154m — kein Höhenfaktor, außer Cusco/Arequipa)
    # Mexico (Mexico City 2240m)
    "Club America":         (1.07, 0.95),
    "Cruz Azul":            (1.07, 0.95),
    "Pumas UNAM":           (1.07, 0.95),
    # Internationale Auswahl-Spiele — Team-Name = Nationalteam
    "Colombia":             (0.0, 0.0),   # Placeholder, nur wenn Heim-Venue bekannt
    "Bolivia":              (1.20, 0.85),
    "Ecuador":              (1.15, 0.90),
}

# ARTIFICIAL_TURF_STADIUMS: set von Heim-Team-Namen die auf Kunstrasen spielen.
# Auswärts-Lambda wird mit TURF_AWAY_PENALTY multipliziert (Literatur: ~-4% Auswärts-Tore).
ARTIFICIAL_TURF_STADIUMS: set[str] = {
    # Skandinavische Vereine (Outdoor-Kunstrasen weit verbreitet)
    "Tromso",
    "Bodo/Glimt",
    "IK Start",
    "Rosenborg",
    "Molde",
    "HJK Helsinki",
    "FC Inter Turku",
    # Russland (viele Stadien Kunstrasen wegen Klima)
    "Krasnodar",
    "Lokomotiv Moscow",
    "Rubin Kazan",
    # Türkei
    "Trabzonspor",
    # Schweiz
    "FC Sion",
}
TURF_AWAY_PENALTY = 0.96  # Auswärts-Lambda × 0.96 auf Kunstrasen

# Phase 4 lern-loop
DRIFT_MONITOR_ENABLED = True
PINNACLE_CLV_ENABLED = True
PER_CLUSTER_CALIBRATION_ENABLED = True

# Roadmap G1 — PPDA als Live-Feature im 1X2-LGBM-Stacker.
# Aktiviert 2026-06-21 nach Sprint-2-Backtest: Brier +0.0035 ✅, Markt-aware
# ROI +11.85pp ✅ (97 Val-Matches mit echten Closing-Quoten). Sprint 1 (DC-
# Lambda-Multiplier für AH/O-U/BTTS) und Sprint 3 (Scorer-xG-Multiplier) waren
# erfolglos — diese Märkte nutzen PPDA bewusst NICHT (eigener Pfad in builder.py).
# Rückrollung: False setzen → builder droppt PPDA-Spalten → scanner-reindex
# füllt 0.0, Modell-Inferenz bleibt stabil.
PPDA_LIVE_ENABLED = True

# Roadmap I6 — Home-Advantage für WM-2026-Gastgeber USA/Canada/Mexico.
# Gastgeber spielen ihre Gruppenspiele (und ggf. KO-Matches) im eigenen Land vor
# Heim-Publikum. Scanner setzt sonst neutral=True für alle WM-Matches (DC-Training
# qualifier-lastig → home_adv verzerrt). HOST_LAMBDA_BOOST multipliziert lh nur
# für Host-Heim-Matches; la bleibt unverändert (kein Doppel-Effekt).
# Wert 1.05 = Literatur-Konsens (Pollard 2006, Clarke); empirische Kalibrierung
# auf WC 2006-2022 lieferte kein robustes Signal (n=25, 95% CI [0.50, 1.19]).
# Rückrollung: HOST_BOOST_ENABLED=False → host_boost=1.0 in daily_scan.
HOST_BOOST_ENABLED = False
HOST_LAMBDA_BOOST = 1.05
HOST_NATIONS: set[str] = {"United States", "Canada", "Mexico"}

# Confederation per team — used for asymmetric divergence threshold.
# Non-UEFA/CONMEBOL away teams have higher confederation-bias risk in DC model
# (training data dominated by qualifier blowouts). Stricter filter applied.
TEAM_CONFEDERATION: dict[str, str] = {
    # UEFA
    "Germany": "UEFA", "France": "UEFA", "Spain": "UEFA", "Portugal": "UEFA",
    "England": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA", "Italy": "UEFA",
    "Croatia": "UEFA", "Austria": "UEFA", "Switzerland": "UEFA", "Denmark": "UEFA",
    "Poland": "UEFA", "Serbia": "UEFA", "Ukraine": "UEFA", "Turkey": "UEFA",
    "Scotland": "UEFA", "Hungary": "UEFA", "Slovakia": "UEFA", "Albania": "UEFA",
    "Czech Republic": "UEFA", "Czechia": "UEFA", "Romania": "UEFA", "Slovenia": "UEFA",
    "Georgia": "UEFA",
    # CONMEBOL
    "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Ecuador": "CONMEBOL", "Venezuela": "CONMEBOL",
    "Paraguay": "CONMEBOL", "Bolivia": "CONMEBOL", "Chile": "CONMEBOL", "Peru": "CONMEBOL",
    # CONCACAF
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Panama": "CONCACAF", "Costa Rica": "CONCACAF", "Honduras": "CONCACAF",
    "Jamaica": "CONCACAF", "El Salvador": "CONCACAF", "Guatemala": "CONCACAF",
    # CAF
    "Morocco": "CAF", "Senegal": "CAF", "Nigeria": "CAF", "Egypt": "CAF",
    "Ivory Coast": "CAF", "Cote d'Ivoire": "CAF", "South Africa": "CAF",
    "Algeria": "CAF", "Tunisia": "CAF", "Ghana": "CAF", "Cameroon": "CAF",
    "Mali": "CAF", "DR Congo": "CAF",
    # AFC
    "Japan": "AFC", "South Korea": "AFC", "Saudi Arabia": "AFC", "Iran": "AFC",
    "Australia": "AFC", "Indonesia": "AFC", "New Zealand": "AFC", "Uzbekistan": "AFC",
    "Qatar": "AFC",
    # OFC
    "New Caledonia": "OFC",
    # WM 2026 qualifiers — not in original list
    "Bosnia and Herzegovina": "UEFA", "Sweden": "UEFA", "Norway": "UEFA",
    "Haiti": "CONCACAF", "Curacao": "CONCACAF",
    "Cape Verde": "CAF", "South Africa": "CAF",
    "Iraq": "AFC", "Jordan": "AFC",
}

COMPETITIVE_TOURNAMENTS = {
    "FIFA World Cup",
    "UEFA Euro",
    "Copa América",
    "Copa America",
    "African Cup of Nations",
    "AFC Asian Cup",
    "UEFA Nations League",
    "CONCACAF Nations League",
    "CONCACAF Gold Cup",
    "FIFA World Cup qualification",
    "UEFA Euro qualification",
    "Copa América qualification",
    "CONMEBOL World Cup qualification",
    "AFC Asian Cup qualification",
    "CAF World Cup qualification",
    "CONCACAF World Cup qualification",
    "OFC World Cup qualification",
    "CONCACAF Nations League qualification",
}

ELO_K_BASE = 40.0

# Multiplier on ELO_K_BASE per tournament — finals count more than qualifiers.
TOURNAMENT_K_FACTORS: dict[str, float] = {
    "FIFA World Cup":                        1.30,
    "UEFA Euro":                             1.20,
    "Copa América":                          1.15,
    "Copa America":                          1.15,
    "African Cup of Nations":                1.00,
    "AFC Asian Cup":                         0.95,
    "CONCACAF Gold Cup":                     0.90,
    "UEFA Nations League":                   1.00,
    "CONCACAF Nations League":               0.80,
    "FIFA World Cup qualification":          0.50,
    "UEFA Euro qualification":               0.55,
    "Copa América qualification":            0.50,
    "CONMEBOL World Cup qualification":      0.50,
    "AFC Asian Cup qualification":           0.45,
    "CAF World Cup qualification":           0.40,
    "CONCACAF World Cup qualification":      0.45,
    "OFC World Cup qualification":           0.30,
    "CONCACAF Nations League qualification": 0.45,
}

# WM 2026 freshness boost — current-tournament matches carry more signal about
# present team form than historical finals. Applied multiplicatively on top of
# TOURNAMENT_WEIGHTS for matches with date >= WC2026_START and tournament="FIFA World Cup".
WC2026_START = "2026-06-11"
WC2026_BOOST = 1.0  # Neutralized post-WM. Phase-Metadata + Sunset in src/phase_flags.py.

# Elo-adjusted DC: scale factor for opponent quality adjustment.
# exp(elo_diff / DC_ELO_SCALE) is multiplied into the home team's Poisson mean.
# 600 ≈ "400 Elo gap doubles expected goal rate" (standard Elo win-prob scale × ln(2)).
DC_ELO_SCALE: float = 600.0  # inference-only: calibrated so a 278-pt Elo gap → ~1.59x on goal rates


# Tournament quality weights for DC NLL — final tournaments > group/NL > qualifiers.
# Qualifiers against weak opponents (e.g. OFC, CAF) inflate attack params without
# reflecting true ability at WC/EURO level. Down-weighting them reduces confederation bias.
TOURNAMENT_WEIGHTS: dict[str, float] = {
    # Finals: full weight — real head-to-head at balanced competition level
    "FIFA World Cup": 1.0,
    "UEFA Euro": 1.0,
    "Copa América": 1.0,
    "Copa America": 1.0,
    "African Cup of Nations": 0.90,
    "AFC Asian Cup": 0.85,
    "CONCACAF Gold Cup": 0.80,
    # Nations Leagues: strong competitive signal, peer-group matches
    "UEFA Nations League": 0.90,
    "CONCACAF Nations League": 0.75,
    # Qualifiers: heavily down-weighted — blowout wins vs minnows inflate attack params.
    # Teams like Japan (14-0 vs Bangladesh) and NZ (12-0 vs Tonga) get reduced influence.
    "FIFA World Cup qualification": 0.30,
    "UEFA Euro qualification": 0.35,
    "Copa América qualification": 0.30,
    "CONMEBOL World Cup qualification": 0.30,
    "AFC Asian Cup qualification": 0.25,
    "CAF World Cup qualification": 0.25,
    "African Cup of Nations qualification": 0.25,
    "CONCACAF World Cup qualification": 0.28,
    "OFC World Cup qualification": 0.20,
    "CONCACAF Nations League qualification": 0.28,
}

# Canonical name map: raw name -> canonical name
# Covers mismatches between martj42 and football-data.co.uk sources
TEAM_NAME_MAP: dict[str, str] = {
    "USA": "United States",
    "US": "United States",
    "Ivory Coast": "Cote d'Ivoire",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "IR Iran": "Iran",
    "Kyrgyz Republic": "Kyrgyzstan",
    "Cabo Verde": "Cape Verde",
    "St. Kitts and Nevis": "Saint Kitts and Nevis",
    "St. Lucia": "Saint Lucia",
    "St. Vincent / Grenadines": "Saint Vincent and the Grenadines",
    "Trinidad & Tobago": "Trinidad and Tobago",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Czech Republic": "Czechia",
    "Republic of Ireland": "Ireland",
    "Northern Ireland": "Northern Ireland",
    "FYR Macedonia": "North Macedonia",
    "Macedonia": "North Macedonia",
    "Slovak Republic": "Slovakia",
    # TheOddsAPI-specific aliases
    "DR Congo": "DR Congo",  # DC uses "DR Congo" directly — no change needed
    "Cote d'Ivoire": "Cote d'Ivoire",
    # Accented/variant spellings from TheOddsAPI for WM 2026 qualifiers
    "Curaçao": "Curacao",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Côte d'Ivoire": "Cote d'Ivoire",
    # ── 2. Bundesliga — ESPN / TheOddsAPI / football-data.co.uk Abweichungen ──
    # Hamburg / HSV
    "Hamburger SV": "Hamburg",
    "HSV": "Hamburg",
    "Hamburger SV (Reserves)": "Hamburg",
    # Karlsruher SC
    "Karlsruher SC": "Karlsruhe",
    "KSC": "Karlsruhe",
    # Greuther Fürth
    "SpVgg Greuther Fürth": "Greuther Furth",
    "SpVgg Greuther Furth": "Greuther Furth",
    "Greuther Fürth": "Greuther Furth",
    "Fürth": "Greuther Furth",
    "Fuerth": "Greuther Furth",
    # Fortuna Düsseldorf
    "Fortuna Düsseldorf": "Fortuna Dusseldorf",
    "Düsseldorf": "Fortuna Dusseldorf",
    # 1. FC Nürnberg
    "1. FC Nürnberg": "Nurnberg",
    "Nürnberg": "Nurnberg",
    "1 FC Nurnberg": "Nurnberg",
    # FC St. Pauli
    "FC St. Pauli": "St Pauli",
    "St. Pauli": "St Pauli",
    # SV Darmstadt 98
    "Darmstadt 98": "Darmstadt",
    "SV Darmstadt 98": "Darmstadt",
    # Preußen Münster
    "Preußen Münster": "Preußen Münster",  # keep canonical accented form
    "Preussen Munster": "Preußen Münster",
    "PreuÃŸen MÃ¼nster": "Preußen Münster",  # cp1252 mojibake guard
    # SV Elversberg
    "SV Elversberg": "Elversberg",
    "1. FC Saarbrücken": "Saarbrucken",
    "Saarbrücken": "Saarbrucken",
    # Holstein Kiel
    "Holstein Kiel": "Kiel",
    # Eintracht Braunschweig
    "Eintracht Braunschweig": "Braunschweig",
    # Hannover 96
    "Hannover 96": "Hannover",
    # FC Magdeburg
    "1. FC Magdeburg": "Magdeburg",
    # SC Paderborn
    "SC Paderborn 07": "Paderborn",
    "Paderborn 07": "Paderborn",
    # Arminia Bielefeld
    "Arminia Bielefeld": "Bielefeld",
    "DSC Arminia Bielefeld": "Bielefeld",
    # VfL Bochum
    "VfL Bochum": "Bochum",
    # FC Schalke 04
    "FC Schalke 04": "Schalke 04",
    "Schalke": "Schalke 04",
    # Hertha BSC
    "Hertha BSC": "Hertha",
    "Hertha Berlin": "Hertha",
    # Kaiserslautern
    "1. FC Kaiserslautern": "Kaiserslautern",
    "FCK": "Kaiserslautern",
    # FC Erzgebirge Aue
    "Erzgebirge Aue": "Erzgebirge Aue",
    "FC Erzgebirge Aue": "Erzgebirge Aue",
    # 1. FC Köln (potenzieller Absteiger / 2.BL-Teilnehmer)
    "1. FC Köln": "FC Koln",
    "1. FC Koeln": "FC Koln",
    "Koln": "FC Koln",
    "Köln": "FC Koln",
    "FC Köln": "FC Koln",
    # SV Sandhausen
    "SV Sandhausen": "Sandhausen",
    "SVS": "Sandhausen",
    # VfB Stuttgart (potenzieller Absteiger)
    "VfB Stuttgart": "Stuttgart",
    # Dynamo Dresden
    "Dynamo Dresden": "Dresden",
    "SG Dynamo Dresden": "Dresden",
    # 2. Bundesliga 2026/27 — Neueinsteiger
    "VfL Osnabrück": "Osnabruck",
    "VfL Osnabruck": "Osnabruck",
    "Osnabrück": "Osnabruck",
    "Osnabruck": "Osnabruck",
    "FC Energie Cottbus": "Cottbus",
    "Energie Cottbus": "Cottbus",
    "VfL Wolfsburg": "Wolfsburg",
    "Wolfsburg": "Wolfsburg",
    "1. FC Heidenheim": "Heidenheim",
    "FC Heidenheim": "Heidenheim",
    "Heidenheim 1846": "Heidenheim",
    "1. FC Heidenheim 1846": "Heidenheim",
    "TSV Eintracht Braunschweig": "Braunschweig",
}


# WM 2026 Group assignments (12 groups, 4 teams each)
# Verified against official FIFA draw held 2025-12-05, Washington D.C.
# Source: https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_draw
WM2026_GROUPS: dict[str, str] = {
    # Group A
    "Mexico": "A", "South Africa": "A", "South Korea": "A", "Czechia": "A",
    # Group B
    "Canada": "B", "Bosnia and Herzegovina": "B", "Qatar": "B", "Switzerland": "B",
    # Group C
    "Brazil": "C", "Morocco": "C", "Haiti": "C", "Scotland": "C",
    # Group D
    "United States": "D", "Paraguay": "D", "Australia": "D", "Turkey": "D",
    # Group E
    "Germany": "E", "Curacao": "E", "Cote d'Ivoire": "E", "Ecuador": "E",
    # Group F
    "Netherlands": "F", "Japan": "F", "Sweden": "F", "Tunisia": "F",
    # Group G
    "Belgium": "G", "Egypt": "G", "Iran": "G", "New Zealand": "G",
    # Group H
    "Spain": "H", "Cape Verde": "H", "Saudi Arabia": "H", "Uruguay": "H",
    # Group I
    "France": "I", "Senegal": "I", "Iraq": "I", "Norway": "I",
    # Group J
    "Argentina": "J", "Algeria": "J", "Austria": "J", "Jordan": "J",
    # Group K
    "Portugal": "K", "DR Congo": "K", "Uzbekistan": "K", "Colombia": "K",
    # Group L
    "England": "L", "Croatia": "L", "Ghana": "L", "Panama": "L",
}


_TEAM_NAME_MAP_LOWER: dict[str, str] = {k.lower(): v for k, v in TEAM_NAME_MAP.items()}


def canonical_name(name: str) -> str:
    return TEAM_NAME_MAP.get(name) or _TEAM_NAME_MAP_LOWER.get(name.lower(), name)
