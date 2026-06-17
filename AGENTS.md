# SportsBrain — Codex Configuration

## Projektziel
Tool zur Vorhersage von Fußball- und Basketball-Ergebnissen + Sportwetten-Value-Optimierung.
Architektur analog zum Ruflo-Trading-System: Datensammlung → Backtest → Live-Signal → Einsatzoptimierung.

## Modus & Skill Auto-Selektion

**Modus:**
| Lean | Deep |
|------|------|
| Einzelne Vorhersage, Quoten-Check, Score-Lookup | Neues Modell, Backtest, Multi-Liga, Feature-Engineering |
| Status-Check, API-Test | Architektur-Entscheidung, Performance-Tuning |

## Projektstruktur

```
sportsbrain/
├── src/           # Kernlogik (Daten, Modelle, Scanner)
├── data/          # Rohdaten, gecachte API-Antworten
├── models/        # Trainierte Modelle (.pkl, .json)
├── scripts/       # One-off Scripts, Backtests
├── results/       # Reports, Predictions, Logs
└── AGENTS.md
```

## System-Architektur (Phasen)

### Phase 1 — Datenpipeline
- Spielergebnisse (historisch): football-data.co.uk CSV, Basketball-Reference
- Live-Quoten: Odds-API oder TheOddsAPI (kostenloser Tier)
- Caching: `.cache/` Ordner, pickle-Format (analog Backtest-System)

### Phase 2 — Vorhersage-Modelle
- **Fußball:** Poisson-Regression (Tore), xG-basiert, Elo-Rating
- **Basketball:** Pythagorean-Erwartung, Pace-adjusted Ratings, ELO
- Backtest: historische Quoten vs. Modell-Output → Expected Value (EV)

### Phase 3 — Value-Optimierung
- Value-Bet: Modell-Wahrscheinlichkeit > Implied Probability der Quote
- Kelly-Criterion: optimale Einsatzgröße (fractional Kelly = 25% empfohlen)
- Max. Einzeleinsatz: dynamischer Bankroll-Tier-Cap aus `STAKE_TIERS`; kein fixer 5%-Cap

### Phase 4 — Live-Scanner
- Täglicher Scan: anstehende Spiele → Vorhersagen → Value-Bets hervorheben
- Output: `results/scan_today.md` (analog `scan_live_report.md`)

## Harte Regeln

- **KEINE SCHÄTZUNGEN:** Wahrscheinlichkeiten nur aus Modell-Output mit echten Daten
- **EV > 0 PFLICHT:** Kein Wetten-Vorschlag ohne positiven Expected Value
- **KELLY-GATE:** Einsatz immer über fractional Kelly berechnen und über den dynamischen Bankroll-Tier-Cap begrenzen — nie "nach Gefühl"
- **BACKTEST ZUERST:** Jedes neue Modell/Feature gegen historische Daten validieren bevor Live-Einsatz
- **MAX 5 AKTIVE WETTEN:** Bankroll-Schutz

## Skill Auto-Aktivierung

| Aufgabe | Skill |
|---------|-------|
| Modell-Architektur | `backtest-expert` |
| Mehrere Szenarien | `scenario-analyzer` |
| Risiko/Einsatz | `position-sizer` |
| Marktkontext | `market-environment-analysis` |

## Rules

- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary — prefer editing existing files
- ALWAYS read a file before editing it
- NEVER commit secrets, credentials, or API keys
- Keep files under 500 lines
- Validate input at system boundaries (API responses, user input)
