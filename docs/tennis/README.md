# Tennis-Modul — Ops & Kalibrierung

Kurzreferenz für Setup, Weekly-Ops und Kalibrierungs-Konventionen des Tennis-Stacks.

## Setup (einmalig)

Environment (in `.env`):

```
ODDS_API_KEY=...            # TheOddsAPI (Primär-Odds + Live-Scores)
BETFAIR_APP_KEY=...         # Optional — Betfair Exchange als Tier-1
BETFAIR_USERNAME=...
BETFAIR_PASSWORD=...
```

Ohne Betfair-Credentials fällt der Merger transparent auf Tier-2 (Tennis-Explorer + OddsPortal + WebSearch) zurück.

## Weekly-Ops

| Aufgabe | Command | Wann |
|---|---|---|
| Elo/LGBM/Serve-Stats-Verify | `python3 scripts/tennis_stats_verify.py` | vor Grand Slam |
| Coverage-Report | `python3 scripts/tennis_coverage_report.py` | Mo morgens |
| Manueller Scan | `python3 scripts/tennis_scan.py --bankroll 100` | ad-hoc |
| Retrain (nach neuen Daten) | `python3 scripts/tennis_retrain.py` | quartalsweise |
| Backtest Category+Surface | `python3 scripts/tennis_full_backtest.py --j2-report` | halbjährlich |
| Gate-Review (Live-vs-Backtest) | `python3 scripts/tennis_gate_review.py` | monatlich |

Automatisierte Läufe (GitHub Actions):
- `tennis_scan.yml` — 4×/Tag (06/11/16/21 UTC), Live-Scan + Signal-Archive
- `tennis_settle.yml` — 2h-Cadence, Settlement offener Bets
- `tennis_retrain.yml` — automatischer Retrain nach Threshold

## Kalibrierungs-Konventionen

### Elo (K-Factor)
| Level | K |
|---|---|
| Grand Slam (`g` / `grand_slam`) | 40 |
| Masters (`m` / `m1000` / `wta1000`) | 32 |
| ATP/WTA 500 (`a` / `atp500` / `wta500`) | 24 |
| Tour Finals (`f`) | 20 |
| Davis Cup (`d`) | 16 |
| Challenger (`c`) | 16 |
| Default / ATP 250 / WTA 250 | 16 |

Mapping in `src/models/tennis_elo.py` (`_K_BY_LEVEL` + `_CATEGORY_TO_LEVEL`). Recency-Decay 10 %/Jahr; K skaliert mit Halbwertszeit 3 Jahre.

### Blend-Weights
- Ensemble: **55 % Elo · 45 % LGBM** (`_LGBM_WEIGHT` in `ensemble.py`)
- Surface-Blend im Elo: **Grass 60 %**, **Clay 70 %**, **Hard 70 %** (`_SURFACE_WEIGHTS`)

### Rule-based Adjustment (J2-M)
- `dominance_rate_diff × 0.30`, gecappt auf **±3 pp**
- Aktiv nur bei **n_matches ≥ 10** für beide Spieler

### Gate-Kriterien (Backtest → Live)
- LIVE: ROI ≥ 3 % bei n ≥ 50 **oder** ROI ≥ 5 % bei n ≥ 30
- BLACKLIST: ROI ≤ −5 %
- Alles dazwischen: SHADOW

### Sanity-Gates auf Odds
- Overround 0.95–1.15
- Odds-Bounds 1.01–50.0
- EV-Cap 40 % (MAX_EV — filtert Model-Artefakte)
- Min-Edge 5 % (`_DEFAULT_MIN_EDGE`)

## Odds-Chain (Multi-Source)

Merger-Priorität nach `source_tier`:

| Tier | Provider | Coverage | AH-Support |
|---|---|---|---|
| 1 | Betfair Exchange | Sharp, Traded | H2H |
| 1 | Pinnacle (J8-I6 geplant) | Sharp | H2H + AH |
| 2 | TheOddsAPI | Retail EU/US/UK/AU | H2H + AH |
| 2 | Tennis-Explorer | breite Coverage inkl. Challenger | H2H |
| 2 | OddsPortal | ~30 Bookies | H2H |
| 4 | WebSearch-Ensemble | last-resort | H2H |
| 5 | Implied (Modell) | Display-only, `no_bet_flag=True` | H2H |

## Namens-Formate

Drei Formate im Stack (kanonisch: **Elo-Format** `"Shelton B."`):
- TheOddsAPI: `"Ben Shelton"` (Vorname Nachname)
- Tennis-Explorer: `"Shelton Ben"` (Nachname Vorname)
- Elo-Storage: `"Shelton B."` (Nachname Initial.)

Normalisierung in `src/tennis/name_norm.py`. Score-Fetcher schreibt Match-Keys zusätzlich in `canonical_match_key(a, b)`-Norm (J8-B3) — verhindert Whitespace/Unicode-Drift beim Settle.

## Bekannte Grenzen

- Sackmann-Repos (öffentlich) seit Juni 2026 nicht mehr abrufbar. Elo aktuell aus `tennis-data.co.uk` XLSX (walk-forward reconstructed).
- Serve-Stats-Snapshots (Tennis-Abstract matchmx) sind cumulative, kein Historie-Point-in-Time. Voller LGBM-Retrain mit Serve-Features (J2-N) braucht 6+ Monate Snapshot-Vorlauf.
- AH/Spreads aktuell nur bei TheOddsAPI robust. Fallback-Provider (TE/OP/Betfair) liefern nur H2H — bei API-Ausfall verschwinden AH-Signale (J8-M5 offen).

## Referenzen

- ROADMAP-Sektion **J2** (Tennis Full-Tour-Ausbau) + **J8** (Audit 2026-08-01)
- Audit-Report: `~/.claude/plans/vivid-knitting-pearl.md`
- Memory: `~/.claude/projects/-Users-philiprassillier-sportsbrain/memory/tennis_module.md`
