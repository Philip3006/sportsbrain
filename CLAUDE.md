# SportsBrain — Claude Code Configuration

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
└── CLAUDE.md
```

## System-Architektur (Phasen)

### Phase 1 — Datenpipeline
- Spielergebnisse (historisch): football-data.co.uk CSV, Basketball-Reference
- Live-Quoten: Odds-API oder TheOddsAPI (kostenloser Tier)
- Caching: `.cache/` Ordner, pickle-Format (analog Backtest-System)

### Phase 2 — Vorhersage-Modelle
- **Fußball:** Poisson-Regression (Tore), xG-basiert, Elo-Rating (LIVE)
- **Basketball:** Pythagorean-Erwartung, Pace-adjusted Ratings, ELO (Phase 5 — geplant zum Saisonstart: BBL ~26.09.2026, Euroleague ~02.10.2026, NBA ~21.10.2026; aktuell NICHT implementiert)
- Backtest: historische Quoten vs. Modell-Output → Expected Value (EV)

### Phase 3 — Value-Optimierung
- Value-Bet: Modell-Wahrscheinlichkeit > Implied Probability der Quote
- Kelly-Criterion: optimale Einsatzgröße (fractional Kelly = 25% empfohlen)
- Max. Einzeleinsatz: 5% des Bankrolls

### Phase 4 — Live-Scanner
- Täglicher Scan: anstehende Spiele → Vorhersagen → Value-Bets hervorheben
- Output: `results/scan_today.md` (analog `scan_live_report.md`)

## Harte Regeln

- **KEINE SCHÄTZUNGEN:** Wahrscheinlichkeiten nur aus Modell-Output mit echten Daten
- **EV > 0 PFLICHT:** Kein Wetten-Vorschlag ohne positiven Expected Value
- **KELLY-GATE:** Einsatz immer über fractional Kelly berechnen — nie "nach Gefühl"
- **BACKTEST ZUERST:** Jedes neue Modell/Feature gegen historische Daten validieren bevor Live-Einsatz
- **MAX 3 AKTIVE WETTEN:** Bankroll-Schutz

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

## ROADMAP-Mechanik (zwingend)

`ROADMAP.md` im Repo-Root ist die einzige verbindliche Roadmap-Quelle.

**Bei jeder Erwähnung von „Roadmap", „Masterplan", „in Zukunft", „Idee", „später bauen", „neues Feature" oder vergleichbaren Hinweisen auf zukünftige Arbeit:**

1. **Komplette `ROADMAP.md` lesen** — die volle Datei, jede Sektion, keine Stichproben.
2. **Neue Idee aufnehmen** — als Item mit Was/Warum/Impact/Aufwand/Risiko/Priorität/Dateien/Abhängigkeiten/Verifikation (konsistentes Format).
3. **Gesamt-Roadmap re-evaluieren** — passt das Item irgendwo besser rein? Werden andere obsolet/verschoben? Reihenfolge ändert sich?
4. **`ROADMAP.md` schreiben** (via Edit/Write) — Änderungen sichtbar markieren (`+ NEU`, `~ GEÄNDERT`, `- ENTFERNT`).
5. **Konsolidierte Übersicht ausgeben** — vollständige Roadmap inkl. aktualisierter Phasen-Reihenfolge + Statistik. Keine Verkürzung, kein „nur das Relevante".

**Synchronisations-Regel**: Mündliche Vorschläge ohne Schreibvorgang in `ROADMAP.md` gelten als nicht aufgenommen.

## Operations — manuell relevante Commands

Was die GitHub-Actions-Workflows nicht automatisieren:

```bash
# Ledger-Status (offen + abgerechnet + P&L)
python3 -c "from src.betting.ledger import ledger_summary; print(ledger_summary())"

# WM-Readiness vor kritischen Phasen prüfen
python3 scripts/wm2026_readiness_check.py

# Sperren manuell pflegen (KO-Phase ab 2026-07-04)
python3 scripts/add_suspension.py --team "..." --player "..." --reason "yellow|red"

# Squad-Cache manuell refreshen (vor wichtigen Spieltagen)
python3 scripts/refresh_squad_cache.py

# Cloudflare-Worker neu deployen (nach worker.js-Änderungen)
cd cloudflare && wrangler deploy

# Manueller Live-Scan außerhalb der Cron-Cadence
python3 scripts/daily_scan.py --bankroll 100 --force
```

## Wartungs-Hinweis Roadmap

Detaillierte 47+ Items, Phasen, Veto-Liste und Reihenfolge stehen in `ROADMAP.md`.
History (alle 87 Iterationen pre-WM) liegt in `docs/archive/improvement_log_pre_wm.md`.
