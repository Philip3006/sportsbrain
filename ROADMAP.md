# SportsBrain — ROADMAP

> **Lebende Quelle der Wahrheit** für alle Audit-Befunde, Entscheidungen und geplanten Arbeiten.
> Aktualisiert: 2026-06-20

---

## 🔄 Wartungs-Mechanik (verbindlich)

Diese Datei ist das einzige verbindliche Roadmap-Dokument. **Bei jeder Erwähnung von „Roadmap", „Masterplan", „in Zukunft", „Idee", „später bauen", „neues Feature", oder vergleichbaren Hinweisen auf zukünftige Arbeit gilt zwingend folgender Prozess:**

1. **Komplette Roadmap lesen** — die volle Datei, jede Sektion, keine Stichproben.
2. **Neue Idee aufnehmen** — als neues Item mit Was/Warum/Impact/Aufwand/Risiko/Priorität/Dateien/Abhängigkeiten/Verifikation. Konsistentes Format zwingend.
3. **Gesamt-Roadmap re-evaluieren** — passt das Item irgendwo besser rein? Werden andere Items dadurch obsolet oder verschoben? Ändert sich die Reihenfolge?
4. **Konsolidierte Übersicht ausgeben** — vollständige Roadmap mit der Änderung sichtbar markiert (`+ NEU`, `~ GEÄNDERT`, `- ENTFERNT`), inkl. aktualisierter Phasen-Reihenfolge und Statistik.
5. **Nichts vergessen** — alle bisherigen 47+ Items bleiben sichtbar; kein „verkürzte Übersicht", kein „nur das Relevante".

**Synchronisations-Regel**: Diese Datei wird **bei jedem inhaltlichen Roadmap-Turn** geschrieben (via Edit/Write). Mündliche Vorschläge ohne Schreibvorgang gelten als nicht aufgenommen.

---

## Bewertungsschlüssel

- **Impact**: 🟢 hoch · 🟡 mittel · ⚪ niedrig
- **Aufwand**: 🟢 niedrig (<1h) · 🟡 mittel (1-4h) · 🔴 hoch (4h+)
- **Risiko**: 🟢 niedrig · 🟡 mittel · 🔴 hoch
- **Priorität**: P0 (sofort) · P1 (diese Woche) · P2 (dieser Monat) · P3 (später)

---

## 🟦 A. Setup der Roadmap-Quelle (P0, ≈ 20 min)

### A1. `ROADMAP.md` als zentrale Quelle anlegen ✅
- **Was**: Diese Roadmap als Datei im Repo-Root.
- **Warum**: Memory ist unsichtbar; ROADMAP.md ist der erste Anlaufpunkt.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Dateien**: `ROADMAP.md`
- **Status**: ✅ Phase 0

### A2. `improvement_log.md` archivieren ✅
- **Was**: → `docs/archive/improvement_log_pre_wm.md` (1:1, kein Inhaltsverlust).
- **Warum**: Repo-Root aufräumen, History bleibt erreichbar.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Status**: ✅ Phase 0

### A3. CLAUDE.md erweitern um „Operations"-Block + Roadmap-Mechanik ✅
- **Was**: Operations-Commands (Ledger-Check, Sperren-CLI, Readiness-Check, Worker-Redeploy). Plus Verweis auf Roadmap-Mechanik.
- **Warum**: Ich (Claude) sehe CLAUDE.md in jedem Turn.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Status**: ✅ Phase 0

---

## 🟦 B. Block 1 — Sofortige Hygiene & Sicherheit (P0, ≈ 90 min)

### B1. `.env`-Backups aus Repo, `.gitignore` verschärfen
- **Was**: `.env.bak_pre_a1` löschen, `.gitignore` um `.env*` (außer `.env.example`) erweitern.
- **Warum**: Secret-Leak-Risiko bei versehentlichem `git add`.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `.gitignore`, `.env.bak_pre_a1` (delete)
- **Verifikation**: `git status` zeigt `.env` als ignoriert; `git ls-files | grep env` enthält keine Secret-Files.

### B2. Worker-CORS-Allowlist
- **Was**: `cloudflare/worker.js`-Funktion `cors()` von `*` auf Allowlist: `https://philip3006.github.io` + `http://localhost:*`.
- **Warum**: Reduziert Token-Klau-Risiko. Skaliert für Freunde-Onboarding.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟡 (Origin-Tippfehler bricht PWA)
- **Dateien**: `cloudflare/worker.js`
- **Verifikation**: `wrangler dev` → PWA funktioniert; `curl -H "Origin: https://evil.com"` blockiert.

### B3. Stale-Banner-Schwelle 26h → 90min
- **Was**: PWA-Frontend Stale-Threshold reduzieren.
- **Warum**: Cadence ist 30 min — 26h ist absurd großzügig.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (Z. ~605)
- **Verifikation**: künstlich `updated` auf 2h alt → Banner erscheint.

### B4. `results/*.bak*` archivieren
- **Was**: 17+ Backup-Files in `results/_archive/` verschieben.
- **Warum**: Audit-Hygiene.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `results/ledger_backup_*.csv`, `models/dixon_coles/*.bak_*`, `*.local_bak`
- **Verifikation**: `ls results/*.bak*` leer.

### B5. CLAUDE.md: Basketball-Status korrigieren
- **Was**: „Basketball" als „Phase 5 — Start zur Euroleague (Okt 2026) / BBL (Sept 2026) / NBA (Okt 2026)" markieren.
- **Warum**: Aktuelle CLAUDE.md erweckt den Eindruck, Basketball sei live.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `CLAUDE.md`

### B6. Telegram-Bot streichen
- **Was**: `scripts/telegram_bot.py` (537 Z.) löschen + Workflow/Config-Referenzen entfernen.
- **Warum**: PWA-Push ist primärer Kanal; doppelte Wartung lohnt nicht.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟡
- **Dateien**: `scripts/telegram_bot.py`, evtl. workflow-yamls, `src/notifications/telegram.py`
- **Verifikation**: `grep -r telegram .github/workflows/` leer.

### B7. Squads-Tab aus PWA-Nav entfernen
- **Was**: Tab aus Bottom-Nav + `view-squads` + Render-Code entfernen.
- **Warum**: Du nutzt ihn nicht; Backend-Squad-Daten brauchen kein UI.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html`

### B8. Operations-Checkliste verdichtet in CLAUDE.md
- **Was**: Aus improvement_log nur die noch manuell relevanten Commands (Ledger-Check, Sperren-CLI, Readiness-Check, Worker-Redeploy).
- **Warum**: Auto-Workflows haben den Rest übernommen.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Status**: Teil von A3, ergänzt mit konkreten Commands

---

## 🟦 C. Block 2 — Trust-UI (P1, ≈ 4–6 h)

### C1. „Why this bet?"-Drawer im Bet-Modal
- **Was**: Aufklappbarer Drawer mit Model-Prob, Market-Prob (Shin-fair), Edge (pp).
- **Warum**: Auf einen Blick verstehen warum eine Wette signalisiert wurde.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`_openBetModalFromBtn`, Modal-DOM)
- **Verifikation**: 5 Bets öffnen → Drawer-Werte = Backend-Output.

### C2. Confidence-Tier (LOW/MED/HIGH) sichtbar
- **Was**: Farbige Pille auf jedem Bet-Tile in Home + im Modal-Header.
- **Warum**: Trust-Signal — Solidität sofort erkennbar.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`oddsBtn`, `renderBets`, Modal)
- **Verifikation**: Tier-Werte aus PWA = `pre_match_scan.log`.

### C3. Forecast-Tab: Tooltip + 1-Zeilen-Erklärung
- **Was**: Über der Monte-Carlo-Tabelle eine Erklärung + Tooltip pro Spalte.
- **Warum**: Verständlichkeit für dich + Freunde.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (`view-forecast`)

### C4. Wochenrecap als UI-Karte im Journal
- **Was**: Oberste Karte: „Letzte 7 Tage: 18 Wetten · ROI +4.2% · 3W/2V/1L · CLV +1.8%".
- **Warum**: `weekly_recap.yml` produziert es als Push — UI-Karte macht es persistent.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (Journal-View), evtl. `signals.json`

### C5. Versionierungs-Pille im Footer
- **Was**: `v2026-06-20 · 5239c42` im PWA-Footer. Build-Zeit + Git-SHA via GitHub-Actions.
- **Warum**: Trust-Signal + Debug-Hilfe bei CI-Drift.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Dateien**: `docs/index.html`, evtl. Workflow-Step

### C6. API-Fail Empty-State mit Retry-Button
- **Was**: Bei `fetch('signals.json')`-Fail: Home zeigt „Daten konnten nicht geladen werden — [Neu laden]".
- **Warum**: Heute Skeleton endlos.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (`load()` + `renderHome`)

### C7. Onboarding-Overlay (3 Steps, einmalig)
- **Was**: Beim ersten Öffnen Overlay mit 3 Tipps. `localStorage.setItem('sb_seen_onboarding', '1')` nach Skip/Done.
- **Warum**: Für Freund-X-Onboarding kritisch.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Dateien**: `docs/index.html`
- **Verifikation**: `localStorage.clear()` → Overlay; Reload → nicht mehr.

---

## 🟦 D. Block 3 — Risiko & Multi-User-Vorbereitung (P1, ≈ 2–3 h)

### D1. Drawdown-Warnung als Banner
- **Was**: Bei Bankroll < 0.85 × `BANKROLL_START`: Warn-Banner. **Keine** Sperre.
- **Warum**: Selbstdisziplin-Anker ohne Auto-Pause.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html`, ggf. `src/betting/ledger.py`

### D2. Token-Rotation
- **Was**: Worker-Endpoint `POST /rotate_token` + Settings-UI-Button.
- **Warum**: Token-Wechsel ohne Worker-Redeploy. Wichtig für Freunde-Onboarding.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡 (Token-Bug → PWA offline)
- **Dateien**: `cloudflare/worker.js`, `docs/index.html`
- **Abhängigkeiten**: B2

### D3. Multi-Bankroll-Snapshot-Schema
- **Was**: `bankroll_snapshot.json` → `bankroll_snapshot_{user}.json` (Default `philip`).
- **Warum**: Friction-freier Onboarding-Pfad für Freund X.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Dateien**: `src/betting/ledger.py`, `src/config.py`, `data/cache/`

---

## 🟦 E. Block 4 — Refactor (P2, separater Sprint, ≈ 6–8 h)

### E1. Frontend-Smoke-Test mit Playwright
- **Was**: 3 Tests: PWA lädt, Bet-Modal öffnet, Stale-Banner bei manipuliertem `updated`.
- **Warum**: Schutznetz **vor** E2.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `tests/frontend/test_pwa_smoke.py` (neu)
- **Verifikation**: `pytest tests/frontend/ -q` grün.

### E2. `docs/index.html` aufteilen (3 875 → ~5 Files)
- **Was**: `index.html` (Markup), `css/app.css`, `js/app.js`, `js/views.js`, `js/bets.js`.
- **Warum**: Wartbarkeit, Caching, Refactor-Basis.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡
- **Dateien**: `docs/index.html` + 4 neue
- **Abhängigkeiten**: E1
- **Verifikation**: PWA visuell identisch; Smoke-Test grün.

### E3. `src/scanner/daily_scan.py` splitten (1 337 → <500/Datei)
- **Was**: `prep.py`, `scoring.py`, `output.py`, `daily_scan.py` (Orchestrator).
- **Warum**: 500-Zeilen-Regel; Test-Anker.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: `src/scanner/`, Test-Imports
- **Verifikation**: `pytest`, `daily_scan --mock` Diff = identisch.

### E4. `src/data/squad_availability.py` splitten (1 025 Z.)
- **Was**: `squad_transfermarkt.py`, `squad_wikipedia.py`, `squad_covers.py`, `squad_merger.py`.
- **Warum**: Klare Verantwortlichkeiten.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: `src/data/`

---

## 🟦 F. Resilienz & Stabilität (P1, parallel zu Block 1–3)

### F1. ESPN-Live-Score-Fallback härten
- **Was**: Dritte Quelle (Fotmob) oder ESPN-Retry mit Backoff (3 Versuche, 5/15/30s).
- **Warum**: Session-Report zeigt ESPN-DNS-Fails; Live-Scores essenziell für Settle.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟡
- **Dateien**: `src/data/espn.py` oder `scripts/settle_bets.py`, `src/data/fotmob.py`
- **Verifikation**: ESPN-DNS blocken → Fallback liefert.

### F2. DNS-Retry-Helper `_retry_request()` extrahieren
- **Was**: 3-Retry-Pattern aus #87 in `scripts/_http_retry.py`. Anwenden auf alle Cloudflare-Worker- + TheOddsAPI- + Sofascore-Calls.
- **Warum**: Wiederkehrendes DNS-Failure-Pattern; zentral lösen.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `scripts/_http_retry.py` (neu), 4-6 Skripte

### F3. CLV-Pre-1600-Bug: Stichprobe + Entscheidung
- **Was**: 10 abgerechnete Bets prüfen: wie viele `clv=""`? Wenn >2: Fix; wenn ≤2: akzeptieren.
- **Warum**: Vor F4 (CLV-UI) müssen Daten sauber sein.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢 (Stichprobe), 🔴 wenn Fix nötig
- **Dateien**: `results/ledger.csv`, evtl. `src/betting/ledger.py`, `scripts/update_closing_odds.py`

### F4. CLV im Journal anzeigen (abhängig von F3)
- **Was**: Pro Bet CLV-Pille + Aggregat oben im Journal-Tab.
- **Warum**: CLV ist langfristig wichtigster Profitabilitäts-Indikator.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`renderBets`), `signals.json`-Schema
- **Abhängigkeiten**: F3

---

## 🟦 G. Während laufender WM (P1, vor KO-Phase 2026-07-04)

### G1. PPDA als Shadow-Feature
- **Was**: `PPDA_LIVE_ENABLED=False`. Aggregation aus StatsBomb-Events (gleitender Mittelwert letzte 10 Matches). LGBM-Feature-Set erweitert, Live-Scanner ignoriert mit Flag. Backtest-Skript misst ROI-Diff.
- **Warum**: Vorbereitung ohne Live-Risiko.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟢 (Flag schützt)
- **Dateien**: `src/features/ppda.py` (neu), `src/features/builder.py`, `src/config.py`, `scripts/backtest_with_ppda.py` (neu)

### G2. Sperren-Tracking automatisieren
- **Was**: `scripts/scrape_suspensions.py` läuft täglich (Sofascore/WhoScored), füllt `data/suspensions.json`.
- **Warum**: Vor KO-Phase 2026-07-04.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🔴 (Sofascore-Quota erschöpft, WhoScored Cloudflare-geschützt)
- **Dateien**: `scripts/scrape_suspensions.py`, `.github/workflows/suspensions.yml`
- **⚠ Realitätscheck**: Wenn keine Quelle: manuelle CLI bleiben + Memory.

### G3. Wikipedia-Squad-Fallback verifizieren
- **Was**: Stichprobe 3 von 15 Cloudflare-blockierten Teams.
- **Warum**: Verifikation der dokumentierten Lösung.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Verifikation**: Markdown-Bericht mit Squad-Count.

---

## 🟦 H. Polish (P2, anytime)

### H1. Push-Notification-Deep-Link
- **Was**: Push-Payload mit `bet_id`; Service-Worker `notificationclick` → `?bet={id}` → Bet-Modal direkt.
- **Warum**: Push wird 1-Klick-Aktions-Trigger.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Dateien**: `docs/sw.js`, `docs/index.html`, `cloudflare/worker.js`, `src/notifications/web_push.py`
- **Abhängigkeiten**: B2, C1

### H2. Legal/Impressum/DSGVO-Stub
- **Was**: Leeres `docs/legal.html` mit Sektionen + Footer-Link.
- **Warum**: Vor Public-Launch füllen.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `docs/legal.html` (neu), `docs/index.html`

---

## 🟦 I. Nach WM-Ende (P1, ab 2026-07-20)

### I1. Multi-Liga-Persistence-Snapshot
- **Was**: `scripts/build_post_wm_snapshot.py` → `data/snapshots/wm2026_final.json` mit allen 64 Matches, Spielerstats (Min/G/A/xG/xA/Form), Team-Aggregaten, Confederation-Summary.
- **Warum**: Basis für Liga-Saison-Start (Bundesliga + Premier 2026-08-15, Euroleague Okt, NBA Okt).
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟢
- **Dateien**: `scripts/build_post_wm_snapshot.py` (neu), `data/snapshots/`
- **Verifikation**: Snapshot enthält 64 Matches + ~736 Spieler-Records.

### I2. WM-2026-Modell-Snapshot einfrieren
- **Was**: `models/snapshots/wm2026/` mit DC/LGBM/Stacker + `metadata.json` (Brier, ROI, Bet-Count).
- **Warum**: Spätere Vergleiche WM-Modell vs. Liga-Modell.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢

### I3. LightGBM + DC Retrain mit voller WM-Daten
- **Was**: Manueller Trigger nach WM-Ende mit `--include-wm-2026` Flag.
- **Warum**: 64 neue Matches = größte Datenerweiterung seit Training.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟡 (Brier-Regression möglich, dann Rollback)
- **Abhängigkeiten**: I1, I2

### I4. Backtest-Inkonsistenz MAX_EV beheben
- **Was**: `src/backtest/walk_forward.py` bekommt `apply_live_filters=True` Default. EV>40%-Filter, Confederation-Filter, MAX_ACTIVE_BETS im Backtest aktiv.
- **Warum**: Backtests sind nur valide, wenn sie das Live-System nachbilden.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: `src/backtest/walk_forward.py`, `scripts/backtest_*.py`, Tests

### I5. PPDA scharfschalten (nach Backtest-Gate)
- **Was**: `PPDA_LIVE_ENABLED=True`, **falls** Backtest-Gate (G1) ROI-Improvement ≥ 0.5pp UND Brier-Improvement ≥ 0.001.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟡
- **Abhängigkeiten**: G1

---

## 🟦 J. Saisonstart-Vorbereitung (P2, ab August 2026)

### J1. Basketball-Modul: Euroleague + BBL + NBA
- **Was**: `src/basketball/` mit Daten-Scraper (Basketball-Reference, Euroleague-API), Modellen (Pythagorean-Expectation, Pace-Adjusted Ratings, Basketball-Elo), Scanner, PWA-Tab.
- **Warum**: Saisons Sept-Okt 2026 (BBL ~26.09., Euroleague ~02.10., NBA ~21.10.).
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🔴 (neue Domain, Bankroll-Schutz wichtig)
- **Dateien**: `src/basketball/`, `docs/index.html`, `signals.json`-Schema
- **Abhängigkeiten**: I1
- **Verifikation**: Backtest auf historischen Saisons; Live erst nach 100+ Mock-Predictions.

### J2. Tennis-Modul ausbauen
- **Was**: Umfang separat besprechen. Erweiterung auf US-Open, Year-Round, MIN_EV-Anpassung.
- **Warum**: Backtest zeigt selektiv profitable Märkte (Wimbledon WTA +8.5%).
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Dateien**: `src/tennis/`, `scripts/tennis_scan.py`

### J3. Brier-Ziel <0.52 reevaluieren
- **Was**: Mit Multi-Liga-Daten erneuter Retrain → Brier-Audit.
- **Impact/Aufwand/Risiko**: ⚪ · 🟡 · 🟢
- **Abhängigkeiten**: I3, J1 (mind. 1 Monat Daten)

### J4. CONMEBOL Away-Bias Post-Mortem
- **Was**: WM-CONMEBOL-Outcomes vs. Confederation-Filter-Threshold. Wenn Bias aufgelöst: Filter relaxen.
- **Impact/Aufwand/Risiko**: ⚪ · 🟡 · 🟢
- **Dateien**: `src/betting/value_detector.py`, Backtest

---

## 🚫 K. Bewusst draußen (Veto-Liste)

| # | Idee | Veto-Grund |
|---|---|---|
| K1 | Service-Worker / Offline-Modus | Du bist online beim Wetten |
| K2 | PPDA direkt live ohne Backtest-Gate | Verzerrung existierender EV-Signale |
| K3 | Sport-Key-Config-Refactor jetzt | YAGNI — erst bei Bundesliga-Bau |
| K4 | Telegram als Backup-Kanal | PWA-Push reicht; doppelte Wartung |
| K5 | `_git_safe_push.sh` weiter härten | Iter #88/#89 reichen, abgehakt |
| K6 | Bookmaker-Auto-Bet via Bookie-API | Legal/ToS-Risiko |
| K7 | Social-Feed / Public-Leaderboard / Pick-Sharing | Lenkt vom Quant-Edge ab |
| K8 | Native iOS/Android-App | PWA reicht; App-Store-Reviews bei Gambling brutal |
| K9 | LLM-Erklärungen pro Wette | Hallucination-Risiko bei harten Stats |
| K10 | Live-per-Minute-Modelling | Sofascore-Quota erschöpft + Cost-Trap |
| K11 | Dark/Light-Toggle | Du nutzt nur Dark |

---

## 🗓️ Empfohlene Umsetzungs-Reihenfolge

| Phase | Items | Wann | Dauer |
|---|---|---|---|
| **0** | A1, A2, A3 (Roadmap-Setup) | ✅ erledigt | 20 min |
| **1** | B1–B8 (Hygiene & Sicherheit) | Tag 1 | 90 min |
| **2** | F1, F2 (Stabilität) | Tag 1-2 | 2-3 h |
| **3** | G3 (Wikipedia-Verify), G2 (Sperren-Auto) | bis 2026-07-03 | 2-4 h |
| **4** | G1 (PPDA Shadow) | bis 2026-07-15 | 4-6 h |
| **5** | F3, F4 (CLV-Audit + UI) | Tag 3-4 | 2-3 h |
| **6** | C1–C7 (Trust-UI) | Tag 4-6 | 4-6 h |
| **7** | D1–D3 (Risiko & Multi-User) | Tag 7 | 2-3 h |
| **8** | E1–E4 (Refactor) | Tag 8-12 | 6-8 h |
| **9** | I1–I5 (Post-WM Snapshot + Retrain) | 2026-07-20 bis 2026-07-31 | 8-12 h |
| **10** | H1, H2 (Push-Deep-Link, Legal-Stub) | anytime ab Tag 10 | 1-2 h |
| **11** | J1 (Basketball) | ab 2026-08-15 | 30-50 h |
| **12** | J2 (Tennis-Ausbau) | nach Spec-Klärung | 8-12 h |
| **13** | J3, J4 (Brier, CONMEBOL Audit) | Q4 2026 | 2-3 h |

---

## 📊 Statistik

- **Insgesamt**: 47 konkrete Items
- **P0**: 11 (sofort) — davon 3 ✅
- **P1**: 18 (diese Woche / vor KO-Phase)
- **P2**: 14 (dieser Monat / Refactor)
- **P3**: 4 (Q4 2026)
- **Veto**: 11 (bewusst nicht gebaut)

---

## 📝 Änderungs-Historie

- **2026-06-20**: Initiale Roadmap aus Audit-Phasen 1-7 + improvement_log-Durchgang. Phase 0 (A1-A3) erledigt.
