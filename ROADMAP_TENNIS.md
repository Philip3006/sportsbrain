# SportsBrain — TENNIS ROADMAP

> **Verbindliche Tennis-Roadmap** zur vollständigen Produktionsparität für ATP + WTA über alle Turniere, Kategorien und Märkte.
> Aktualisiert: 2026-07-30
> Priorität: **P0 — Top-Fokus bis 1. Aug 2026 (Toronto/Montreal-Start)**

---

## 🎯 Ziel

Nach Umsetzung dieser Roadmap kann SportsBrain für **jeden ATP/WTA-Match aller 49 registrierten Turniere** in allen 6 Märkten:
- Value-Bets identifizieren (Elo + LGBM-Stacker)
- automatisch platzieren (nach PWA-Bestätigung)
- auto-settlen (inkl. Retirement/Walkover)
- CLV tracken (Closing-Odds-Capture)
- pushen (VAPID)
- im Signal-Archive für Nachanalyse festhalten
- im Dashboard darstellen (dedizierte Tennis-Sektion)

**Turnier-Nummer 1 live:** National Bank Open (Toronto ♀ / Montreal ♂) am **1. August 2026**.

---

## 🔄 Wartungs-Mechanik

Diese Datei folgt denselben Regeln wie `ROADMAP.md`:
1. Bei jeder Tennis-Idee: **komplette `ROADMAP_TENNIS.md` lesen** — jede Sektion.
2. Neues Item mit Was/Warum/Impact/Aufwand/Risiko/Priorität/Dateien/Abhängigkeiten/Verifikation aufnehmen.
3. Gesamt-Roadmap re-evaluieren, Reihenfolge/Abhängigkeiten prüfen.
4. Konsolidierte Übersicht ausgeben (`+ NEU`, `~ GEÄNDERT`, `- ENTFERNT`).
5. Synchronisations-Regel: Nur was in dieser Datei steht, gilt.

Diese Roadmap ergänzt die zentrale `ROADMAP.md` (Football-Fokus). Für Tennis-spezifische Arbeit ist **diese Datei die primäre Quelle**.

---

## Bewertungsschlüssel

- **Impact**: 🟢 hoch · 🟡 mittel · ⚪ niedrig
- **Aufwand**: 🟢 niedrig (<1h) · 🟡 mittel (1-4h) · 🔴 hoch (4h+)
- **Risiko**: 🟢 niedrig · 🟡 mittel · 🔴 hoch
- **Priorität**: P0 (sofort) · P1 (diese Woche) · P2 (dieser Monat) · P3 (später)

---

## 📊 Phasenübersicht

| Phase | Zeitraum | Ziel-Turnier | Kern-Deliverables | Status |
|---|---|---|---|---|
| **P0** | 2026-07-30 (heute, ~2h) | — | Football pausieren, Baseline, Test-Suite grün, diese Datei | 🔄 in Arbeit |
| **P1** | 2026-07-31 → 2026-08-01 (36h) | **Toronto/Montreal** (1.–13.08.) | Auto-Settlement, Closing-Odds-Capture, Health-Integration, Push, Dashboard-Sektion, Elo-Update-Cron, Backtest-Rerun, All-Categories-Live-Gate | 🕒 geplant |
| **P2** | 2026-08-02 → 2026-08-13 (11 Tage) | **Cincinnati** (13.–23.08.) | LGBM-Stacker, H2H + Form Features, Live-Gate-Re-Review, Correlation-Guard | 🕒 geplant |
| **P3** | 2026-08-14 → 2026-08-30 (17 Tage) | **US Open** (30.08.–13.09.) | Serve/Return-Stats, PWA-Tennis-Tab, Retirement-Handling, Rank-Adjust, Match-Reminder | 🕒 geplant |
| **P4** | 2026-09-14 → 2026-09-30 (2 Wochen) | — | Post-Slam-Backtest-Review, Gate-Refit, Basketball-Kickoff parallel | 🕒 geplant |

---

## 🧠 Was ist schon fertig (Baseline J2-A bis J2-J)

Das Tennis-Modul ist deutlich weiter als der Rest dieser Roadmap suggeriert — die folgenden Bausteine stehen und werden **wiederverwendet**:

- ✅ **J2-A** Tournament-Registry (49 Turniere, 8 Kategorien) — `src/tennis/tournaments.py`
- ✅ **J2-B** Backtest-Gate (per-category ROI-Verdicts) — `scripts/tennis_gate_review.py`
- ✅ **J2-C** Market-Expansion (Set/Game/Set-Betting-Märkte) — `src/tennis/sim.py`, `src/betting/tennis_detector.py`
- ✅ **J2-D** Scanner-Dispatcher (multi-tournament) — `scripts/tennis_scan.py`
- ✅ **J2-E** PWA Tournament-Meta — `src/notifications/web_dashboard.py`
- ✅ **J2-F** CI/Cron ganzjährig 4×/Tag — `.github/workflows/tennis_scan.yml`
- ✅ **J2-G** Comprehensive Backtest (32.707 Match-Zeilen 2019-2025) — `scripts/tennis_full_backtest.py`
- ✅ **J2-H** Surface-aware Live-Gate (4 profitable Paare) — `src/config.py:TENNIS_CATEGORY_SURFACE_MODE`
- ✅ **J2-I** Elo + WebSearch Fallback-Stack — `src/tennis/elo_source.py`, `scripts/tennis_scan._websearch_tennis_fallback`
- ✅ **J2-J** Signal-Archive I9-Integration (Tennis-Signale in `signal_history.jsonl`)

**Test-Coverage:** 143 tests / all passing.

---

## 🧬 WM-Learnings, die auf Tennis übertragen werden

| Learning aus WM 2026 | Anwendung auf Tennis |
|---|---|
| Signal-Archive I9 rettet die Nachanalyse | Tennis-Signale bereits in `signal_history.jsonl`, aber `settle_market()` deckt Tennis-Märkte NICHT ab → `settle_tennis_market()` bauen |
| Silent Empty Writes zerstören PWA (Incident 2026-06-26) | Dashboard-Guards analog Football; Tennis-Sektion darf nie Empty-Write triggern |
| Auto-Log ohne Bestätigung ist verboten | Detector schickt Signale, Ledger-Eintrag NUR über PWA-Confirm oder explizites CLI-Flag |
| CLV +11% bei ROI -1,6% zeigt: Sample zu klein, System OK | Live-Gate-Review erst nach ≥30-50 settled Bets pro Kat/Surface — nicht nach 5 Verlusten anpassen |
| Odds-WebSearch-Fallback bei <3 Bookies | Beibehalten, Overround-Check 0,95-1,15 |
| Health-Aggregation verhindert stille Ausfälle | Tennis-Jobs (scan, settle, closing_odds, retrain) alle in `JOB_SCHEDULE` |
| DC-Elo-Architektur: Live-Elo essentiell, Training-Elo NICHT | Für Tennis: Walk-forward-Elo mit Reference-Date, keine In-Training-Elo-Features |
| Push mit VAPID-Key-Fix (trailing `"` + Extra-Zeile) | Selbe Infrastruktur, sport-agnostisch |
| Merge-Konflikte in Auto-Files → `--theirs` strategy | Selber Handler |
| Backtest-First-Roll-out: LIVE nur nach ROI ≥ 3% bei n ≥ 50 | Bereits im Gate implementiert (J2-B), bleibt Gesetz |
| Kelly-Cap €5-40 je Bankroll-Tier | Bereits shared code, kein Zusatzaufwand |
| Signal-Archive muss von Tag 1 dabei sein | Für alle 6 Tennis-Märkte von Anfang an aktiv |

---

## 🟦 P0 — Vorbereitung (heute, ~2h)

**Ziel:** Sauberer Start, Football-Ressourcen freigeben.

### P0.1 — Repo-State absichern
- **Was**: `git status` prüfen, Auto-Files committen, Test-Suite lokal grün fahren, Feature-Branch `tennis/full-coverage` anlegen
- **Warum**: Kein Rollback-Risiko während Umbau
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Verifikation**: `pytest tests/tennis/` → 143 tests pass; `git status` → clean

### P0.2 — Football-Workflows deaktivieren
- **Was**: 7 Workflows umbenennen zu `.disabled` oder Cron auskommentieren:
  - `daily_scan.yml`, `settle.yml`, `closing_odds.yml`, `auto_retrain.yml`, `scrape_suspensions.yml`, `live_score_push.yml`, `prematch_scan.yml`
- **Warum**: Spart API-Quota + CI-Minuten; WM ist beendet, Vereinssaisons starten frühestens 15.8.
- **Behalten**: `cloud_healer.yml`, `consume_pending_bets.yml`, `tennis_scan.yml`, `weekly_recap.yml`
- **Zusätzlich**: `WC2026_BOOST` in `src/config.py` auf `1.0` setzen
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `.github/workflows/*.yml`, `src/config.py`
- **Verifikation**: `gh workflow list --state active` → nur die 4 gewählten aktiv

### P0.3 — Bankroll-Snapshot dokumentieren
- **Was**: Aktueller Tennis-Bankroll in `data/bankroll_snapshot_philip.json` festhalten
- **Warum**: Definierter Startpunkt für Tennis-Kampagne
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢

### P0.4 — Diese Datei anlegen
- **Was**: `ROADMAP_TENNIS.md` (dieses File) im Root
- **Status**: ✅ erledigt beim Schreiben dieser Zeile

---

## 🟩 P1 — Production-Ready für Toronto/Montreal (36h)

**Ziel:** Bis 1. Aug 12:00 UTC ist Tennis so produktionsreif wie Football zur WM.

### P1.1 — Tennis-Settlement (`settle_tennis_market()`)
- **Was**: Neues Modul `src/betting/tennis_settlement.py` mit `settle_tennis_market(bet, match_result) -> "won"|"lost"|"push"|"pending"`. Deckt alle 6 Märkte + Retirement/Walkover ab.
- **Warum**: Ohne Auto-Settlement kein CLV, kein Signal-Archive-Outcome, kein Push. Football's `settle_market()` deckt Tennis nicht ab.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡
- **Dateien**: `src/betting/tennis_settlement.py` (neu, ~200 LOC), `scripts/tennis_settle.py` (neu), `.github/workflows/tennis_settle.yml` (Cron `15 */2 * * *`), `tests/tennis/test_settlement.py` (~25 Tests)
- **Score-Fetching**: TheOddsAPI `/scores` → ESPN Tennis API → WebSearch
- **Priorität**: P0 (blockiert alles)

### P1.2 — Closing-Odds-Capture für Tennis
- **Was**: `scripts/update_tennis_closing_odds.py` + `.github/workflows/tennis_closing_odds.yml` (Cron alle 30 Min)
- **Warum**: Ohne Closing-Odds kein CLV → keine Modell-Kalibrierung
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: siehe oben, `tests/tennis/test_closing_odds.py` (~10 Tests)
- **Wiederverwendung**: `scripts/update_closing_odds.py` (Football) als Blueprint
- **Priorität**: P0

### P1.3 — Health-Integration
- **Was**: 4 Tennis-Jobs in `src/monitoring/health_writer.py:JOB_SCHEDULE` eintragen; Health-Writer-Aufrufe in allen Tennis-Workflows
- **Warum**: Ausfälle sonst unentdeckt (Cloud-Healer kann nicht triggern)
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Dateien**: `src/monitoring/health_writer.py`, alle 4 Tennis-Workflows
- **Verifikation**: `results/health/tennis_*.json` existieren nach erstem Run
- **Priorität**: P0

### P1.4 — Post-Match-Push (VAPID)
- **Was**: `scripts/tennis_post_match_push.py`, wird nach jedem `tennis_settle`-Run getriggert
- **Warum**: Feedback-Loop Ergebnis → Nutzer
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Wiederverwendung**: `src/notifications/web_push.py:send_settlement_alert()` bereits sport-agnostisch
- **Dateien**: siehe oben, `tests/tennis/test_post_match_push.py` (~8 Tests)
- **Priorität**: P1

### P1.5 — Dashboard `tennis`-Sektion in `signals.json`
- **Was**: Neue Funktion `_build_tennis_stats(ledger_df) -> dict` in `src/notifications/web_dashboard.py`. Neue Top-Level-Sektion `tennis` mit `active_tournaments`, `stats` (per_category/surface/market/tour), `live_gate_status`, `elo_top10_hard/clay/grass`
- **Warum**: Football hat dedizierte Sektion — Tennis braucht Symmetrie für Dashboard
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Guard**: Empty-Write-Guard wie bei Football (Incident 2026-06-26)
- **PWA**: In P1 nur minimaler Preview — vollständiger Tab in P3
- **Dateien**: `src/notifications/web_dashboard.py`, `tests/tennis/test_dashboard_tennis_stats.py` (~12 Tests)
- **Priorität**: P1

### P1.6 — Elo-Update-Cron (wöchentlicher XLSX-Refresh)
- **Was**: `.github/workflows/tennis_elo_refresh.yml` (Cron `0 3 * * 1`, Montags 03:00 UTC); `scripts/tennis_retrain.py` (Elo-Rebuild + Snapshot-Save nach `models/tennis/elo_snapshot.pkl`)
- **Warum**: `tennis-data.co.uk` wird wöchentlich aktualisiert; Sackmann seit Juni offline
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: siehe oben, `models/tennis/` neuer Ordner, `tests/tennis/test_retrain.py` (~6 Tests)
- **Priorität**: P0

### P1.7 — Backtest-Rerun mit aktuellen Daten
- **Was**: `python3 scripts/tennis_full_backtest.py --full-tour --years 2020-2025 --use-category-edge --j2-report`; danach `scripts/tennis_gate_review.py` für PROMOTE/DEMOTE-Empfehlungen
- **Warum**: Gates verifizieren, evtl. neue LIVE-Kombinationen freigeschaltet
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Output**: `results/audits/tennis_full_backtest_2026-07-31.md`
- **Priorität**: P0

### P1.8 — All-Categories go-live (mit Backtest-basiertem Gate)
- **Was**: `src/config.py:TENNIS_CATEGORY_MODE` + `TENNIS_CATEGORY_SURFACE_MODE` gemäß P1.7-Empfehlungen aktualisieren
- **Warum**: „Volle Abdeckung" = Scanner läuft überall, Ledger-Eintrag folgt Backtest-Verdict (Risikoschutz)
- **Erwartung**: LIVE für ~6-8 Kombinationen, BLACKLIST für ~5, SHADOW für Rest (schreiben Signal-Archive aber kein Ledger)
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟡
- **Priorität**: P0

### P1.9 — E2E Smoke-Test (31.07. Abends, Go/No-Go)
- **Was**: 5 manuelle Checks:
  1. `python3 scripts/tennis_scan.py --mock`
  2. `python3 scripts/tennis_scan.py --bankroll 100` (echt, Toronto-Signale?)
  3. `python3 scripts/tennis_settle.py --dry-run`
  4. `curl $SIGNALS_CLOUD_URL/health` → alle tennis_*-Jobs `ok`
  5. PWA öffnen, `tennis`-Sektion sichtbar
- **Kriterium**: Alle 5 grün → 1.8. 12:00 UTC live

---

## 🟨 P2 — Model Depth bis Cincinnati (11 Tage)

**Ziel:** ML-Stacker analog zum Football-DC+LGBM, mehr Features, Live-Gate-Re-Review nach 2 Wochen Live-Daten.

### P2.1 — Tennis-LightGBM-Stacker (J2-K)
- **Was**: `src/models/tennis_lgbm.py` — Walk-forward-Training auf 2020-2025 XLSX, Isotonic-Kalibrator, Persistierung nach `models/tennis/lgbm.pkl` + `lgbm_calibrator.pkl` + `lgbm_features.json`
- **Features**: Elo-Diff, Elo-Age, Form (Last-10), H2H, Ranking-Diff, Age-Diff, Surface-Career-Winrate, BO3/BO5, Round, Days-Since-Last-Match, Serve-% (Placeholder für P3)
- **Meta-Blend**: Elo-Only 60% + LGBM 40% initial (adaptiv per Retrain)
- **Wiederverwendung**: `src/models/lgbm.py` (Football-Stacker) als Referenz-Architektur
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡
- **Dateien**: siehe oben, `tests/tennis/test_lgbm.py` (~20 Tests)
- **Priorität**: P1

### P2.2 — H2H + Form Features
- **Was**: `src/tennis/features.py` mit `h2h_record()`, `form_winrate()`, `days_since_last_match()`, `age_at_date()`, `rank_delta_6w()`
- **Ranking-Daten**: `src/data/tennis_rankings.py` (Scraper ATP/WTA Live-Rankings, wöchentlich Cron)
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: siehe oben, `tests/tennis/test_features.py` (~15 Tests)
- **Priorität**: P1

### P2.3 — Live-Gate-Re-Review nach 2 Wochen (12.08.)
- **Was**: `scripts/tennis_gate_review.py --live-window 14d` — vergleicht Backtest-Gates gegen 2 Wochen echte Performance
- **Achtung**: Manuelle Anpassung, kein Auto-Change (Feedback-Memory: „Keine vorschnellen Strategie-Änderungen")
- **Erwartete Sample-Size**: ~30-60 settled Bets
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Priorität**: P1

### P2.4 — Retrain-Cron aktivieren
- **Was**: `tennis_elo_refresh.yml` um `scripts/tennis_lgbm_retrain.py` erweitern (wöchentlicher LGBM-Retrain)
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Priorität**: P1

### P2.5 — Correlation-Guard für Multi-Market-Bets
- **Was**: Tennis-Correlation-Matrix in `src/betting/correlation.py`:
  - Match-Winner + First-Set: 0,75
  - Match-Winner + AH-1.5: 0,85
  - Match-Winner + Set-Betting (2-0): 0,60
  - O/U-Sets + O/U-Games: 0,50
- **Regel**: max 3 aktive Wetten pro Match ODER kumulierter Stake ≤ 8% Bankroll pro Match
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Dateien**: `src/betting/correlation.py`, `tests/tennis/test_correlation.py` (~10 Tests)
- **Priorität**: P1

---

## 🟧 P3 — Full Feature Set bis US Open (17 Tage)

**Ziel:** Grand-Slam-Ready, PWA-Tennis-Tab, Live-Stats, Retirement-Handling.

### P3.1 — Live-Serve/Return-Stats-Integration (J2-M)
- **Was**: `src/data/tennis_stats.py` — Serve %, Ace %, Break-Points-Won %, First-Serve %, Deuce-vs-AdCourt-Split
- **Datenquelle-Analyse (P3.1a, ~1 Tag)**:
  1. Sackmann-Repo (falls wieder online) — Primary
  2. Sofascore Tennis API (falls Quota) — Sekundär
  3. ATP/WTA Web-Scraper — brüchig aber möglich
  4. Fallback: P3.1 verschieben, LGBM ohne Serve-Stats weiterhin OK
- **Integration**: Als neue LGBM-Features (Retrain nötig)
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🔴
- **Priorität**: P2

### P3.2 — PWA Tennis-Tab
- **Was**: 4 Sub-Tabs in `docs/index.html` + `docs/js/app.js`:
  - Tennis-Overview (Live-Turniere, Signale-Feed)
  - Tennis-Stats (ROI/CLV per Kat/Surface/Market)
  - Tennis-Elo-Top20 (Overall/Hard/Clay/Grass + Tour-Toggle)
  - Tennis-Live-Board (aktuelle Matches mit In-Play-Odds — P4-Kandidat)
- **Design**: Bet365-Stil, grüner Top-Indicator, Nav `#1a2030` (Feedback-Memory)
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡
- **Wiederverwendung**: Football-Dashboard-Komponenten mit Sport-Filter erweitern
- **Priorität**: P1

### P3.3 — Match-Reminder + Kickoff-Push
- **Was**: `scripts/tennis_match_reminder.py` (15-25 Min vor Match-Start, Payload: Turnier + Round + Elo-Diff + Signals-Count)
- **Wiederverwendung**: `match_reminder.py` (Football, 25-35 Min pre-KO) als Blueprint
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Priorität**: P2

### P3.4 — Retirement/Walkover-Handling
- **Was**: `settle_tennis_market()` erweitern:
  - Retirement Set 1 → Match Winner + First Set = VOID
  - Retirement Set 2+ → Match Winner = Führender = WIN, AH & O/U-Sets voidet
  - Walkover → alle Märkte VOID
  - „In Progress" / „Suspended" (Regen) korrekt behandeln
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🔴
- **Tests**: `test_settlement.py` um ~15 Retirement/Walkover-Cases erweitern
- **Priorität**: P1

### P3.5 — Ranking-basierter Adjust (Rank-Injustice)
- **Was**: Feature `rank_elo_gap = elo_rank - atp_rank`; bei Gap > 20 Positionen → `elo_adjust = ±1,5% × min(20, gap/2)` als LGBM-Feature
- **Warum**: Elo hinkt bei Comeback-Spielern hinter Ranking her
- **Wiederverwendung**: Football `_rank_adjust()`-Konzept (±3%), aber schwächer (Tennis ist reiner)
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Priorität**: P2

### P3.6 — In-Play-Detector (Stretch-Goal)
- **Was**: TheOddsAPI `/inplay` für Tennis; EV-Check nach jedem Set-Ende bei Live-Odds > Fair-Odds
- **Regeln**: min_edge ≥ 15%, max 1 In-Play-Bet pro Match, striktes Kelly-Cap
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🔴
- **Realistische Einschätzung**: Vermutlich P4, nicht P3
- **Priorität**: P3

### P3.7 — US-Open-Slam-Prep-Checkliste (29.08. Abends)
- [ ] Alle Draws importiert (Sackmann/tennis-data.co.uk)
- [ ] Elo für alle 128 ♂ + 128 ♀ Draw-Spieler vorhanden
- [ ] Ranking-Cache frisch (< 3 Tage alt)
- [ ] Serve-Stats-Feed (P3.1) live oder NaN-toleriert
- [ ] BO5-spezifische min_edge geprüft (höhere Set-Varianz)
- [ ] Push-Test 24h vorher (fake settlement)
- [ ] Health-Dashboard 100% grün
- [ ] Manueller Scan der ersten Runde live

---

## 🟪 P4 — Post-Slam-Review + Basketball-Kickoff (2 Wochen)

**Ziel:** Erkenntnisse aus 6 Wochen echtem Tennis-Betting konsolidieren, danach Basketball-Fokus.

### P4.1 — Comprehensive Live-Backtest (nach US-Open-Final)
- **Was**: Live-ROI vs. Backtest-ROI-Delta per Kategorie/Surface; CLV-Distribution per Markt; Kalibrator-Refit (Isotonic pro Kombination)
- **Sample-Erwartung**: 100-200 settled Bets über 6 Wochen
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟢
- **Priorität**: P1

### P4.2 — Gate-Refit (echt datenbasiert)
- **Was**: LIVE promoten wenn Live-ROI + Backtest-ROI beide ≥ 3% bei kombiniertem n ≥ 100; BLACKLIST wenn Live-ROI ≤ -8% bei n ≥ 30
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟡
- **Priorität**: P1

### P4.3 — Modell-Learnings dokumentieren
- **Was**: `results/audits/tennis_us_open_review_2026-09-14.md` (Was funktionierte / Feature-Importance / Nächste Iteration)
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Priorität**: P2

### P4.4 — Basketball-Kickoff (parallel ab 15.08.)
- **Was**: **Außerhalb dieser Roadmap** — siehe zentrale `ROADMAP.md` J1
- **Kontext**: BBL 26.09., EuroLeague 24.09., NBA ~21.10. → Basketball-Modul-Bau parallel ab Mitte August
- **Priorität**: P2

---

## 📁 Files-Änderungen (Gesamt-Übersicht)

### Neue Files
```
ROADMAP_TENNIS.md                                   ← diese Datei
src/betting/tennis_settlement.py                    ← P1.1
src/models/tennis_lgbm.py                           ← P2.1
src/tennis/features.py                              ← P2.2
src/data/tennis_rankings.py                         ← P2.2
src/data/tennis_stats.py                            ← P3.1 (optional)
scripts/tennis_settle.py                            ← P1.1
scripts/update_tennis_closing_odds.py               ← P1.2
scripts/tennis_retrain.py                           ← P1.6
scripts/tennis_lgbm_retrain.py                      ← P2.4
scripts/tennis_post_match_push.py                   ← P1.4
scripts/tennis_match_reminder.py                    ← P3.3
.github/workflows/tennis_settle.yml                 ← P1.1
.github/workflows/tennis_closing_odds.yml           ← P1.2
.github/workflows/tennis_elo_refresh.yml            ← P1.6
models/tennis/{elo_snapshot,lgbm,lgbm_calibrator}.pkl + lgbm_features.json
tests/tennis/test_{settlement,closing_odds,post_match_push,dashboard_tennis_stats,retrain,lgbm,features,correlation}.py  ← ~120 neue Tests
```

### Geänderte Files
```
src/config.py                     ← WC2026_BOOST=1.0; TENNIS_CATEGORY_(SURFACE_)MODE nach P1.7
src/monitoring/health_writer.py   ← 4 neue Job-Schedule-Einträge
src/notifications/web_dashboard.py ← _build_tennis_stats() + tennis-Sektion
src/betting/tennis_detector.py    ← LGBM-Meta-Blend + Correlation-Guard
src/betting/correlation.py        ← Tennis-Correlation-Matrix
docs/index.html + docs/js/app.js  ← Tennis-Tab (P3.2)
```

### Deaktivierte Files (Rename `.disabled` oder Cron auskommentieren)
```
.github/workflows/{daily_scan,settle,closing_odds,auto_retrain,scrape_suspensions,live_score_push,prematch_scan}.yml
```

### Wiederverwendete Files (kein Change)
```
src/betting/kelly.py              ← shared, sport-agnostisch
src/betting/ledger.py             ← shared
src/notifications/web_push.py     ← VAPID, sport-agnostisch
src/monitoring/aggregate_health.py ← liest automatisch alle Health-Files
src/tennis/{tournaments,discovery,sim,elo_source,calibration}.py  ← bereits fertig
scripts/tennis_{scan,backtest,full_backtest,gate_review}.py       ← bereits fertig
```

---

## 🎚 Live-Gate-Matrix (Ist-Zustand + Ziel nach P1.7)

### Ist (Stand 2026-07-30, aus J2-H)

**Category-Level Default:**
```python
TENNIS_CATEGORY_MODE = {"grand_slam": "live", ...}  # rest shadow
```

**Surface-Overrides (überschreiben Category-Default):**
```python
TENNIS_CATEGORY_SURFACE_MODE = {
    ("atp500", "grass"):  "live",  # Backtest +18.6% ROI
    ("wta250", "grass"):  "live",  # +16.0%
    ("wta1000", "clay"):  "live",  # +8.4%
    ("wta500", "grass"):  "live",  # +8.1%
}
```

**Backtest-BLACKLIST (nicht in Live-Gate, aber im Scanner):**
- atp500 hard (-8.8%)
- grand_slam ATP grass (Wimbledon Herren, -9.5%)
- m1000 ATP clay (-9.4%)
- tour_final WTA hard (-13.0%)
- wta1000 hard (-5.3%)

### Ziel nach P1.7 (Update basierend auf Rerun)
Wird nach dem Backtest-Rerun mit Daten bis 2025 aktualisiert. Kernprinzip: **infrastruktur-mäßig alles abgedeckt** (Scanner läuft überall), aber **Live-Gates strikt nach Backtest-Verdict** — SHADOW-Kombinationen schreiben ins Signal-Archive, kommen aber nicht ins Ledger.

---

## ⚠️ Offene Fragen / Risiken

| Risiko | Impact | Mitigation |
|---|---|---|
| Sackmann-Repos bleiben offline | Kein T-1-Elo-Update | tennis-data.co.uk wöchentlich (P1.6) — Update-Lag 1-7 Tage akzeptabel |
| TheOddsAPI-Quota erschöpft | Kein Live-Scanning | WebSearch-Fallback (J2-I) + API-Usage-Monitor in Health |
| Retirement-Handling komplex | Falsche Settlements | Konservativ: bei Unsicherheit MANUAL_REVIEW, kein Auto-Void |
| Tennis-Live-Volume > erwartet (100+ Bets/Woche) | Kelly-Cap überlastet, Push-Flood | Rate-Limiter im Push, max 3 aktive Bets pro Tag |
| Football-Cron-Deaktivierung bricht PWA-Bestandsfeatures | Stale Football-Daten sichtbar | signals.json `football`-Sektion beibehalten, PWA-Warning „WM beendet" |
| P1 zu ambitioniert für 36h | Toronto-Start ohne volles Live | Fallback: nur P1.1 + P1.7 + P1.8 (Settlement + Backtest + Gate) — Rest nachschieben |
| Serve-Stats-Quelle für P3.1 nicht findbar | Weniger Features im LGBM | Akzeptabel — LGBM funktioniert ohne, nur Präzisionsverlust |

---

## 📝 Change-Log

| Datum | Commit | Änderung |
|---|---|---|
| 2026-07-30 | (pending) | + NEU: Datei angelegt. P0-P4 initial-strukturiert. |

---

## 🧭 Nächste Schritte

1. **Sofort (heute):** P0.1-P0.4 durchziehen (Football-Cron aus, Tests grün, Bankroll-Snapshot, diese Datei committen)
2. **31.07. früh:** P1.1-P1.6 parallel angehen (2-3 Sub-Sessions)
3. **31.07. Abends:** P1.7 (Backtest) + P1.8 (Gate-Update) + P1.9 (Smoke-Test) → Go/No-Go
4. **01.08. 12:00 UTC:** Live-Betrieb Toronto/Montreal
5. **02.08. → 13.08.:** P2 parallel zu Live-Betrieb (LGBM + Features)
6. **13.08. Cincinnati-Start:** LGBM live, alle Kategorien mit ML-Blend
7. **14.08. → 30.08.:** P3 (Serve-Stats, PWA-Tab, Retirement, Slam-Prep)
8. **30.08. → 13.09.:** US Open Live-Betrieb
9. **14.09. → 30.09.:** P4 (Review + Gate-Refit); parallel Basketball-Modul-Kickoff
