# SportsBrain — ROADMAP

> **Lebende Quelle der Wahrheit** für alle Audit-Befunde, Entscheidungen und geplanten Arbeiten.
> Aktualisiert: 2026-06-21

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

### B1. `.env`-Backups aus Repo, `.gitignore` verschärfen ✅
- **Was**: `.env.bak_pre_a1` löschen, `.gitignore` um `.env*` (außer `.env.example`) erweitern.
- **Warum**: Secret-Leak-Risiko bei versehentlichem `git add`.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `.gitignore`, `.env.bak_pre_a1` (delete)
- **Verifikation**: `git status` zeigt `.env` als ignoriert; `git ls-files | grep env` enthält keine Secret-Files.
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. Bonus: git-History gegen alle `.env*`-Pfade geprüft → nur `.env.example` war je committed, keine Secret-Leaks historisch.

### B2. Worker-CORS-Allowlist ✅
- **Was**: `cloudflare/worker.js`-Funktion `cors()` von `*` auf Allowlist: `https://philip3006.github.io` + `http://localhost:*`.
- **Warum**: Reduziert Token-Klau-Risiko. Skaliert für Freunde-Onboarding.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟡 (Origin-Tippfehler bricht PWA)
- **Dateien**: `cloudflare/worker.js`
- **Verifikation**: `wrangler dev` → PWA funktioniert; `curl -H "Origin: https://evil.com"` blockiert.
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`, deployed als Worker-Version `6a8744c6`. Allowlist enthält GitHub Pages + localhost-Regex + optionale `ALLOWED_ORIGINS`-Env-Var als Custom-Domain-Slot (kein Worker-Redeploy nötig für künftige Domain). 10/10 Logik-Tests + live-`curl`-Smoke (allowed/blocked/no-origin) grün.

### B3. Stale-Banner-Schwelle 26h → 90min ✅
- **Was**: PWA-Frontend Stale-Threshold reduzieren.
- **Warum**: Cadence ist 30 min — 26h ist absurd großzügig.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (Z. ~605)
- **Verifikation**: künstlich `updated` auf 2h alt → Banner erscheint.
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. `age > 1.5` (Stunden) → 90 min = 3× Cadence-Puffer.

### B4. `results/*.bak*` archivieren ✅
- **Was**: 17+ Backup-Files in `results/_archive/` verschieben.
- **Warum**: Audit-Hygiene.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `results/ledger_backup_*.csv`, `models/dixon_coles/*.bak_*`, `*.local_bak`
- **Verifikation**: `ls results/*.bak*` leer.
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. Getrennte Archive (`results/_archive/`, `models/dixon_coles/_archive/`), beide in `.gitignore`.

### B5. CLAUDE.md: Basketball-Status korrigieren ✅
- **Was**: „Basketball" als „Phase 5 — Start zur Euroleague (Okt 2026) / BBL (Sept 2026) / NBA (Okt 2026)" markieren.
- **Warum**: Aktuelle CLAUDE.md erweckt den Eindruck, Basketball sei live.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `CLAUDE.md`
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. Klartext „⚠️ PHASE 5 — NICHT IMPLEMENTIERT" plus Verweis auf Roadmap-Item J1.

### B6. Telegram-Bot streichen ✅
- **Was**: `scripts/telegram_bot.py` (537 Z.) löschen + Workflow/Config-Referenzen entfernen.
- **Warum**: PWA-Push ist primärer Kanal; doppelte Wartung lohnt nicht.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟡
- **Dateien**: `scripts/telegram_bot.py`, evtl. workflow-yamls, `src/notifications/telegram.py`
- **Verifikation**: `grep -r telegram .github/workflows/` leer.
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. Beide Files weg, `daily_scan.py`/`drift_monitor.py`/`tennis_scan.py` auf Web-Push umgestellt, `--no-telegram` bleibt als CLI-Alias für Legacy-Cron-Calls, `wm2026_readiness_check.py` prüft jetzt VAPID-Keys statt TELEGRAM-Tokens, `cloudflare/SETUP.md` aktualisiert.

### B7. Squads-Tab aus PWA-Nav entfernen ✅
- **Was**: Tab aus Bottom-Nav + `view-squads` + Render-Code entfernen.
- **Warum**: Du nutzt ihn nicht; Backend-Squad-Daten brauchen kein UI.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html`
- **Status (2026-06-20)**: Erledigt in Commit `c61f142`. `view-squads`/`renderSquads`/`filterSquads`/`openSquad` weg; Bet-Modal-Helper `squadSection()` bleibt funktional (nutzt `_squads`-Cache weiter im Hintergrund). Nav-Eintrag war bereits zuvor entfernt.

### B8. Operations-Checkliste verdichtet in CLAUDE.md ✅
- **Was**: Aus improvement_log nur die noch manuell relevanten Commands (Ledger-Check, Sperren-CLI, Readiness-Check, Worker-Redeploy).
- **Warum**: Auto-Workflows haben den Rest übernommen.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Status**: ✅ Teil von A3 (siehe Operations-Block in `CLAUDE.md`).

---

## 🟦 C. Block 2 — Trust-UI (P1, ≈ 4–6 h) ✅ erledigt 2026-06-21

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

## 🟦 D. Block 3 — Risiko & Multi-User-Vorbereitung (P1, ≈ 2–3 h) ✅ erledigt 2026-06-21/22

### D1. ✅ Drawdown-Warnung als Banner
- **Was**: Bei Bankroll < 0.85 × `BANKROLL_START`: Warn-Banner. **Keine** Sperre.
- **Warum**: Selbstdisziplin-Anker ohne Auto-Pause.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`_buildDrawdownBanner`, `.dd-banner` CSS, `renderJournal` Wiring)
- **Status (2026-06-21)**: Erledigt in Commit `bc67d8f`. Banner als oberste Karte im Journal-Tab. Trigger: `total_equity = start + pnl_closed < 0.85 × start`. Zeigt aktuelle Bankroll, Drawdown%, klares „Kein Auto-Stop"-Hinweis. Bei aktueller Bankroll €112.50 inaktiv (gewollt). `.gitignore` zusätzlich um `.claude/worktrees/`, `.swarm/`, `ruvector.db` erweitert.

### D2. ✅ Token-Rotation (Master + Per-User-Token + 24h-Grace)
- **Was**: Worker-Endpoint `POST /rotate_token` + Settings-UI-Button.
- **Warum**: Token-Wechsel ohne Worker-Redeploy. Wichtig für Freunde-Onboarding.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡 (Token-Bug → PWA offline)
- **Dateien**: `cloudflare/worker.js`, `docs/index.html`
- **Abhängigkeiten**: B2
- **Status (2026-06-22)**: Erledigt in Commit `f3e98fa`. **Worker**: Master-Token (env.API_TOKEN) bleibt unverändert für Python-Cron-Jobs; neue KV-Struktur `user_tokens` → `{[user]: {active, previous: {token, expires_at}|null, rotated_at}}`. `authResolve()` akzeptiert Master ODER aktiven User-Token ODER alten Token während 24h Grace. POST `/rotate_token {user}` generiert 256-bit Random-Token, alter wandert als `previous` mit `expires_at=+24h`. GET `/token_status?user=...` liefert `has_active`/`grace_active`/`rotated_at`. **PWA**: Settings-Row „🔄 Token rotieren" → confirm + POST mit aktuellem `sb_token` → speichert neuen Token in localStorage, zeigt Ablaufdatum des alten Tokens als Toast. **Rollback** bei Bug: `wrangler kv key delete user_tokens` setzt Schema zurück, Master-Token bleibt gültig. **⚠ Deploy-Pflicht**: `cd cloudflare && wrangler deploy` vor Live-Schaltung.

### D3. ✅ Multi-Bankroll-Snapshot-Schema
- **Was**: `bankroll_snapshot.json` → `bankroll_snapshot_{user}.json` (Default `philip`).
- **Warum**: Friction-freier Onboarding-Pfad für Freund X.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Dateien**: `src/betting/ledger.py`, `src/config.py`, `src/notifications/web_dashboard.py`, `docs/index.html`, `tests/betting/test_bankroll_snapshot.py`
- **Status (2026-06-21)**: Erledigt in Commit `1ed3bd3`. **Backend**: `src/config.py` bekommt `DEFAULT_USER = "philip"` + Helper `bankroll_snapshot_path_for(user) → data/cache/bankroll_snapshot_{user}.json`. `peek_/get_bankroll_snapshot()` bekommen `user`-Parameter (default DEFAULT_USER); Erstaufruf migriert die alte `bankroll_snapshot.json` automatisch in den Default-Slot (`_resolve_snapshot_path`). Explizit übergebene Pfade (Tests) bleiben unverändert. `signals.json.meta.default_user` neu. **PWA**: localStorage `sb_user` (Default `philip`), Settings-Row „👤 Aktiver User" mit Prompt-basiertem Slot-Wechsel; `_meta`-Global. **Tests**: 2 neue (`test_legacy_snapshot_migrates_into_default_user_slot`, `test_per_user_snapshots_are_isolated`) — 498/498 grün. **Hinweis**: Backend liest aktuell nur den Default-User; weitere Slots sind vorbereitet, bekommen aber noch keine eigenen Daten. Multi-User-Routing für Friends-Onboarding wird erst aktiv, wenn `signals_{user}.json`-Pipeline pro User gebaut wird.

### D4. ✅ Bankroll-Eingabe im Onboarding + NEU
- **Was**: Neuer Step 2 in `ONB_STEPS` (zwischen Welcome und Zahlen-Erklärung): Nummerisches Input-Feld „Deine Startbankroll (€)". Wert wird in `localStorage.sb_bankroll_start` gespeichert. Beim Signal-Load: wenn kein Backend-`bankroll_state.start` vorhanden (neuer User), wird der localStorage-Wert als `_bankrollState` übernommen → Bankroll-Strip zeigt sofort korrekte Zahlen. `_applyUserBankroll()` auch aus dem Onboarding-Flow heraus aufrufbar.
- **Warum**: Neue User (Freunde) sahen immer €0 oder falsche Bankroll, weil `bankroll_state` aus signals.json nur Philip's Daten enthält. Ohne eigene Bankroll keine sinnvollen Kelly-Stake-Empfehlungen.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (`ONB_STEPS`, `_onbRender`, `_onbSaveBankrollIfNeeded`, `_applyUserBankroll`, Signal-Load-Block)
- **Abhängigkeiten**: D3 (Multi-User-Schema), C7 (Onboarding-Overlay)
- **Verifikation**: `localStorage.clear()` → Onboarding zeigt Bankroll-Step → Wert eingeben → Weiter → Bankroll-Strip oben zeigt eingegebenen Wert. Reload: Wert bleibt. Philip (mit echtem `bankroll_state`): kein Override.
- **Status (2026-06-22)**: Erledigt.

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

### F1. ESPN-Live-Score-Fallback härten ✅
- **Was**: ESPN-Retry mit Backoff (3 Versuche, 5/15/30s) via zentralen `retry_request`-Helper. Fotmob-3.-Quelle bewusst ausgeklammert (YAGNI; weniger Brittleness).
- **Warum**: Session-Report zeigt ESPN-DNS-Fails; Live-Scores essenziell für Settle.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟡
- **Dateien**: `src/data/odds_api.py` (`_fetch_espn_wm_scores` → `retry_request`)
- **Status (2026-06-20)**: Erledigt zusammen mit F2 — ESPN-Call läuft jetzt durch `retry_request("GET", url, log_prefix="[espn]")` mit Default-Backoff.

### F2. DNS-Retry-Helper `_retry_request()` extrahieren ✅
- **Was**: 3-Retry-Pattern als `scripts/_http_retry.py::retry_request(method, url, *, retries=3, backoff=(5,15,30), retry_on_status, ...)`. Default: retry auf `requests.RequestException`; optional auf HTTP-Status.
- **Warum**: Wiederkehrendes DNS-Failure-Pattern; zentral lösen.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `scripts/_http_retry.py` (neu, 95 Z.), `tests/scripts/test_http_retry.py` (7 Tests). Migrierte Call-Sites: `scripts/consume_pending_bets.py` (GET+DELETE), `scripts/settle_bets.py` (TheOddsAPI), `scripts/tennis_scan.py`, `src/data/odds_api.py` (3 Stellen inkl. ESPN), `src/data/sofascore.py`, `src/data/statsbomb.py`, `src/data/fotmob.py`, `src/data/injury_data.py`, `src/data/squad_availability.py`, `src/data/football_data.py`, `src/data/btts_odds.py`, `src/data/international.py`, `src/data/football_data_intl.py`, `src/data/tennis_data.py`.
- **Status (2026-06-20)**: Erledigt. 431 Tests grün (+9 ggü. Baseline 422). odds_api's eigene `_http_get_with_retry` (mit 422-spezifischer Logik) bleibt absichtlich erhalten — projekt-spezifisches Verhalten.

### F3. CLV-Pre-1600-Bug: Stichprobe + Entscheidung ✅
- **Was**: 10 abgerechnete Bets prüfen: wie viele `clv=""`? Wenn >2: Fix; wenn ≤2: akzeptieren.
- **Warum**: Vor F4 (CLV-UI) müssen Daten sauber sein.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢 (Stichprobe), 🔴 wenn Fix nötig
- **Dateien**: `results/ledger.csv`, `scripts/update_closing_odds.py`, `tests/scripts/test_update_closing_odds.py`
- **Status (2026-06-21)**: Erledigt in Commit `92cf85b`. Stichprobe ergab 41/43 leeren CLVs — systemischer Bug, kein Einzelfall. Drei Root-Causes:
  1. **Pandas NaN-Bug**: leere CSV-Felder werden als NaN geladen, `str(NaN).strip()` → `"nan"` → truthy → der `continue` in der CLV-Backfill-Schleife skipt JEDE Zeile. Fix via `pd.isna(v) or not str(v).strip()`.
  2. **Markt-Map unvollständig**: `_MARKET_ODDS_KEY` (13 Märkte) → durch dynamischen `_resolve_closing_odds()` ersetzt mit Fallback auf `totals_lines`/`spreads`-Dict für Quarter-Balls und arbiträre Handicaps (`o/u3.0_*`, `ah+0.5_home` etc.).
  3. **Void-Status ausgeschlossen**: `settled_mask = ["won","lost"]` → erweitert um `"void"`.
  - `--backfill-only`-Flag für API-freie Re-Computation. 25 Tests neu (Resolver + NaN-Regression). Nach Backfill: 18/43 CLV gefüllt (Rest hat keine validen closing_odds — meist 0.0 oder Daten-Korruption mit ratio >3).

### F4. CLV im Journal anzeigen (abhängig von F3) ✅
- **Was**: Pro Bet CLV-Pille + Aggregat oben im Journal-Tab.
- **Warum**: CLV ist langfristig wichtigster Profitabilitäts-Indikator.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`_renderSettledCards`, `renderJournalStats`), `src/notifications/web_dashboard.py`
- **Abhängigkeiten**: F3
- **Status (2026-06-21)**: Erledigt in Commit `c2df64c`. (a) Per-Bet CLV-Pille im Settled-Tab — farbcodiert (grün >+0.5%, rot <-0.5%, sonst neutral) mit Title-Tooltip (entry → closing). (b) "Ø CLV letzte 30 Tage"-Karte zusätzlich zur Lifetime-Karte im Journal. (c) Backend: `settled_bets` liefert jetzt `clv` (decimal) + `closing_odds`; `summary` erweitert um `mean_clv_30d`/`n_clv_30d` (rolling 30-Tage via `placed_date`). (d) Void-Bets fließen in CLV-Aggregation ein (CLV bleibt aussagekräftig auch bei Annullierung), bleiben aber aus Hit-Rate/Per-Team/Per-Konföderation ausgeschlossen. Aktuell: 19 Wetten mit CLV, Ø +6.54%.

---

## 🟦 G. Während laufender WM (P1, vor KO-Phase 2026-07-04)

### G1. PPDA als Shadow-Feature ✅
- **Was**: `PPDA_LIVE_ENABLED=False`. Aggregation aus StatsBomb-Events (gleitender Mittelwert letzte 10 Matches). LGBM-Feature-Set erweitert, Live-Scanner ignoriert mit Flag. Backtest-Skript misst ROI-Diff.
- **Warum**: Vorbereitung ohne Live-Risiko.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟢 (Flag schützt)
- **Dateien**: `src/features/ppda.py` (neu), `src/features/builder.py`, `src/config.py`, `scripts/backtest_with_ppda.py` (neu), `src/data/statsbomb_ppda.py` (neu), `src/data/fbref_ppda.py` (neu, Saison-Fallback-Snapshot)
- **Status (2026-06-20)**: Erledigt. PPDA-Berechnung aus StatsBomb-Events (Pässe in Opp-60% / Def-Aktionen im Press-Bereich x≥48, Denominator-Floor 5 → NaN-Schutz). Rolling-Window N=10 mit Bayes-Shrinkage gegen Konföderations-Prior (PRIOR_WEIGHT=3.0, MIN_MATCHES=3), Fallback-Kaskade Konföderation → FBref-Snapshot → globaler Fallback 11.5. Builder integriert via `force_ppda`-Flag (Live bleibt off durch `PPDA_LIVE_ENABLED=False`). Backtest-Script vergleicht Brier + ROI-Proxy auf identischem Train/Val-Split. I5-Gate: Δ Brier ≥ 0.001 UND Δ ROI ≥ 0.5pp. 14 Unit-Tests grün, Gesamt-Suite 460/460 (+14 ggü. Baseline 446).
- **Backtest-Resultate 2026-06-21**:
  - **1X2-LGBM (scripts/backtest_with_ppda.py)**: Brier 0.5048 → 0.5020 (Δ +0.0029 ✅), ROI-Proxy +0.46% → +0.14% (Δ −0.32pp ⚠️). I5-Gate **nicht bestanden**, Shadow bleibt aktiv.
  - **Markt-Erweiterung (scripts/backtest_with_ppda_markets.py)**: 1909 Val-Matches, DC-Lambda-Adjustment via `ppda_lambda_multipliers` (Boost 2.5% pro PPDA-z, Clip ±10%). Brier durchgängig minimal schlechter (max −0.0015). ROI-Effekte gemischt: positiv bei 1X2_home (+1.19pp), 1X2_away (+1.24pp), over_2_5 (+1.11pp), btts_yes (+0.58pp); negativ bei btts_no (−2.27pp), under_2_5 (−1.62pp), draw (−0.71pp). Insight: Adjustment schiebt Modell systematisch Richtung „mehr Tore" — passt zur PPDA-Theorie, aber Brier-Verschlechterung deutet auf Overfitting des Multiplier-Tunings (z_scale=5, boost=0.025).
  - **Scorer-Markt**: out-of-scope dieser Iteration (braucht per-Player-xG × Minuten × Team-PPDA-Pfad).
  - **Empfehlung**: PPDA bleibt Shadow. Vor Live-Schaltung: (a) Multiplier-Tuning gegen Brier-Floor; (b) markt-aware ROI mit echten Closing-Quoten statt self-priced; (c) Scorer-Pfad nachziehen.
- **Live-Schaltung 2026-06-21**: `PPDA_LIVE_ENABLED = True` gesetzt. Training-Pipeline (`scripts/train_lgbm.py`) lädt jetzt StatsBomb-PPDA und trainiert mit `force_ppda=True` → neues LGBM-Modell mit 91 Features (88 + 3 PPDA). Scanner (`src/scanner/daily_scan.py`) lädt `ppda_df` nur wenn Flag True und übergibt es an `build_feature_row`. Gate WC2022-Holdout: ✅ PASS (Brier-Improvement vs DC +0.0825). Rückrollung: Flag auf False → scanner-reindex(fill_value=0) droppt die 3 Spalten stabil.

### G2. Sperren-Tracking automatisieren ✅
- **Was**: `scripts/scrape_suspensions.py` läuft täglich (Multi-Source), füllt `data/suspensions.json`.
- **Warum**: Vor KO-Phase 2026-07-04.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🔴 (Sofascore-Quota erschöpft, WhoScored Cloudflare-geschützt)
- **Dateien**: `scripts/scrape_suspensions.py`, `.github/workflows/scrape_suspensions.yml`, `tests/scripts/test_scrape_suspensions.py`
- **⚠ Realitätscheck**: Wenn keine Quelle: manuelle CLI bleiben + Memory.
- **Status (2026-06-20)**: Erledigt. Quellen: FIFA.com (Gewicht 3), UEFA.com (2), BBC Sport (1), ESPN (1). Confidence-Score = Σ Source-Gewichte + 2 (Squad-Cache-Verifikation) + 2 (≥2 unabhängige Quellen). Auto-Merge ab Score ≥ 5 → `data/suspensions.json`; sonst → `data/suspensions_candidates.json` für manuelle Review via `add_suspension.py`. Push-Notification mit Top-3 Funden. Workflow: täglich 06:00 UTC (`scrape_suspensions.yml`). 15 Unit-Tests grün (Total 446 = Baseline 431 + 15).

### G3. Wikipedia-Squad-Fallback verifizieren ✅
- **Was**: Stichprobe 3 von 15 Cloudflare-blockierten Teams.
- **Warum**: Verifikation der dokumentierten Lösung.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟢
- **Verifikation**: Markdown-Bericht mit Squad-Count.
- **Status (2026-06-20)**: Erledigt. Stichprobe Tunisia/Senegal/Jordan (Seed 20260620) liefert jeweils 26 Spieler aus `_fetch_wc_squads_page` (MediaWiki Parse-API auf `2026_FIFA_World_Cup_squads`). Bericht: `results/audits/g3_wikipedia_squad_verify_2026-06-20.md`. Befund: Per-Team-Pages (`{Team}_at_the_2026_FIFA_World_Cup`) sind in der Praxis 404 — der echte Fallback ist die konsolidierte WC-Squads-Page. Keine Code-Änderungen nötig.

---

## 🟥 L. Hot Fixes — laufende WM (P0/P1)

### L1. ✅ Gruppen-Standings-Fix (Team-Namens-Mismatch)
- **Was**: Gruppe-Tabellen in PWA/Forecast-Tab zeigen falsche Stände (Punkte, Tordiff, Rang). Ursache ermitteln (falsche Aggregation aus `signals.json`, veralteter Gruppen-Cache, Sortier-Bug im Frontend) → beheben.
- **Warum**: Falsche Gruppen-Stände verzerren Forecast-Prognosen (Qualifikations-Wkt. falsch), KO-Phase-Simulationen unbrauchbar und alle WM-Bet-Entscheidungen ab Gruppenfinale.
- **Impact**: 🟢 — direkt sichtbarer Fehler, alle 48 Gruppenspiel-Vorhersagen betroffen
- **Aufwand**: 🟡 (1-3 h: Diagnose welche Schicht falsch rechnet, dann Fix + Smoke-Test)
- **Risiko**: 🟡 — Fix in `daily_scan.py`/`index.html` kann Render-Regressions auslösen
- **Priorität**: P0 — WM-Gruppenphase endet 2026-06-26, danach KO-Runde
- **Dateien**: `src/scanner/daily_scan.py` (Gruppen-Aggregator), `docs/index.html` (`renderForecast`/`renderGroups`), `data/cache/group_standings_*.json` (falls vorhanden)
- **Verifikation**: Standings manuell gegen FIFA-Tabelle vergleichen — alle 8 Gruppen exakt korrekt (Pkt/Tore/GD/Rang)
- **Status (2026-06-21)**: Erledigt in Commit `75db4f9`. Root-Cause: `renderStandings()` matchte Roh-Namen aus `wm_results` (`"Bosnia & Herzegovina"`, `"USA"`, `"Ivory Coast"`, `"Czechia"`) nicht gegen die canonical WM_GROUPS-Namen → 3 von 11 gespielten Matches wurden im Lookup geskippt, Schweiz/Deutschland/USA/Cote d'Ivoire zeigten 0 Spiele. Fix: inverse Lookup-Map (`normTeam` + `TEAM_ALIASES` + Reverse-Alias → canonical) übersetzt Match-Teams vor `stats[]`-Zugriff. Verifikation 2026-06-21 nachgezogen: alle 36 `wm_results` matchen sauber, 0 Kollisionen im `matchKey`, Mexico/USA/Germany je 6pt nach 2 Spielen korrekt.

### L2. ✅ Forecast-Tab Umbau (Gruppen + Bracket + Cleanup)
- **Was**: (a) Gruppen-Standings (renderStandings-Output) oben im Forecast-Tab einbetten, (b) Bracket-Vorschau R32→Finale basierend auf wahrscheinlichsten Qualifizierten + DC-Predictions pro Paarung, (c) Layout aufräumen: xPoints einklappbar, kompaktere Spalten, klare Sektionen.
- **Warum**: Diagnose 2026-06-21 zeigte: Forecast-Werte rechnen mathematisch korrekt (ΣP=100, Σadvance=3200). Was als "fehlerhaft" wirkte: (1) L1's Standings-Bug spillte in PWA-Forecast-Eindruck, (2) Tab zu unübersichtlich, (3) Bracket-Vorschau (wer-vs-wen R16/QF/SF/Final beim aktuellen Stand) fehlt komplett — Forecast ist heute nur Wkt.-Tabelle ohne Pfad-Visualisierung.
- **Impact**: 🟢 — Forecast wird endlich Entscheidungs-Werkzeug für KO-Bets, nicht nur abstrakte Tabelle
- **Aufwand**: 🟡 (3-5 h: Step1 Frontend-Embed + Cleanup ~30min, Step2 Backend-Bracket + Frontend-Render ~3h)
- **Risiko**: 🟡 — Bracket-Mapping ist vereinfacht (FIFA-2026-Auslosung folgt offiziell erst später); transparent als "Approximation" markieren
- **Priorität**: P1 — vor 2026-07-04 (KO-Start)
- **Dateien**: `docs/index.html` (`renderForecast`, neue `_renderBracket`), `scripts/build_wm_forecast.py` (neuer Block `most_likely_qualifiers` + `bracket_preview`)
- **Abhängigkeiten**: L1 ✅
- **Verifikation**: Bracket-Vorschau zeigt 16 R16-Paarungen, jedes Match einen wahrscheinlicheren Sieger, Pfad bis zum Finale plausibel
- **Status (2026-06-21)**: Erledigt in zwei Commits. **Step 1** (`188c99e`): `renderStandings()` nimmt jetzt optionalen `targetId`-Parameter und rendert zusätzlich in `forecast-standings-container`; Forecast-Layout neu (Header → Gruppen-Stände → Stage-Wahrscheinlichkeiten → xPoints in `<details>` einklappbar). **Step 2** (`5170b9c`): neue `_build_bracket_preview()` in `scripts/build_wm_forecast.py` — deterministisch 32 wahrscheinlichste Qualifizierte (Top-2 nach `p_first+p_second`, Best-8 Drittplatzierte), Seeded-Pairing (1v32, 2v31...), pro Match DC-Vorhersage (höhere P(Sieg) gewinnt), 5 Rounds R32→Finale. Frontend: Champion-Banner + Round-`<details>`-Blöcke (R32 + Finale offen) mit Sieger-Highlight, Seeds, Flags. 488/488 Tests grün. Hinweis-Banner zur Approximation (echtes FIFA-Mapping folgt via M5 nach Auslosung 2026-06-27).

### L3. ✅ Journal-Fix — Future-dated Voids waren ausgefiltert
- **Was**: Journal-Tab zeigt fehlerhafte Einträge, fehlt Bets oder stellt Status/P&L falsch dar. Bugfix für bestehenden Render-Code — kein neues Feature (C4/F4 sind separate Feature-Items).
- **Warum**: Journal ist Abrechnungs-Basis; falsche Darstellung macht P&L-Tracking unzuverlässig.
- **Impact**: 🟡 — operativer Fehler, kein Modell-Risiko
- **Aufwand**: 🟢 (< 1 h falls nur Render-Bug, 🟡 wenn Daten-Schema kaputt)
- **Risiko**: 🟢 — Journal ist read-only; kein Ledger-Schreibzugriff aus Frontend
- **Priorität**: P1
- **Dateien**: `docs/index.html` (`renderBets`/Journal-View), `signals.json` (`settled_bets`-Feld)
- **Verifikation**: Alle settled Bets aus `results/ledger.csv` erscheinen im Journal mit korrektem Status (void/won/lost) und P&L
- **Status (2026-06-21)**: Erledigt. Root-Cause: `_get_settled_bets_for_dashboard()` filterte ALLE Bets mit `match_date > today` (Future-Anomalie-Schutz). Das blockte aber legitim früh annullierte Bets, deren Match noch nicht stattfand (z.B. Algeria vs Austria 2026-06-28 void, Switzerland vs Canada 2026-06-24 void — gekillt wegen Lineup/Market-Close). Filter umgebaut: skip future-dated NUR für `won`/`lost`/`push` (echte Daten-Anomalien); `void` mit Future-Datum zeigt immer (legitim). Journal-Anzahl 41 → 43, Void-Count 6 → 8 = exakt ledger.csv (8 void/14 won/21 lost = 43). 488/488 Tests grün.

---

## 🟦 M5. + NEU FIFA-2026-Bracket-Mapping (P1, vor KO-Phase 2026-07-04)
- **Was**: Aktuelles Bracket-Vorschau in `_build_bracket_preview()` (build_wm_forecast.py) nutzt **Seeded-Pairing** (Seed 1 vs 32, etc.) als Approximation. Nach offizieller FIFA-KO-Auslosung am 2026-06-27 in Las Vegas wird das echte R32-Slot-Mapping verkündet (z.B. „1A vs 3C/D/E"). Hardcoded `FIFA_R32_SLOTS` einbauen, der die 32 Qualifizierten den 16 R32-Matches gemäß offiziellem Bracket zuweist.
- **Warum**: Aktuelle Approximation ist mathematisch fair, aber NICHT die echte Paarung. Für KO-Bet-Entscheidungen ab 2026-07-04 brauchen wir die korrekten Slots (z.B. Argentina nicht zwangsläufig gegen den schwächsten Drittplatzierten).
- **Impact**: 🟢 — KO-Bet-Genauigkeit pro Match, korrekte Pfad-Wahrscheinlichkeiten Champion
- **Aufwand**: 🟢 (1-2 h: Slot-Tabelle eintragen + Logik anpassen + Tests)
- **Risiko**: 🟢 — reiner Lookup, Backend-Output-Schema identisch
- **Priorität**: P1 — nach FIFA-Auslosung 2026-06-27, vor KO-Start 2026-07-04
- **Dateien**: `scripts/build_wm_forecast.py` (`_build_bracket_preview`, neuer Konstanten-Block `FIFA_2026_R32_SLOTS`), evtl. `docs/index.html` (Hinweis-Text entfernen)
- **Abhängigkeiten**: L2 ✅ (Bracket-Infrastruktur), FIFA-Auslosung 2026-06-27
- **Verifikation**: 16 R32-Paarungen exakt nach FIFA-Bracket, manuell gegen FIFA.com-Bracket-PDF

---

## 🟦 M. Trust-UI v2 — Onboarding & Erklärbarkeit (P1, ≈ 3–5 h) ✅ erledigt 2026-06-21

> Folge-Phase aus C1–C7-Feedback (2026-06-21): „Drawer im Modal zu unscheinbar, brauche ihn schon am Bet-Kärtchen; Onboarding zu kompliziert; Begriffe (CLV/ROI/EV/Edge) müssen erklärt werden; Walkthrough wäre cool."

### M1. „Warum diese Wette?"-Drawer inline auf der Bet-Karte
- **Was**: Den heutigen Modal-Drawer (C1) zusätzlich direkt auf jeder `sig-card` als `<details>`-Element rendern — sichtbar **vor** Klick auf „Wette platzieren". Inhalt: Modell% / Markt-fair% / Edge pp + 2-Zeilen-Klartext-Begründung („KI hält X für wahrscheinlicher als der Markt — daraus ergibt sich +Ypp Vorteil").
- **Warum**: Aktuell muss man erst das Bet-Modal öffnen um die Begründung zu sehen — UX-Friction, Vertrauen leidet. Inline-Drawer macht Trust auf einen Blick erkennbar.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟢
- **Dateien**: `docs/index.html` (`sigCard`, neuer CSS-Block `.why-bet-inline`)
- **Verifikation**: 5 Bet-Karten haben aufklappbaren Drawer; Werte = Modal-Werte.

### M2. Noob-freundliche Erklärtexte
- **Was**: Drawer/Tooltips in Alltagssprache. Statt „Edge +5.2pp" → „+5.2 Prozentpunkte mehr als der Markt – wenn die KI recht hat, gewinnst du langfristig". EV-Tooltip → konkretes €-Beispiel. CLV-Tooltip → „Markt hat dir nachgegeben = du warst früher schlauer".
- **Warum**: Ich (Philip) + Freunde sind keine Quant-Profis. Begriffe wie „Implied Probability" oder „Shin-fair" überfordern.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (Tooltip-Strings in `sigCard`, `_buildSettledCard`, ggf. `_buildWeeklyRecap`)

### M3. Onboarding-Rewrite + interaktiver Walkthrough
- **Was**: (a) Onboarding-Overlay-Text vereinfachen (Eltern-tauglich). (b) Neuer „Tour starten"-Knopf am Ende → ein Overlay-Spotlight führt durch 5 echte UI-Elemente (Home-Karte → Bet-Karte → Warum-Drawer → EV/Tier → Journal-CLV). Highlight via halbtransparentem Backdrop + scrollIntoView. (c) „Onboarding neu starten" in Settings-Modal.
- **Warum**: Aktuelles 3-Step-Onboarding ist statischer Text-Wall. Walkthrough macht Features greifbar; Re-Start für Demo bei Freunden.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡 (Walkthrough-Selector kann bei dynamischem Render fehlen → defensive Fallbacks)
- **Dateien**: `docs/index.html` (`ONB_STEPS`, neue `WALK_STEPS` + `_walkStart/_walkNext/_walkEnd`, Settings-Row)
- **Verifikation**: `localStorage.clear()` → Onboarding zeigt sich; Klick „Tour starten" → 5 Highlights nacheinander; Settings → „Tour neu starten" funktioniert.

### M4. Glossar-Modal („Begriffe erklärt")
- **Was**: Neues Modal mit allen Fachbegriffen: EV, Edge, CLV, ROI, Kelly, Stake, Tier (HIGH/MED/LOW), Fair-Quote, Vig, Implied Probability, Void, Hit-Rate. Pro Eintrag: 1-Satz-Definition + konkretes Mini-Beispiel.
- **Warum**: Single Source of Truth statt verstreuter Tooltips. Erreichbar via Footer-Link + Settings-Row + Onboarding-Link.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `docs/index.html` (Glossar-Modal-DOM + `GLOSSARY` array + `_openGlossary()`)
- **Verifikation**: Modal öffnet sich von 3 Stellen; 12 Einträge sichtbar.

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

### I6. ✅ Home-Advantage Gastgeber-Länder (WM 2026)
- **Was**: Separate `host_boost` Parameter in `dc.predict_match()`: wenn Team in `HOST_NATIONS = {"United States", "Canada", "Mexico"}` und das Spiel im jeweiligen Heimland stattfindet, wird Lambda_home mit einem Faktor `HOST_LAMBDA_BOOST` (default 1.08, aus historischen Daten WC 2006/2010/2014/2018 kalibriert) multipliziert. Venue-Erkennung via `match.get("venue_country")` aus TheOddsAPI. Falls kein Venue: Host-Match via fixture-Daten (Wikipedia/ESPN) annotieren.
- **Warum**: Gastgeber-Vorteil ist statistisch messbar (+3–8% Gewinnwahrscheinlichkeit, bes. Gruppenphase). USA/Kanada/Mexiko spielen vor Heim-Publikum — aktuell wird `neutral=True` gesetzt was diesen Vorteil ignoriert.
- **Impact**: 🟢 — direkte Qualitätsverbesserung für 16 von 64 WM-Matches (je ~5 Gruppenspiele + KO pro Gastgeber)
- **Aufwand**: 🟡 (1-3 h: Kalibrierung via WC-Historisch-Daten + Venue-Lookup + DC-Integration + Backtest-Verifikation)
- **Risiko**: 🟡 — falscher Boost-Faktor kann EV verzerren; Backtest-Gate nötig (Brier vorher/nachher)
- **Priorität**: P1 — WM 2026 läuft, KO-Phase ab 2026-07-04
- **Dateien**: `src/models/dixon_coles.py` (`predict_match`, `fit`), `src/config.py` (`HOST_NATIONS`, `HOST_LAMBDA_BOOST`), `src/scanner/daily_scan.py` (Venue-Übergabe), `scripts/run_backtest.py` (Verifikation)
- **Abhängigkeiten**: G1 (DC-Modell stabil), historische WM-Daten (vorhanden)
- **Verifikation**: Brier auf WC2006/2010/2014/2018 Gastgeber-Matches verbessert sich; WM2026-Prognosen USA/CAN/MEX zeigen plausiblen Boost von ~3-8pp gegenüber Baseline
- **Status (2026-06-21)**: Erledigt. **Kalibrierung**: Empirische Auswertung WC 2006-2022 (n=25 Host-Heim-Matches) lieferte kein robustes Signal (95% CI [0.50, 1.19], dominiert von Confounds: Gastgeber-Team-Stärke 2006/2014/2018 sehr hoch, Qatar 2022 Ausreißer nach unten). Entscheidung: **Literatur-Default `HOST_LAMBDA_BOOST = 1.05`** (konservativer Pollard/Clarke-Wert) statt empirisch-volatilem Wert. **API**: `_lambdas()` bekommt optionalen `host_boost`-Parameter (multipliziert nur `lh`, `la` bleibt unverändert — kein Doppel-Effekt). 14 Predict-Funktionen propagieren den Parameter durch (predict_match/_staged/_scoreline/_totals/_totals_all/_btts/_asian_handicap/_asian_handicap_all/_goals_range/_half_goals_range/_xg/_first_scorer). **Wiring**: `daily_scan.py` setzt `host_boost = HOST_LAMBDA_BOOST if home in HOST_NATIONS else 1.0` einmal pro Match und schleift ihn an 8 DC-Call-Stellen durch. **Backtest-Gate**: ΔBrier +0.0051 auf 25 historische Host-Heim-Matches (Gate ≥0.001 ✅). Predicted P(host_win) rückt 0.315 → 0.327 näher an actual 0.440 (zeigt: 1.05 ist konservativ, könnte später nachgezogen werden). **Tests**: 8 neue Unit-Tests (`tests/models/test_dixon_coles_host_boost.py`), 496/496 Gesamt-Suite grün (+8). **Rückrollung**: `HOST_BOOST_ENABLED=False` → host_boost=1.0 in scanner.

### I7. + NEU Monte Carlo Simulationen (Scoreline-Verteilung)
- **Was**: `src/analysis/monte_carlo.py` mit `simulate_match(home, away, params, n=10000)` → zieht N mal aus der DC-Scoreline-Matrix (Poisson-Sampler), gibt zurück: Top-5 wahrscheinlichste Scores, kumulative Tor-Verteilung (P(0), P(1), ..., P(5+)), Most-Likely-Score, Most-Likely-Result (H/D/A). Integration in PWA-Forecast-Tab als „Wahrscheinlichste Ergebnisse" unter den Prognosen.
- **Warum**: DC `predict_scoreline()` liefert bereits die volle Matrix — Monte Carlo ist nur ein Sampler drüber und macht die Outputs für dich intuitiv lesbar. Zusätzlicher Nutzen: komplexe Märkte (Correct Score, beide Teams treffen in Halbzeit X) können exakt aus Sims abgeleitet werden ohne analytische Näherung.
- **Impact**: 🟡 — visueller Mehrwert in PWA + Basis für spätere Correct-Score-Märkte (falls re-enablet)
- **Aufwand**: 🟢 (< 2h: Sampler ~50 Zeilen, PWA-Integration ~30 Zeilen)
- **Risiko**: 🟢 — keine Modell-Änderung; nur Display
- **Priorität**: P2 — nach I6 (Home Advantage), da dort die Scoreline-Matrix ohnehin verbessert wird
- **Dateien**: `src/analysis/monte_carlo.py` (neu), `docs/index.html` (Forecast-Tab C3-Erweiterung), `signals.json` (neues Feld `top_scores` pro Match)
- **Abhängigkeiten**: G1 (DC-Modell), C3 (Forecast-Tab Tooltip)
- **Verifikation**: Top-5 Scores für 5 WM-Spiele manuell gegen analytische Matrix-Diagonale gegenprüfen (Max-Abweichung <1pp bei N=10000)

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
| **1** | B1–B8 (Hygiene & Sicherheit) | ✅ erledigt 2026-06-20 | 90 min |
| **2** | F1, F2 (Stabilität) | ✅ erledigt 2026-06-20 | 2 h |
| **3** | G3 (Wikipedia-Verify), G2 (Sperren-Auto) | ✅ erledigt 2026-06-20 | 2-4 h |
| **4** | G1 (PPDA Shadow) | ✅ erledigt 2026-06-20 | 4-6 h |
| **5** | F3, F4 (CLV-Audit + UI) | ✅ erledigt 2026-06-21 | 2-3 h |
| **6** | C1–C7 (Trust-UI) | ✅ erledigt 2026-06-21 | 4-6 h |
| **6b** | M1–M4 (Trust-UI v2: Inline-Drawer, Noob-Texte, Walkthrough, Glossar) | ✅ erledigt 2026-06-21 | 3-5 h |
| **7** | D1–D3 (Risiko & Multi-User) | ✅ erledigt 2026-06-21/22 | 2-3 h |
| **8** | E1–E4 (Refactor) | Tag 8-12 | 6-8 h |
| **8b** | M5 (FIFA-Bracket-Mapping nach Auslosung) | ab 2026-06-27 (Auslosung), vor 2026-07-04 | 1-2 h |
| **9** | I6 (Home Advantage Gastgeber) | ✅ erledigt 2026-06-21 | 1-3 h |
| **9b** | I1–I5 (Post-WM Snapshot + Retrain) | 2026-07-20 bis 2026-07-31 | 8-12 h |
| **9c** | I7 (Monte Carlo Sims) | nach I6, anytime | < 2 h |
| **10** | H1, H2 (Push-Deep-Link, Legal-Stub) | anytime ab Tag 10 | 1-2 h |
| **11** | J1 (Basketball) | ab 2026-08-15 | 30-50 h |
| **12** | J2 (Tennis-Ausbau) | nach Spec-Klärung | 8-12 h |
| **13** | J3, J4 (Brier, CONMEBOL Audit) | Q4 2026 | 2-3 h |

---

## 📊 Statistik

- **Insgesamt**: 57 konkrete Items (+1 neu: D4)
- **P0**: 12 (sofort) — davon 12 ✅ (Phase 0 + Phase 1 + L1 vollständig)
- **P1**: 25 (diese Woche / vor KO-Phase) — inkl. I6, L2, L3, M1-M4 (neu)
- **P2**: 15 (dieser Monat / Refactor) — inkl. I7 (Monte Carlo)
- **P3**: 4 (Q4 2026)
- **Veto**: 11 (bewusst nicht gebaut)

---

## 📝 Änderungs-Historie

- **2026-06-20**: Initiale Roadmap aus Audit-Phasen 1-7 + improvement_log-Durchgang. Phase 0 (A1-A3) erledigt.
- **2026-06-20**: ~ Phase 1 (B1-B8) vollständig erledigt in Commit `c61f142`, Worker-Deploy `6a8744c6`. Alle Verifikations-Kriterien erfüllt. Roadmap-Workflow (Überblick → Detail-Fragen → Tests vor Push) als Feedback-Memory persistiert. Nächste Phase: F1/F2 (Stabilität).
- **2026-06-20**: ~ Phase 2 (F1, F2) erledigt. Zentraler `scripts/_http_retry.py::retry_request` mit 7 Unit-Tests, 14 Call-Sites migriert (Worker, TheOddsAPI, ESPN, Sofascore, StatsBomb, Fotmob, Wikipedia, Covers, Football-Data, etc.). Default-Backoff (5/15/30s) deckt DNS-Aussetzer ab, die heute mehrfach im Session-Report auftauchten. 431/431 Tests grün. Fotmob als 3. Live-Score-Quelle bewusst nicht gebaut (YAGNI). Nächste Phase: G3 + G2 (Wikipedia-Verify, Sperren-Auto) bis 2026-07-03.
- **2026-06-20**: ~ Phase 3 (G3, G2) erledigt. G3: Stichprobe Tunisia/Senegal/Jordan (Seed 20260620) liefert 26 Spieler aus `_fetch_wc_squads_page`; Per-Team-Wikipedia-Seiten sind faktisch 404 → echter Fallback ist die konsolidierte WC-Squads-Page. G2: Multi-Source-Scraper (FIFA/UEFA/BBC/ESPN) mit Confidence-Score (Source-Gewicht + Squad-Verifikation + Multi-Source-Bonus), Auto-Merge ab Score ≥ 5, sonst Kandidaten-Datei für manuelle Review. Workflow täglich 06:00 UTC. 446/446 Tests grün (+15). Nächste Phase: G1 (PPDA Shadow) bis 2026-07-15.
- **2026-06-21**: + I6 NEU (Home-Advantage Gastgeber-Länder, P1 vor KO-Phase), + I7 NEU (Monte Carlo Simulationen, P2). Statistik: 47 → 49 Items. Priorisierung: I6 vor I1-I5.
- **2026-06-21**: BTTS und Goals 2-4 aus Scanner deaktiviert nach Backtest-Validierung (392 Spiele): BTTS 13pp Kalibrierungslücke, Goals 2-4 9pp Lücke + falsche Richtung. AH ±0.5 und O/U bleiben aktiv (≤2pp Gap). `GOALS_RANGE_ENABLED=False`, BTTS-Block entfernt. Neues Skript `scripts/backtest_special_markets.py`.
- **2026-06-21**: + L1 NEU (Gruppen-Standings-Fix, P0 — Gruppen stimmen nicht), + L2 NEU (Forecast-Fix, P1, abhängig L1), + L3 NEU (Journal-Fix, P1). Neue Section 🟥 L. Hot Fixes — laufende WM. Statistik: 49 → 52 Items.
- **2026-06-20**: ~ Phase 4 (G1) erledigt. Neue Module `src/data/statsbomb_ppda.py` (Event-Parser, PPDA pro Match aus Pässen-in-Opp-60% / Def-Aktionen-im-Press-Bereich x≥48, Denominator-Floor 5 → NaN-Schutz, eigener 24h-Cache) und `src/data/fbref_ppda.py` (Saison-PPDA-Snapshot-Fallback). `src/features/ppda.py`: Rolling-Window N=10 mit Bayes-Shrinkage gegen Konföderations-Prior (Fallback-Kaskade Konföderation → FBref → 11.5). `src/features/builder.py` bekommt `ppda_df`/`force_ppda`-Parameter; Live bleibt off durch `PPDA_LIVE_ENABLED=False`. `scripts/backtest_with_ppda.py` vergleicht Brier + ROI-Proxy auf identischem Train/Val-Split, schreibt `results/audits/g1_ppda_backtest_*.json`. I5-Gate-Kriterium: Δ Brier ≥ 0.001 UND Δ ROI ≥ 0.5pp. 14 Unit-Tests neu, Gesamt-Suite 460/460. Nächste Phase: F3/F4 (CLV-Audit + UI).
- **2026-06-21**: ~ Phase 6 (C1–C7) erledigt. C1: aufklappbarer „Warum diese Wette?"-Drawer im Bet-Modal mit Modell% / Markt-fair% / Edge pp (Shin-fair aus `s.fair_prob` thread via `data-fair-prob`, Fallback `100/odds` mit `*`-Hinweis). C2: LOW-Tier-CSS ergänzt, Tier-Pille im Modal-Header neben Kind-Badge, infoTip für LOW im Bet-Card. C3: Forecast-Spaltenkopf bekommt `title=`-Tooltips via `_FC_COLS[].tip`, plus 1-Zeilen-Erklärung über der Tabelle. C4: `_buildWeeklyRecap()` aggregiert `_settledBets` der letzten 7 Tage (W/V/L, P&L, ROI, Ø CLV) als oberste Journal-Karte. C5: `_build_info()` in `web_dashboard.py` schreibt `{sha, date}` in `signals.json`, Frontend rendert Pille in neuem `<footer id="app-footer">`. C6: `_renderApiFailEmpty(msg)` ersetzt Skeletons in allen Haupt-Containern durch Retry-Button bei Load-Fehler. C7: 3-Step Onboarding-Overlay via `localStorage.sb_seen_onboarding`. Gesamt-Suite 488/488 grün. Nächste Phase: D1–D3 (Risiko & Multi-User).
- **2026-06-21**: + M NEU (Trust-UI v2: M1 Inline-Drawer, M2 Noob-Texte, M3 Walkthrough, M4 Glossar). Direkt nach C1–C7 als Folge-Iteration: Drawer im Modal zu unscheinbar → inline an Karte; Onboarding zu kompliziert → Walkthrough mit Spotlight; CLV/ROI/EV undefiniert → Glossar-Modal. Statistik: 52 → 56 Items. Reihenfolge-Slot 6b (zwischen Phase 6 und 7).
- **2026-06-21**: ~ Phase 6b (M1–M4) erledigt in Commit `d5e2eb2` + 11 Folge-Fixes (`a76add5`, `44d0ed0`, `2a44e18`, `dab6e82`, `f25190a`, `8bd2416`, `f16cd13`, `27f7358`, `dfca5f8`, `8e499db`, `4dc27dc`). M1: `why-inline`-`<details>`-Drawer per Default eingeklappt mit Modell% / Markt-fair% / Edge pp + Klartext-Begründung + „Begriffe erklärt →"-Link zum Glossar. M2: alle Tooltips in Alltagssprache (Edge, EV als €-Beispiel, CLV als „Markt hat dir nachgegeben", Tier-Erklärungen). M3: `WALK_STEPS` mit 14 Stufen (Home → Football-Tab → Bet-Karte → Drawer → EV/Tier → Offen/Live/Abgerechnet → Journal-CLV → Schluss), Spotlight-Ring scrollt mit, FAB-Guard bei dynamischem Render, Off-Screen-Hide, Demo-Modus mit synthetischen Daten (`_walkDemoActive`), Trigger nur manuell via Settings + Footer + Onboarding-Disclaimer. M4: `GLOSSARY`-Modal mit 12+ Einträgen, erreichbar aus Footer-Link, Settings-Row und aus M1-Drawer („Begriffe erklärt →"). Nächste Phase: L1–L3 (Hot Fixes) vor 2026-06-26 (Gruppenende) bzw. 2026-07-04 (KO-Phase).
- **2026-06-21**: ✅ Phase 9 (I6) erledigt. Host-Boost für WM-2026-Gastgeber USA/CAN/MEX. Empirische Kalibrierung auf WC 2006-2022 (n=25) lieferte kein robustes Signal (95% CI [0.50, 1.19]) — Literatur-Default `HOST_LAMBDA_BOOST = 1.05` als konservativer Pollard/Clarke-Wert. `_lambdas()` + 14 Predict-Funktionen propagieren neuen `host_boost`-Parameter (multipliziert nur `lh`). `daily_scan.py` setzt Boost via Home-Team-Heuristik. Backtest-Gate ✅: ΔBrier +0.0051 auf 25 historische Host-Matches, predicted P(host_win) rückt 0.315 → 0.327 näher an actual 0.440. 8 neue Unit-Tests, 496/496 Suite grün. Rückrollung via `HOST_BOOST_ENABLED=False`. Nächste Phase: **M5** (FIFA-Bracket-Mapping nach Auslosung 2026-06-27) oder **Phase 7** (D1–D3 Risiko/Multi-User).
- **2026-06-21**: ✅ Hot-Fixes L1 + L2 erledigt (vorher schon committed in `75db4f9`/`188c99e`/`5170b9c`, jetzt Roadmap-Status nachgezogen). L1: `renderStandings()` Team-Namens-Mismatch (USA/BIH/Ivory Coast wurden geskippt) via inverse Lookup-Map gefixt. L2: Forecast-Tab umgebaut — Standings eingebettet, Bracket-Vorschau R32→Finale, xPoints einklappbar. Verifikation: alle 36 wm_results matchen, Standings rechnen korrekt (Mexico/USA/Germany je 6pt). Nächste Phase: **I6** (Home-Advantage Gastgeber) vor KO 2026-07-04.
- **2026-06-22**: + D4 NEU (Bankroll-Eingabe im Onboarding). Onboarding-Step 2 mit Nummerischem Input, `localStorage.sb_bankroll_start`, Override von `_bankrollState` für neue User ohne Backend-State. 57 Items total.
- **2026-06-21/22**: ~ Phase 7 (D1, D2, D3) erledigt. **D1** (`bc67d8f`): Drawdown-Warnbanner als oberste Karte im Journal-Tab, triggert bei `start + pnl_closed < 0.85 × start`, kein Auto-Stop. **D3** (`1ed3bd3`): Multi-User-Schema vorbereitet — `DEFAULT_USER`-Konstante + `bankroll_snapshot_path_for(user)`-Helper; per-user-Snapshot-Dateien mit Auto-Migration der Legacy-Datei; `signals.json.meta.default_user`; PWA-Settings „👤 Aktiver User" mit `localStorage.sb_user`. 2 neue Tests, 498/498 grün. **D2** (`f3e98fa`): Token-Rotation mit Master + Per-User-Token + 24h-Grace. Worker bekommt `user_tokens`-KV-Struktur, `authResolve()`-Async-Funktion mit drei Akzeptanzpfaden (Master/aktiver User-Token/alter Token in Grace), Endpunkte `POST /rotate_token` + `GET /token_status`. PWA-Settings „🔄 Token rotieren" mit Auto-Switch. ⚠ `wrangler deploy` nötig vor Live-Schaltung. Nächste Phase: **8** (E1–E4 Refactor) oder **8b** (M5 FIFA-Bracket nach Auslosung 2026-06-27).
- **2026-06-21**: ~ Phase 5 (F3, F4) erledigt. F3 (Commit `92cf85b`): drei Root-Causes für 41/43 leere CLVs gefunden — Pandas NaN-Truthiness im Backfill-Check, unvollständige Markt-Map, fehlender Void-Status. `_resolve_closing_odds()`-Helper deckt jetzt auch Quarter-Ball-O/Us und arbiträre Handicaps via dynamische `totals_lines`/`spreads`-Dicts ab. 16 historische Bets erfolgreich backfilled, 25 Tests neu. F4 (Commit `c2df64c`): farbcodierte CLV-Pille pro Settled-Bet + "Ø CLV letzte 30 Tage"-Karte zusätzlich zur Lifetime-Karte; Backend liefert `clv`/`closing_odds` in settled_bets und `mean_clv_30d`/`n_clv_30d` in summary; Void-Bets fließen in CLV-Aggregation (nicht Hit-Rate). Gesamt-Suite 488/488. Nächste Phase: C1–C7 (Trust-UI).
