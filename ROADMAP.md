# SportsBrain — ROADMAP

> **Lebende Quelle der Wahrheit** für alle Audit-Befunde, Entscheidungen und geplanten Arbeiten.
> Aktualisiert: 2026-06-22

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

### D6. + NEU ✅ Invite-Link + Self-Onboarding
- **Was**: Friction-freier Onboarding-Pfad für neue User. Du erzeugst in Settings einen Invite-Link (`POST /invite` mit Master-Token), schickst ihn deinem Freund. Empfänger öffnet Link → PWA liest `?invite=…` aus URL → Onboarding zeigt Username-Step (3–20 Zeichen, a-z/0-9/_-) → `POST /register {invite, user}` legt User-Slot im Worker mit selbst-gewähltem Namen an, gibt Per-User-Token zurück. Token + Username werden in localStorage gespeichert. Restlicher Onboarding-Flow (Bankroll) läuft wie gehabt. Ab dann sind alle backend-seitig generierten Dateien nach dem User benannt (`ledger_{user}.csv`, `signals_{user}.json`, KV `pending_bets_{user}`).
- **Warum**: D5 lieferte die Multi-Tenant-Pipeline, aber Onboarding war 2-Schritt-Hürde (User muss Token + Username manuell tippen). D6 macht es zum „Link senden, fertig".
- **Architektur**:
  - **Worker**: KV `invites` → `{[invite_token]: {created_at, note, used_by, used_at}}`. `/invite` (nur Master), `/register` (no-auth, validiert Invite). Invite ist einmalig — nach Konsum als `used_by` markiert.
  - **PWA**: Self-executing IIFE liest `?invite=` aus URL, speichert in `localStorage.sb_invite_pending`, räumt URL via `history.replaceState`. `_HAS_INVITE` Flag steuert ob Username-Step in `ONB_STEPS` eingefügt wird. `_onbRegisterUserIfNeeded()` ruft `/register` async; bei Fehler bleibt User auf dem Step. `_createInvite()` in Settings für Admin.
  - **Sicherheit**: Username-Server-side sanitisiert (`a-z0-9_-`, ≥3 chars, ≤32). Reservierter Username blockiert (`philip`). `/register` ohne Auth, aber braucht gültiges Invite — kein offenes Spam-Risiko.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 · 🟢
- **Dateien**: `cloudflare/worker.js` (readInvites/writeInvites, /invite, /register), `docs/index.html` (URL-Parse IIFE, `ONB_STEPS` mit Username-Step, `_onbRegisterUserIfNeeded`, `_createInvite`, Settings-Row, Onboarding-Force-Show bei pending invite)
- **Abhängigkeiten**: D5 ✅
- **Verifikation**: (a) Master-Settings → „Freund einladen" → Link kopiert. (b) Inkognito-Browser öffnet Link → `?invite=…` aus URL weg, Onboarding zeigt Username-Step. (c) Username eingeben → User-Slot existiert in Worker-KV `user_tokens`. (d) Re-Use desselben Invite-Links → 400 „invite already used". (e) Bestehender Username → 400 „username taken".
- **Status (2026-06-22)**: Erledigt. Worker-Deploy `f0f84701-9620-4270-97f3-cbb4ea3ef713`. 503/503 Tests grün.

### D5. + NEU ✅ Multi-User v2 — Ledger-Split + Per-User-Routing
- **Was**: Volle End-to-End-Multi-User-Pipeline aufbauend auf D3-Foundation (D3 lieferte nur Bankroll-Snapshot-Slot pro User).
  - **Ledger-Split**: `results/ledger.csv` → `results/ledger_{user}.csv` mit Auto-Migration (analog D3-Snapshot-Pattern via `_resolve_ledger_path`).
  - **Per-User signals.json**: `docs/data/signals_{user}.json`. Default-User schreibt zusätzlich `signals.json` (Backward-Compat). Helper `write_signals_json_all_users()` loop über alle bekannten User.
  - **Worker-Routing**: KV-Keys `signals_json_{user}` + `pending_bets_{user}`; Default-User mappt auf Legacy-Keys ohne Suffix. `authResolve()` liefert resolved user; Master-Token kann via `?user=` jeden Slot ansprechen.
  - **Scan-Pipeline**: `scripts/daily_scan.py` + `scripts/tennis_scan.py` rufen `write_signals_json_all_users()` — einmal scoren, pro User filtern (Architektur-Entscheidung).
  - **Settle/Consume**: `scripts/settle_bets.py` + `scripts/consume_pending_bets.py` + `scripts/post_match_update.py` loopen über `list_known_users()` und schreiben per-user-Ledger via `?user=`-Query gegen Worker.
- **Warum**: D3 hat nur Foundation gelegt; Freunde-Onboarding braucht echte Multi-Tenant-Trennung (Ledger, Bankroll, Pending-Queue, Signal-Feed). Mit D5 sieht alice ihren eigenen Ledger, ihre eigene Bankroll, ihre eigenen Wetten — komplett isoliert von philip.
- **Architektur-Entscheidungen** (explizit bestätigt vor Implementation):
  - **Routing**: Token→User-Mapping im KV (`user_tokens` → `authResolve`) — Token == Identität, kein Spoofing-Risiko.
  - **User-Anlage**: Implizit beim ersten `POST /rotate_token {user:'alice'}` mit Master-Token → Token + Default-Bankroll-Slot (leerer Ledger entsteht beim ersten append).
  - **Scan-Modus**: Einmal scoren, pro User filtern — DC/LGBM/Stacker + Quoten sind weltweite Wahrheit; nur Bankroll/Stake/Ledger pro User.
  - **Migration**: Auto-Rename `ledger.csv → ledger_philip.csv` beim ersten `_resolve_ledger_path()`-Call.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡 (Migration-Bug = Datenverlust → Tests Pflicht)
- **Dateien**: `src/config.py` (`ledger_path_for`), `src/betting/ledger.py` (`_resolve_ledger_path`, `user`-Parameter in `append_bets/settle_from_results/count_open_bets/ledger_summary/_live_bankroll`), `src/notifications/web_dashboard.py` (`write_signals_json(user=...)`, `list_known_users()`, `write_signals_json_all_users()`, `_get_*` mit `ledger_path`-Override), `cloudflare/worker.js` (`_pendingKey/_signalsKey/_sanitizeUser`, `/signals.json` GET + `/signals` POST + `/pending_bets` routen per User, `?user=` für Master), `scripts/daily_scan.py`, `scripts/tennis_scan.py`, `scripts/post_match_update.py`, `scripts/consume_pending_bets.py`, `scripts/settle_bets.py` (per-user-Loops), `scripts/match_reminder.py`/`weekly_recap.py`/`live_score_push.py`/`generate_session_report.py`/`drift_monitor.py` (hardcoded Pfade entfernt → `ledger_path_for(DEFAULT_USER)`), `tests/betting/test_ledger_multiuser.py` (5 neue Tests: Migration, Isolation, Default-User-Gating, explizite Pfade, ledger_summary-Routing).
- **Status (2026-06-22)**: Erledigt. Worker-Deploy `2e3b3888-6731-43ae-8b39-dc39ee2956b9`. 503/503 Tests grün (+5 Multi-User-Tests). **Migration läuft transparent** — bestehender Single-User-Pfad ist nach Migration `ledger_philip.csv`; nichts in der PWA muss umgestellt werden. **Rollback**: `wrangler kv key delete user_tokens` (Worker) + Restore `ledger_philip.csv → ledger.csv` (lokal) + Revert-Commits.

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
- **Status**: ✅ Erledigt. 3/3 Tests grün.

### E2. `docs/index.html` aufteilen (3 875 → ~5 Files)
- **Was**: `index.html` (Markup), `css/app.css`, `js/app.js`, `js/views.js`, `js/bets.js`.
- **Warum**: Wartbarkeit, Caching, Refactor-Basis.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡
- **Dateien**: `docs/index.html` + 4 neue
- **Abhängigkeiten**: E1
- **Verifikation**: PWA visuell identisch; Smoke-Test grün.
- **Status**: ✅ Erledigt. `index.html` 370 Z., `css/app.css` 639 Z., `js/app.js` 626 Z., `js/views.js` 2450 Z., `js/bets.js` 933 Z. Smoke-Test grün.

### E3. `src/scanner/daily_scan.py` splitten (1 337 → <500/Datei)
- **Was**: `prep.py`, `scoring.py`, `output.py`, `daily_scan.py` (Orchestrator).
- **Warum**: 500-Zeilen-Regel; Test-Anker.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: `src/scanner/`, Test-Imports
- **Verifikation**: `pytest`, `daily_scan --mock` Diff = identisch.
- **Status (2026-06-22)**: ✅ Erledigt. `prep.py` 185 Z., `scoring.py` 627 Z. (per-Match-Loop ist 508 Z., physisch nicht weiter teilbar), `output.py` 317 Z., `daily_scan.py` 309 Z. Externe Imports angepasst: `_confederation_min_edge`/`_count_model_agreement` → `src.scanner.scoring`, `_load_latest_dc_params` → `src.scanner.prep`. 523/523 Tests grün. Commit `3cb5d2f`.

### E4. `src/data/squad_availability.py` splitten (1 025 Z.)
- **Was**: `squad_transfermarkt.py`, `squad_wikipedia.py`, `squad_covers.py`, `squad_merger.py`.
- **Warum**: Klare Verantwortlichkeiten.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡
- **Dateien**: `src/data/`
- **Status (2026-06-22)**: ✅ Erledigt. 5 Sub-Module: `squad_models.py` (Types + Cache-Helpers), `squad_covers.py`, `squad_wikipedia.py`, `squad_transfermarkt.py`, `squad_merger.py`. `squad_availability.py` → Backward-Compat-Shim. Monkeypatch-Pfade in `test_suspensions.py` + `test_squad_wikipedia.py` auf korrekte Sub-Module aktualisiert. 523/523 Tests grün. Commit `b4d5562`.

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

### F5. + NEU Live-Loops raus aus GitHub Actions → Cloudflare Worker Cron (P1)
- **Was**: Hochfrequente Loops (`live_score_push` alle 2 Min, `consume_pending_bets` alle 2 Min, `cloud_healer` alle 30 Min) wandern aus GH Actions in einen Cloudflare Worker mit `crons`-Trigger. Worker-Cron ist verlässlich (echtes 2-min-Raster), kein freies Skippen wie bei GH-Actions-Cron. Worker ruft die Python-Logik nicht selbst auf — er triggert die Endpunkte (TheOddsAPI für Live-Scores direkt, Settle/Push via existierender Worker-KV-Routen) bzw. löst bei Bedarf einen GH-Workflow per `repository_dispatch` aus, wenn schwerer Python-Code laufen muss. Architektur-Skizze:
  - **A. Live-Scores**: Worker pollt TheOddsAPI `/scores` direkt alle 2 Min, schreibt Diff in KV `live_scores`, PWA pollt KV. Kein GH-Run mehr nötig.
  - **B. Push-Notifications**: VAPID-Send direkt aus Worker (Web-Push-Lib läuft im Worker-Edge-Runtime). Subscriptions liegen schon in KV `push_subs`.
  - **C. Consume Pending Bets**: Worker kann Bets aus `pending_bets_{user}` direkt zum Ledger nicht schreiben (Python-only) — stattdessen `repository_dispatch` an GH Actions, aber nur wenn KV nicht leer (Throttle = 0 Calls bei 0 pendings statt 720 leerer Runs/Tag).
  - **D. Cloud-Healer**: Worker liest `health.json` aus KV, triggert nur bei `status != ok` `workflow_dispatch` der betroffenen Workflows.
- **Warum**: Aktuelles Pattern produziert 14× cloud-healer-Retry-Commits + Commit-Flut + Stale-Banner pro Tag. Root-Cause ist GH-Actions-Cron-Unzuverlässigkeit (`*/30 * * * *` läuft real eher 1–2×/h unter Last; `*/2 * * * *` ist GH-offiziell „best effort"). Cloudflare Worker Cron hat dagegen ein hartes Raster und ist im Free-Tier ausreichend (100k Calls/Tag).
- **Impact**: 🟢 — Stale-Banner verschwindet, Commit-Volumen sinkt ~70%, schnellere Push-Latenz, weniger CI-Minuten verbrannt.
- **Aufwand**: 🔴 (8–14 h: Worker-Routen + KV-Schema-Migration für `live_scores` + VAPID im Worker + repository_dispatch-Flow + Rollback-Plan; jeder Teilbereich für sich testbar)
- **Risiko**: 🟡 — Worker-Bug killt Live-Scores für alle User; Migration in 4 Stufen (A→D) mit Schattenbetrieb gegen GH-Cron, dann Cutover, dann GH-Workflows deaktivieren.
- **Priorität**: **P1** — heutige Diagnose zeigte 3.2h-Stale-Daten + 14 Retry-Commits/48h; Aktivierung hilft jetzt schon, nicht erst nach WM.
- **Dateien**: `cloudflare/worker.js` (neue Cron-Handler `scheduled()`, KV `live_scores`/Push-Send/Healer-Trigger), `cloudflare/wrangler.toml` (`[triggers] crons`), `.github/workflows/live_score_push.yml`/`consume_pending_bets.yml`/`cloud_healer.yml` (Stufenweise auf `workflow_dispatch`-only umstellen, später entfernen), `docs/js/app.js` (Live-Score-Poll-URL ggf. anpassen), `src/notifications/web_push.py` (Server-Side-Send bleibt Fallback)
- **Abhängigkeiten**: B2 (Worker-Allowlist) ✅, D5 (Per-User-KV) ✅
- **Verifikation**: (a) Cloudflare-Logs zeigen Worker-Cron-Trigger im 2-min-Raster konstant. (b) `health.json` zeigt `live_score_push` älter als 5 Min nie. (c) Stale-Banner triggert nicht mehr ohne echten Failure. (d) GH-Actions-Minuten sinken nach Cutover um ≥70%. (e) Push-Latenz beim Test-Send < 30s (vorher: bis 5 Min wegen GH-Queue).

### F6. + NEU Cloud-Healer No-Commit-Mode (P2)
- **Was**: `cloud_healer.yml` committet aktuell jeden Retry-Log-Eintrag in `results/auto_heal_cloud.log` (14 Commits in 48h). Stattdessen: Healer schreibt Log nur in den Workflow-Run (visible in GH-UI), nicht ins Repo. Optional: aggregiert Log alle 24h einmal als ein Commit. Eliminiert die größte Single-Source der Commit-Flut.
- **Warum**: Jeder Retry produziert einen sichtbaren Bot-Commit der wie ein Failure wirkt, obwohl er nur „Selbstheilung lief" bedeutet. Verzerrt die History und triggert Merge-Konflikte mit echten Scans.
- **Impact**: 🟢 — sauberere Git-History, weniger Merge-Konflikte (siehe L5).
- **Aufwand**: 🟢 (~30 min: `git add` + `git commit` in `cloud_healer.yml` entfernen, dafür `actions/upload-artifact` für Log-File)
- **Risiko**: 🟢 — reine Workflow-Änderung, keine Logik
- **Priorität**: **P2** — entlastet, wird durch F5 evtl. obsolet (wenn Healer im Worker läuft, gibt es gar kein Repo-Commit mehr)
- **Dateien**: `.github/workflows/cloud_healer.yml` (Commit-Step entfernen, `actions/upload-artifact@v4` hinzufügen)
- **Abhängigkeiten**: keine; sofort umsetzbar
- **Verifikation**: Nach Healer-Run kein neuer `auto: cloud-healer retry`-Commit; Log als Artifact am Workflow-Run anhängbar.

### F7. + NEU tennis_scan überschreibt football-Schedule (P2)
- **Was**: `scripts/tennis_scan.py` ruft `write_signals_json_all_users(schedule=schedule)` mit einer **tennis-only** Schedule (außerhalb `--mock` ist es eine leere Liste). `write_signals_json::schedule_data = schedule` (wenn `schedule is not None`) überschreibt damit den gesamten Football-Schedule auf `[]`. Heute harmlos weil der nächste `prematch_scan` ihn wiederherstellt, aber latente Race-Condition: läuft tennis_scan kurz vor PWA-Refresh, sieht der User 0 Football-Spiele bis zum nächsten Football-Scan. Fix: tennis_scan übergibt nur den **eigenen Tennis-Anteil** des Schedules und mergt mit Football-Schedule aus `existing` (analog wie `football_data` und `tennis_data` schon getrennt gemergt werden); oder `schedule`-Parameter wird auf `partial_schedule_sport='tennis'` umgestellt.
- **Warum**: Konsistenz mit dem vorhandenen Sport-Merge-Pattern für `football`/`tennis`-Signale. Vermeidet User-sichtbare 0-Spiele-Lücken zwischen Scans.
- **Impact**: 🟡 — kleine UX-Verbesserung, eliminiert eine Klasse von „warum verschwinden Spiele plötzlich"-Bugs.
- **Aufwand**: 🟢 (~1 h: in `web_dashboard.py::write_signals_json` Schedule pro Sport mergen analog `football_data`/`tennis_data`; Test + Smoke)
- **Risiko**: 🟢
- **Priorität**: P2 — kein akutes Problem heute (PWA bekommt Football-Schedule beim nächsten prematch_scan zurück), aber Hygiene
- **Dateien**: `src/notifications/web_dashboard.py` (`write_signals_json` — Schedule-Merge per Sport), `scripts/tennis_scan.py` (`schedule`-Übergabe als tennis-only markieren), `tests/notifications/test_signals_json_schedule_merge.py` (neu)
- **Verifikation**: Smoke: erst football-scan mit 16 Schedule, dann tennis-scan ohne Tennis-Spiele → resultierende `signals.json` hat weiterhin 16 Football-Schedule-Einträge.

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

### L4. + NEU ✅ Stake-System v2 — Odds-Bucket-Cap + Korrelations-Adjustment
- **Was**: Zwei strukturelle Stake-Bugs in einer Iteration behoben.
  - **(a) Odds-blinde Sizing**: `dynamic_stake_eur(ev, conf, br)` skalierte nur über EV → potenziell €20 Stake auf 5.5er Scorer-Quote (20% BR-Risk auf ~22% Trefferwahrscheinlichkeit). Fix: neuer Pflicht-Cap pro Quoten-Bucket (`ODDS_BUCKET_CAPS`): ≤2.0→100%, 2-3→75%, 3-5→55%, >5→35% des Tier-MAX. Implementiert in `src/betting/kelly.py::odds_cap_factor`, in `dynamic_stake_eur` via neuem optionalem `decimal_odds`-Parameter.
  - **(b) Korrelations-blinde Stake**: Signale auf demselben Match wurden je Markt unabhängig dimensioniert. Beispiel CZE–MEX: ah-0.5_away (Mexico) + scorer Hložek (Czech) — diese widersprechen sich, blieben aber beide voll dimensioniert. Beispiel ZAF–KOR: away + o/u3.0_over — positiv korreliert (Tor-Quelle gleiche Seite), doppeltes Risiko. Fix: neues Modul `src/betting/correlation.py::apply_correlation_adjustments` mit:
    - Klassifizierung pro Signal („home"/„away"/„neutral", Scorer via `BetSignal.player_team` aus goalscorer.py).
    - Neg-Korr-Detektor: Underdog-Seite × `NEG_CORR_DISCOUNT = 0.50` und mit `stake_reason` markiert (Leg bleibt erhalten, wird NICHT gedroppt — User-Vorgabe).
    - Pos-Korr-Detektor: Sieg-Side + Over (Modell-p_over > 0.55) bzw. BTTS-Yes + Over → beide × `POS_CORR_DISCOUNT = 0.70`.
    - Match-Exposure-Cap: Σ stake pro `match_id` ≤ `tier_hi × MAX_MATCH_EXPOSURE_MULT (1.5)`, sonst proportional skaliert.
  - **Kennzeichnung verpflichtend**: neues `BetSignal.stake_reason`-Feld → Ledger-CSV-Spalte (Migration in `_load()`), `signals.json.correlation_note`, oranger „↓ Korr"-Badge in PWA (`docs/js/views.js`), CLI-Marker `⚠ KORR-↓ (alt→neu)` im daily_scan-Output und in `_confirm_bets()`.
- **Warum**: Heute Nacht (CZE–MEX, ZAF–KOR) zeigten beide Probleme exemplarisch. User-Triggered Audit der letzten 4 Wetten + Plan-Approval `valiant-munching-pie`.
- **Impact/Aufwand/Risiko**: 🟢 · 🟡 · 🟡 (verändert Stake-Logik live während WM)
- **Priorität**: P0 — direkt eingebaut nach Plan-Approval 2026-06-25 vor heutigen Scan-Runs
- **Dateien**: `src/config.py` (4 neue Konstanten), `src/betting/kelly.py` (odds_cap_factor + Signatur), `src/betting/value_detector.py` (`stake_reason`+`player_team`-Felder, Signatur-Update), `src/betting/correlation.py` (NEU), `src/betting/goalscorer.py` + `src/betting/tennis_detector.py` + `src/scanner/scoring.py` (Signatur-Updates), `src/betting/ledger.py` (stake_reason-Spalte+Migration), `src/notifications/web_dashboard.py` (correlation_note durchreichen), `docs/js/views.js` (Badge), `scripts/daily_scan.py` (Sammel-und-adjust-Flow), `tests/betting/test_kelly_odds_cap.py` (NEU, 17 Tests), `tests/betting/test_correlation_staking.py` (NEU, 6 Tests).
- **Verifikation**: 265/265 Tests grün (betting+scanner). Trockenlauf gegen heutigen Slate: Scorer-Quote 5.5 mit EV 15% bei BR 100 → max €7 statt €20.
- **Status (2026-06-25)**: Erledigt.

### L5. + NEU ✅ signals.json Conflict-Marker → Cloud-Wipe → PWA-Blackout (2026-06-26)
- **Was**: PWA zeigte 0 Spiele. Cloudflare-KV `signals_json_philip` lieferte `football:[], schedule:[], all_odds:{}, model_tips:{}`. Lokale `docs/data/signals.json` war gesund (11:02 UTC, 27 Football-Signals, 16 Schedule).
- **Root-Cause**: `docs/data/signals_philip.json` enthielt unaufgelöste Git-Konflikt-Marker (`<<<<<<< Updated upstream` / `>>>>>>> Stashed changes`) aus einem fehlgeschlagenen `git stash pop` nach Rebase. `scripts/_git_safe_push.sh::_git_clear_unmerged` hatte sie nicht entfernt (`git checkout --theirs` funktioniert bei stash-Konflikten ohne Stage-3-Eintrag nicht zuverlässig). Resolve-Commit `5e57333` committete die Datei mit Markern. Beim nächsten `tennis_scan` crashte `json.loads()` auf dieser Datei → `web_dashboard.py::write_signals_json` fing den Fehler still ab (`existing = {}`), schrieb leeren Payload und pushte ihn via `aggregate_health._push_to_cloud` an den Worker.
- **Fix** (Commits `24ca28b`/`47839bf`):
  - `web_dashboard.py::write_signals_json` wirft jetzt `RuntimeError` bei Konflikt-Markern oder ungültigem JSON in `existing` — kein stilles Daten-Wipe mehr.
  - `_git_safe_push.sh::_git_clear_unmerged` Kaskade: `--theirs` → `--ours` → `HEAD-Reset`; finaler Guard verhindert Staging einer Datei mit Markern.
  - `signals_philip.json` mit gültiger `signals.json` synchronisiert + Cloud manuell via `curl -X POST` wiederhergestellt.
- **Impact**: 🟢 — verhindert komplette Daten-Wipes der Cloud-KV durch Git-Hygiene-Fehler. Hat heute zur PWA-Blackout geführt; ohne Guards würde der Bug bei jedem Konflikt-Vorfall wieder auftreten.
- **Aufwand/Risiko**: 🟢 / 🟢 (3 Files geändert, Tests vorhanden)
- **Verifikation**: (a) Manueller Test: `signals_{user}.json` mit Konflikt-Markern → `write_signals_json` crasht statt zu schreiben. (b) `_git_clear_unmerged` mit künstlich gemarkerter Datei → File wird auf HEAD zurückgesetzt, nicht gestaged. (c) Cloud-KV nach Manual-Restore: 27 Football, 16 Schedule, PWA rendert wieder Spiele.
- **Folgeitem**: K5 („`_git_safe_push.sh` weiter härten") sollte aus der Veto-Liste entfernt werden — heutiger Vorfall zeigt, dass weiteres Härten doch gerechtfertigt war.
- **Status (2026-06-26)**: Erledigt.

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

## 🟦 M5. + NEU FIFA-2026-Bracket-Mapping (P1, vor KO-Phase 2026-07-04) 🔧 Gerüst fertig
- **Was**: Aktuelles Bracket-Vorschau in `_build_bracket_preview()` (build_wm_forecast.py) nutzt **Seeded-Pairing** (Seed 1 vs 32, etc.) als Approximation. Nach offizieller FIFA-KO-Auslosung am 2026-06-27 in Las Vegas wird das echte R32-Slot-Mapping verkündet (z.B. „1A vs 3C/D/E"). Hardcoded `FIFA_R32_SLOTS` einbauen, der die 32 Qualifizierten den 16 R32-Matches gemäß offiziellem Bracket zuweist.
- **Warum**: Aktuelle Approximation ist mathematisch fair, aber NICHT die echte Paarung. Für KO-Bet-Entscheidungen ab 2026-07-04 brauchen wir die korrekten Slots (z.B. Argentina nicht zwangsläufig gegen den schwächsten Drittplatzierten).
- **Impact**: 🟢 — KO-Bet-Genauigkeit pro Match, korrekte Pfad-Wahrscheinlichkeiten Champion
- **Aufwand**: 🟢 (1-2 h: Slot-Tabelle eintragen + Logik anpassen + Tests)
- **Risiko**: 🟢 — reiner Lookup, Backend-Output-Schema identisch
- **Priorität**: P1 — nach FIFA-Auslosung 2026-06-27, vor KO-Start 2026-07-04
- **Dateien**: `scripts/build_wm_forecast.py` (`_build_bracket_preview`, `_resolve_slot`, `FIFA_2026_R32_SLOTS`)
- **Abhängigkeiten**: L2 ✅ (Bracket-Infrastruktur), FIFA-Auslosung 2026-06-27
- **Verifikation**: 16 R32-Paarungen exakt nach FIFA-Bracket, manuell gegen FIFA.com-Bracket-PDF
- **Stand (2026-06-22)**: 🔧 Gerüst committed. `FIFA_2026_R32_SLOTS = [None × 16]` als Konstante. `_resolve_slot("1A"/"2C"/"3G")` löst Gruppen-Position auf besten Team-Dict auf. `_build_bracket_preview()` prüft `slots_ready` (alle 16 non-None) → offizieller Pfad; sonst → Seeded-Pairing-Fallback (mit note). `note`-Feld fehlt im Output wenn Slots gesetzt → Approximations-Banner verschwindet auto im Frontend. **Nach Auslosung 2026-06-27**: `FIFA_2026_R32_SLOTS` mit 16 Tupeln füllen, `None`s entfernen.

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
- **Status (2026-06-22)**: ✅ Erledigt. `send_scan_alert()` baut `?bet=MATCH:MARKET`-URL wenn 1 Top-Signal. SW nutzt bereits `notification.data.url`. Frontend: `_openBetModalForBetId()` + `?bet=`-Check in `_load()` mit `history.replaceState`-Cleanup. Commit `5c4808c`.

### H2. Legal/Impressum/DSGVO-Stub
- **Was**: Leeres `docs/legal.html` mit Sektionen + Footer-Link.
- **Warum**: Vor Public-Launch füllen.
- **Impact/Aufwand/Risiko**: ⚪ · 🟢 · 🟢
- **Dateien**: `docs/legal.html` (neu), `docs/index.html`
- **Status (2026-06-22)**: ✅ Erledigt. `docs/legal.html` mit Impressum/DSGVO-Stub (Platzhalter für Public-Launch). Footer-Link in `index.html` ergänzt. Smoke-Tests grün.

### H3. ✅ erledigt 2026-06-24 Wette aus Wetten-Tab canceln (Bet-Cancel-Flow)
- **Was**: Auf jeder offenen Bet-Karte im „Wetten"-Tab (Offen-Sektion) ein Cancel-Button (🗑️ / „Wette verwerfen"). Klick → Confirm-Dialog („Wette XY wirklich verwerfen? Eintrag wird aus Ledger entfernt, Stake an Bankroll zurückerstattet."). Bei Bestätigung: (a) PWA ruft `POST /cancel_bet {bet_id}` am Worker, (b) Worker entfernt Eintrag aus `pending_bets_{user}` KV und schreibt Audit-Eintrag, (c) `src/betting/ledger.py::cancel_bet(bet_id, user)` markiert Bet im Ledger als `status=cancelled` (NICHT löschen — Audit-Trail!), Stake wird **nicht** zur P&L gezählt, Bankroll-Snapshot bekommt Stake zurück. Frontend re-rendert Offen-Liste ohne die gecancelte Wette.
- **Warum**: Aktuell gibt es keinen Weg, eine bereits eingetragene Wette wieder loszuwerden, wenn man sich kurz nach Eintrag dagegen entscheidet (Lineup-News, Quoten-Drop, Bauchgefühl). Workaround heute: manueller CSV-Edit im Ledger + Snapshot-Korrektur — fehleranfällig und für Multi-User-Setup (D5/D6) gar nicht praktikabel. Cancel-Flow macht das Verwerfen zum 2-Klick-Vorgang und hält Audit-Trail sauber.
- **Impact/Aufwand/Risiko**: 🟢 (UX-Lücke schließt) · 🟡 (2-4 h: Ledger-Cancel-Helper + Worker-Route + Frontend-Button + Tests) · 🟡 (Datenintegrität: Cancel nach Settle muss blockiert sein; Race mit `consume_pending_bets`-Cron darf nicht zu Doppel-Eintrag führen — Idempotenz-Key auf `bet_id`)
- **Priorität**: P1 — dringend hochgestuft 2026-06-24; Workaround (CSV-Edit) ist für Multi-User (D5/D6) nicht praktikabel
- **Dateien**: `src/betting/ledger.py` (neuer `cancel_bet(bet_id, user)` → Status `cancelled`, Bankroll-Snapshot-Refund), `cloudflare/worker.js` (neuer `POST /cancel_bet`-Endpoint mit User-Auth, KV-Cleanup für `pending_bets_{user}`), `scripts/consume_pending_bets.py` (Cancel-Marker respektieren — kein Re-Insert), `docs/js/bets.js` (Cancel-Button-DOM + Confirm-Modal + Fetch + Re-Render), `docs/css/app.css` (Button-Styling), `tests/betting/test_ledger_cancel.py` (NEU: Cancel-Idempotenz, Cancel-nach-Settle blockiert, Multi-User-Isolation, Bankroll-Refund), `tests/scripts/test_cancel_bet_worker.py` (Worker-Route)
- **Abhängigkeiten**: D5 (Multi-User-Ledger) ✅, B2 (Worker-CORS) ✅
- **Verifikation**: (a) Cancel-Button erscheint nur bei `status=open`-Bets, nicht bei live/settled. (b) Cancel → Bet verschwindet aus Offen-Tab, taucht NICHT in Live/Settled auf, Bankroll +Stake. (c) Cancel-Aufruf auf bereits gecancelte Bet → 200 OK, kein Doppel-Refund (Idempotenz). (d) Cancel-Aufruf nach Settle → 409 Conflict. (e) `consume_pending_bets`-Cron 5 min später läuft → keine Re-Aufnahme der gecancelten Bet. (f) Multi-User: alice canceln darf nicht philip's Bet treffen.

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

### I4. ✅ erledigt 2026-06-24 Backtest-Inkonsistenz MAX_EV beheben
- **Was**: `src/backtest/walk_forward.py` bekommt `apply_live_filters=True` Default. EV>40%-Filter, Confederation-Filter, MAX_ACTIVE_BETS im Backtest aktiv.
- **Warum**: Backtests sind nur valide, wenn sie das Live-System nachbilden.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡
- **Dateien**: `src/backtest/walk_forward.py`, `tests/backtest/test_walk_forward_live_filters.py` (6 neue Tests)
- **Status**: `apply_live_filters=True` als Default in `run_event_backtest` + `run_all_backtests`. EV-Cap (MAX_EV=0.40) filtert Qualifier-Artefakte. Daily-Bet-Cap (MAX_ACTIVE_BETS=5, sortiert nach EV) verhindert Overtrading. Confederation-min_edge war bereits aktiv. 673/673 Tests grün.

### I5. ✅ erledigt 2026-06-21 PPDA scharfschalten (nach Backtest-Gate)
- **Was**: `PPDA_LIVE_ENABLED=True`, **falls** Backtest-Gate (G1) ROI-Improvement ≥ 0.5pp UND Brier-Improvement ≥ 0.001.
- **Impact/Aufwand/Risiko**: 🟡 · 🟢 · 🟡
- **Abhängigkeiten**: G1
- **Status**: Sprint-2-Backtest (2026-06-21) ergab Brier +0.0035 ✅, Markt-ROI +11.85pp ✅ → `PPDA_LIVE_ENABLED = True` gesetzt.

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

### I7. ✅ Monte Carlo Simulationen (Scoreline-Verteilung)
- **Was**: `src/analysis/monte_carlo.py` mit `scoreline_distribution(matrix)` — analytisch aus DC-Scoreline-Matrix: Top-3 wahrscheinlichste Scores + kumulative Tor-Verteilung (P(0)/P(1)/P(2)/P(3+)). Integration in PWA: Bracket-Karten und anstehende Gruppenspiele klickbar → Match-Detail-Modal.
- **Warum**: DC `predict_scoreline()` liefert die volle Matrix — analytische Ableitung ist exakter als N=10k Sampling und sofort. Basis für spätere Correct-Score-Märkte.
- **Impact**: 🟡 — visueller Mehrwert in PWA + Basis für spätere Correct-Score-Märkte
- **Aufwand**: 🟢
- **Risiko**: 🟢 — keine Modell-Änderung; nur Display
- **Dateien**: `src/analysis/monte_carlo.py` (neu), `scripts/build_wm_forecast.py` (`_match_scoreline`, `group_matches` in Output, Bracket-Matches mit `scoreline`), `docs/index.html` (`_renderGroupMatches`, `_openMatchDetail`, `_closeMatchDetail`, Match-Detail-Modal-DOM)
- **Status (2026-06-22)**: ✅ Erledigt in Commit `c847425`. 7 Unit-Tests, 510/510 Suite grün. `wm_forecast.json` enthält jetzt `group_matches` (72 Total, 60 pending mit Scoreline) + `scoreline` pro Bracket-Match. Forecast-Tab zeigt „Anstehende Gruppenspiele" mit Most-Likely-Score als Vorschau; Klick öffnet Modal mit Win-Wkt., Top-3 Scores, Tor-Balkendiagramm.

---

## 🟦 J. Saisonstart-Vorbereitung (P2, ab August 2026)

### J1. Basketball-Modul: Euroleague + BBL + NBA
- **Was**: `src/basketball/` mit Daten-Scraper (Basketball-Reference, Euroleague-API), Modellen (Pythagorean-Expectation, Pace-Adjusted Ratings, Basketball-Elo), Scanner, PWA-Tab.
- **Warum**: Saisons Sept-Okt 2026 (BBL ~26.09., Euroleague ~02.10., NBA ~21.10.).
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🔴 (neue Domain, Bankroll-Schutz wichtig)
- **Dateien**: `src/basketball/`, `docs/index.html`, `signals.json`-Schema
- **Abhängigkeiten**: I1
- **Verifikation**: Backtest auf historischen Saisons; Live erst nach 100+ Mock-Predictions.

### J2. ~ GEÄNDERT Tennis-Modul Full-Tour-Ausbau (P1, in Umsetzung)
- **Was**: Ganzjähriger Tennis-Betrieb für alle ATP/WTA-Turniere ab 250 aufwärts (Grand Slams + ATP/WTA 1000/500/250, ~80 Events/Jahr). Maximale Markt-Breite: Match Winner, Set AH ±1.5, First Set, O/U Sets, Total Games O/U. Backtest-First-Roll-out: pro Kategorie ROI-Gate vor Live-Schaltung.
- **Warum**: WM endet 2026-07-20, Bundesliga startet 2026-08-15 — dazwischen Lücke. US-Open-Serie startet 2026-08-10 als natürlicher Einstiegspunkt. Wimbledon-Backtest validiert WTA +8.5% ROI — Übertragbarkeit auf andere Slams/Surfaces ist Phase-B-Frage.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 (17h) · 🟡 (Tournament-Drift bei TheOddsAPI; pro Kategorie Shadow-Gate schützt Bankroll)
- **Priorität**: P1 (aus P2 hochgestuft, weil Saison-Gap nach WM)
- **Plan-Datei**: `~/.claude/plans/rippling-brewing-deer.md`
- **Sub-Phasen** (alle ✅ 2026-06-23/24):
  - **A. Tournament-Abstraktion** ✅ — `src/tennis/tournaments.py` Registry (49 Events: 8 Slams + 9 ATP Masters + 6 WTA 1000 + 13 ATP 500/250 + 10 WTA 500/250 + 2 Tour Finals), `src/tennis/discovery.py` TheOddsAPI /sports + 1h-Cache + Stale-Fallback + unknown_sport_key-Wrap, `src/config.py` `TENNIS_MIN_EDGE_BY_CATEGORY` + `TENNIS_CATEGORY_MODE`. 30 Tests grün. **Commit 6436db9.**
  - **B. Backtest-Erweiterung** ✅ — `tennis_odds.py::fetch_full_tour_odds()` lädt annual XLSX (alle ATP/WTA-Events ab 2019), `categorize_series()` mapped Series-Spalte auf Registry-Kategorien (incl. 'International' tour-aware), `tennis_backtest.py --full-tour --use-category-edge --j2-report` schreibt Per-Category-Verdict-Markdown mit ✅LIVE/⚠SHADOW/🚫BLACKLIST. Gate: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30 → LIVE; ROI≤-5% → BLACKLIST. 19 Tests neu. **Commit 99e4af1.**
  - **C. Markt-Erweiterung** ✅ — `src/tennis/sim.py`: closed-form `set_score_probs/total_sets_probs/p_total_sets_over` + Monte-Carlo `simulate_match` (Game-Level mit alternierendem Aufschlag, Tiebreak bei 6:6, tour-spezifische Hold-Baselines ATP 0.80/WTA 0.72), neue Detector-Funktionen `detect_total_sets/total_games/set_betting` mit relaxierten `min_prob`/`max_odds` für jeweilige Markt-Charakteristik. 28 Tests neu. **Commit 98b03af.**
  - **D. Scanner-Refactor** ✅ — `tennis_scan.py` komplett umgebaut zum Multi-Tournament-Dispatcher: Wimbledon-Hardcode raus, `discover_active_tournaments()`-Loop, `_parse_event_markets()` aggregiert h2h+spreads+set_winner+totals+set_betting in einem Pass, `detect_all_markets()` aggregiert alle 6 Markttypen, --all-live/--tournament-Flags, Live/Shadow-Modi pro Kategorie, WebSearch-Fallback-Hook (Stub mit TODO J2-D2). 14 neue Tests + Legacy-Tests umgeschrieben. **Commit ad34e15.**
  - **E. PWA-Anzeige** ✅ — `_signal_to_dict()` um tournament_meta erweitert, `write_signals_json()` um tennis_tournament_map-Parameter, `docs/js/views.js renderSport('tennis')` gruppiert nach Tournament mit Surface-Icons (🌱/🟧/🟦) + Kategorie-Pille + BO-Format. Football-Pfad unverändert. 4 Tests neu. **Commit 2105a1b.**
  - **F. CI/Cron + Roll-out** ✅ — `.github/workflows/tennis_scan.yml` von „Wimbledon nur im Juli" auf „ganzjährig 4×/Tag" + workflow_dispatch `all_live`-Input. `scripts/tennis_gate_review.py` parsed Backtest-Markdown und vergleicht gegen Live-Ledger (PROMOTE/DEMOTE/BLACKLIST/KEEP-Empfehlungen pro Kategorie). 11 Tests neu. **Commit 30a40e5.**
  - **G. Comprehensive Backtest** ✅ 2026-06-24 — `scripts/tennis_full_backtest.py` lädt 32 707 Match-Zeilen aus tennis-data.co.uk XLSX (2019-2025, ATP+WTA, alle Kategorien), baut walk-forward Elo direkt aus XLSX (Sackmann-Repos sind ab 2026-06 nicht mehr öffentlich), 7 772 Match-Winner-Value-Bets backtested. `src/tennis/calibration.py` (`invert_p_match_to_p_set`, `evaluate_set_markets`, `evaluate_game_markets`) liefert Brier+Hit% für O/U-Sets, Set-Betting und O/U-Games (synthetisch, keine historischen Quoten). Output `results/audits/tennis_full_backtest_2026-06-24.md` mit 5 Sektionen (Match-Winner ROI, Set-Markt Brier, Game-Markt Brier, Empfehlung TENNIS_CATEGORY_MODE, Surface-aware LIVE-Tabelle). **Verdicts**: 6 LIVE / 9 SHADOW / 5 BLACKLIST. Top-Edges: atp500 grass +18.6%, wta250 grass +16.0%, wta1000 clay +8.4%. Wimbledon ATP (grand_slam grass) -9.5% → BLACKLIST. `src/data/tennis_odds.py` Bugfix: duplikate `Surface`-Spalte in `_KEEP+keep_extra` + WTA-Series unter `Tier` statt `Series`. 13 neue Tests (calibration + wrapper), 648/648 Suite grün. **Commit pending.**
- **Dateien**: `src/tennis/` (NEU), `src/config.py`, `src/data/tennis_odds.py`, `src/betting/tennis_detector.py`, `scripts/tennis_scan.py`, `scripts/tennis_backtest.py`, `scripts/tennis_full_backtest.py`, `src/tennis/calibration.py`, `docs/js/views.js`, `.github/workflows/tennis_scan.yml`, `tests/tennis/`
- **Abhängigkeiten**: I3 (Multi-Liga-Retrain) optional

### J3. Brier-Ziel <0.52 reevaluieren
- **Was**: Mit Multi-Liga-Daten erneuter Retrain → Brier-Audit.
- **Impact/Aufwand/Risiko**: ⚪ · 🟡 · 🟢
- **Abhängigkeiten**: I3, J1 (mind. 1 Monat Daten)

### J2-H. ✅ erledigt 2026-06-24 Surface-aware TENNIS_CATEGORY_MODE (P1 — vor Wimbledon)
- **Was**: Aktueller `TENNIS_CATEGORY_MODE` ist nur per Kategorie. J2-G-Backtest zeigt: Surface-Edge dominiert — atp500 grass +18.6% vs hard -8.8%, wta250 grass +16% vs hard -2%. Einführung `TENNIS_CATEGORY_SURFACE_MODE: dict[(category, surface), str]` damit profitable Kombinationen freigeschaltet werden ohne pauschale Kategorie-Risiken. Lookup-Reihenfolge im Scanner: `(category, surface)` → `category` (fallback) → `"shadow"` (default).
- **Warum**: Aktuell blockiert. 6 profitable LIVE-Kombinationen aus J2-G fallen unter `shadow`-Default, weil ihre Kategorie negative Gesamt-ROI hat. Konkret für die kommenden 4 Wochen blockiert: **Bad Homburg WTA** (läuft jetzt, wta500 grass +8.1%), **Eastbourne ATP/WTA** (~22.06.-28.06.), **Newport ATP** (~14.-20.07., atp250 grass), **WTA Wimbledon-Vorbereitung** allgemein. Ab Mitte Juli folgt Hartplatzsaison ATP 250 (atp250 hard +5.3%, blockiert da `atp250=shadow`). **Ohne J2-H bleibt das Tennis-System bis weit ins Q3 hinein nahezu inaktiv**.
- **Impact/Aufwand/Risiko**: 🟢 (6+ zusätzliche LIVE-Kombinationen sofort verfügbar) · 🟢 (3-4 h: Map + Lookup + Tests, kein neuer Detector-Code) · 🟡 (kleinere Stichproben pro (cat,surface) — n≥50/30-Gate gilt weiter; Drift wenn Surface unbekannt → Fallback auf Kategorie-Default)
- **Priorität**: **P1** (hochgestuft 2026-06-24: Tennis-Saison läuft, Wimbledon in 5 Tagen, danach Sommer-Hardcourt — Verzögerung kostet messbar Edge)
- **Dateien**: `src/config.py` (neue Map `TENNIS_CATEGORY_SURFACE_MODE` + Helper `tennis_mode(category, surface)`), `scripts/tennis_scan.py::category_mode` (Signatur um `surface`-Parameter erweitern), `src/tennis/tournaments.py` (Doku — `Tournament.surface` ist Lookup-Key), `tests/tennis/test_category_surface_mode.py` (NEU)
- **Verifikation**: (a) Unit-Tests: Lookup mit/ohne Surface, Fallback-Kaskade, alle 20 J2-G-Verdicts als Fixture; (b) Scanner-Smoke mit `--all-live`-Bypass vs. mit neuer Map → erwarte Bad Homburg/Eastbourne aktiv; (c) Memory `tennis_module.md` mit finaler Map dokumentieren

### J2-I. + NEU Tennis-Scanner Fallback-Stack (Elo + WebSearch) ✅ erledigt 2026-06-24
- **Was**: Zwei-stufiger Fallback im Live-Scanner: (1) Match-History via tennis-data.co.uk XLSX wenn Sackmann-GitHub down ist (statt Default-Elo), (2) WebSearch-Tennis-Odds (2-way, analog Football-Pattern) + TheOddsAPI `/events`-Endpoint wenn `/odds` HTTP 422 zurückgibt und Match-Start ≤48h ist.
- **Warum**: Live-Scan vom 2026-06-24 lieferte 0 Signale weil (a) Sackmann-Repos seit Juni 2026 nicht mehr public → Default-Elo (alles 50/50) und (b) TheOddsAPI `/odds` für Wimbledon noch 422 (Markt öffnet üblicherweise 1-3 Tage vor Start). Beide Lücken zusammen blockieren das System die letzten 48h vor Turnier, genau wo der Edge am größten ist.
- **Impact/Aufwand/Risiko**: 🟢 (Signal-Resilienz für gesamte Tennis-Pipeline) · 🟢 (~2 h umgesetzt) · 🟡 (WebSearch-Quoten können stale/falsch sein → strikter Overround-Check 0.95-1.15)
- **Priorität**: P1 — Voraussetzung für Wimbledon 2026
- **Dateien**: `src/tennis/elo_source.py` (NEU), `scripts/tennis_scan.py` (`_websearch_tennis_fallback` echte 2-way-Impl, `_fetch_events_only` für 422-Fall, `_fetch_both_tours` nutzt neuen Loader), `tests/tennis/test_elo_source.py` + `tests/tennis/test_websearch_fallback.py` (14 neue Tests)
- **Verifikation**: 662/662 Suite grün; nächster Live-Scan-Run loggt `[elo] XLSX-Fallback` + ggf. `[websearch]` pro Match

### J2-J. ✅ erledigt 2026-06-24 Tennis Signal-Archive — I9-Integration für Tennis-Signale (P1, vor Wimbledon 2026-06-29)
- **Was**: `_archive_signals()` in `src/scanner/output.py` (I9) wird aktuell nur vom Fußball-Scanner aufgerufen. `scripts/tennis_scan.py` archiviert keine Signale. Tennis-Signale sollen in dieselbe `signal_history.jsonl` geschrieben werden — mit `sport="tennis"` + `tournament`-Feld, damit I8/I9-Feedback-Loop auch für Tennis funktioniert.
- **Warum**: I9 wurde ohne Tennis gebaut (Feedback-Memory: "I9/archive_signals() muss bei jedem neuen Sport/Saison von Tag 1 integriert sein"). Wimbledon startet 2026-06-29 — alle Signale gehen verloren ohne Archive.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 (~1 h, I9 bereits gebaut) · 🟢
- **Priorität**: P1 — vor Wimbledon 2026-06-29
- **Dateien**: `scripts/tennis_scan.py` (`_archive_signals()`-Call mit `sport="tennis"`), `src/scanner/output.py` (sport-Parameter ergänzen falls fehlt), `scripts/backfill_signal_outcomes.py` (Tennis-Outcome-Lookup via ESPN/TheOddsAPI)
- **Abhängigkeiten**: I9 (Tennis-Integration gleichzeitig mit I9 bauen)
- **Verifikation**: Nach Tennis-Scan: `signal_history.jsonl` enthält Zeilen mit `"sport": "tennis"`.

### J2-K. + NEU Tennis ML-Modell + Feature Engineering (P2, vor US-Open-Serie 2026-08-10)
- **Was**: LightGBM/HistGradientBoosting analog `src/models/lgbm_model.py` für Tennis. Features: H2H-Bilanz (surface-spezifisch), Form (letzte N Matches), Serve-Stats (Aces/First-Serve%, Break%), Surface-Elo-Delta, Tournament-Level, Travel-Fatigue. Walk-forward-Gate: ML-ROI ≥ Elo-Baseline auf 2024-2025-Holdout. Output: Ensemble aus Elo + ML analog Fußball DC + LGBM.
- **Warum**: Aktuell nur Elo → kein Ensemble, kein SHAP, keine H2H/Form-Nutzung. Tennis-Backtest zeigt ROI-Varianz zwischen Surfaces (atp500 grass +18.6% vs hard −8.8%) — ML kann diese Signale kombinieren und Surface-Kontext stärker gewichten als lineares Elo.
- **Impact/Aufwand/Risiko**: 🟢 · 🔴 · 🟡 (Overfitting-Risiko bei kleinen Stichproben; Walk-forward-Gate Pflicht)
- **Priorität**: P2 — nach WM, vor US-Open-Serie (ab 2026-08-10)
- **Dateien**: `src/tennis/features.py` (neu), `src/models/tennis_lgbm.py` (neu), `scripts/tennis_train.py` (neu), `tests/tennis/test_tennis_features.py` (neu)
- **Abhängigkeiten**: J2 ✅, J2-G ✅ (Datenbasis)
- **Verifikation**: Walk-forward-ROI Tennis-ML ≥ Elo-Baseline auf Holdout 2024-2025; Brier-Improvement ≥ 0.005.

### J2-L. + NEU Tennis Walk-forward Backtest + CLV-Tracking (P2)
- **Was**: `scripts/tennis_backtest.py` hat ROI-Auswertung aber kein Walk-forward (kein Time-Split, kein CLV-Tracking). Analog `src/backtest/walk_forward.py` + `src/backtest/clv_tracker.py` für Tennis: rollierendes Fenster (Train 3 Jahre, Val 6 Monate), Closing-Odds aus Tennis-XLSX-Spalten B365W/B365L, CLV-Delta pro Bet. CLV-Backfill für bestehende Tennis-Bets im Ledger.
- **Warum**: Aktuell kein formaler Zeitreihen-Backtest für Tennis — ROI-Werte aus J2-G sind Gesamt-Sample, nicht zeitstabil. CLV fehlt komplett für Tennis-Bets (nur Fußball hat CLV-Tracking via F3/F4).
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟢
- **Priorität**: P2 — parallel zu J2-K
- **Dateien**: `src/backtest/tennis_walk_forward.py` (neu), `scripts/tennis_clv_backtest.py` (neu), `src/betting/ledger.py` (Tennis-CLV-Backfill-Support)
- **Abhängigkeiten**: J2-G ✅ (Datenbasis), J2-K (Tennis-ML optionale Integration)
- **Verifikation**: Output mit Walk-forward-ROI + CLV-Histogramm pro Kategorie. Tennis-Bets im Ledger haben `clv`-Feld gefüllt.

### J2-M. + NEU Tennis Live-Statistiken als zweite Datenquelle (P3)
- **Was**: Tennis nutzt aktuell nur historische XLSX-Daten und Elo. Keine Live-Stats (Aces, First-Serve%, Break-Punkte). Mögliche Quellen: Tennis Abstract API (Match-Stats, kostenfrei), WTA/ATP Tour APIs, Sofascore Tennis-Endpunkt. Feature-Input für J2-K ML-Modell.
- **Warum**: Serve%-Stats sind stärkster Tennis-Predictor — Held-Service-Rate korreliert direkt mit Match-Ausgang. Surface-spezifische Serve-Stats würden J2-K-Features deutlich verbessern.
- **Impact/Aufwand/Risiko**: 🟡 · 🔴 · 🟡 (API-Stabilität, Rate-Limits)
- **Priorität**: P3 — erst nach J2-K (ML-Modell braucht die Features)
- **Dateien**: `src/data/tennis_stats.py` (neu), `src/tennis/features.py`
- **Abhängigkeiten**: J2-K

### I8. + NEU Market-Performance-Feedback-Loop (System lernt aus Losern und Winnern)
- **Was**: Per-Markt-ROI aus dem Ledger berechnen und in die Signalgenerierung zurückführen. Konkret: nach Settle werden für jeden Markt `(bets, won, lost, stake, pnl, roi)` aggregiert; Märkte mit ROI < −20 % bei ≥ 10 settled Bets bekommen automatisch einen erhöhten `min_edge` (+5 pp Penalty). Märkte mit ROI > +15 % bei ≥ 10 Bets können optional einen niedrigeren Threshold erhalten (−2 pp Bonus). Die resultierende Per-Markt-Edge-Map wird als `data/cache/market_performance.json` persistiert und beim nächsten Scan-Run geladen. Zwei Ausbaustufen: **A (regelbasiert, 1–2 h)** feste Schwellwerte; **B (adaptiv, 4–6 h)** gleitender ROI über Rolling-Window + Bayesianischer Shrinkage gegen globalen Prior.
- **Warum**: Aktuell ist `MIN_EDGE = 0.03` für alle Märkte identisch — das Ledger wird nur geöffnet, um Duplikate zu verhindern, nie um zu lernen. Stand 2026-06-24 (56 settled Bets): `o/u2.5_under` −39.8 % ROI bei 8 Bets, `o/u3.0_under` −44 % bei 3 Bets, `btts_yes` −23 % bei 3 Bets — das System schlägt trotzdem täglich Under- und BTTS-Wetten vor. Gleichzeitig lässt es profitable Märkte (`draw` +103.7 %, `o/u3.0_over` +192.5 %) unbelohnt durch. Kein Feedback-Loop = das System wiederholt Fehler strukturell.
- **Impact**: 🟢 — direkte Verbesserung der Signal-Qualität; weniger Noise bei nachweislich schlechten Märkten
- **Aufwand**: 🟡 (Variante A: 1–2 h; Variante B: 4–6 h)
- **Risiko**: 🟡 — kleine Stichproben (<10 Bets) können echten Edge fälschlicherweise sperren → Minimum-Bet-Gate ist Pflicht; Variante A vermeidet Overfitting durch harte Schwellen
- **Priorität**: P2 — nach WM, sobald 15+ Bets pro Markt akkumuliert (aktuell: Under/Draw/Over haben ausreichend Daten)
- **Dateien**: `data/cache/market_performance.json` (neu), `scripts/settle_bets.py` (Aggregat schreiben nach Settle), `src/scanner/scoring.py` (`_load_market_performance()` → `min_edge_override` pro Markt), `src/config.py` (`MARKET_PERF_MIN_BETS=10`, `MARKET_PERF_ROI_PENALTY_THRESHOLD=-0.20`, `MARKET_PERF_PENALTY_PP=0.05`)
- **Abhängigkeiten**: I3 (Retrain optional); funktioniert sofort mit vorhandenen 56 Bets
- **Verifikation**: (a) Nach Settle: `market_performance.json` enthält ROI pro Markt. (b) Nächster Scan: o/u2.5_under bekommt `min_edge = 0.08` statt 0.03 → deutlich weniger Under-Signale. (c) Smoke-Test: Variante A on/off — gleicher Score-Run, Anzahl Under-Signale fällt


### I9. ✅ erledigt 2026-06-24 Signal-Archive & Lernen aus allen generierten Signalen (nicht nur platzierten Wetten)
- **Was**: Jeden Scan-Run alle generierten Signale (inkl. nicht-platzierter) in ein persistentes Archiv schreiben (`data/cache/signal_history.jsonl` — eine JSON-Zeile pro Signal, append-only). Nach Match-Ende: Ergebnis via bestehenden Settle-Quellen (martj42/ESPN/TheOddsAPI) nachschlagen und rückwirkend pro Signal `outcome` (correct/wrong/void) eintragen. Aggregat: Pro Markt/Konföderation/Surface `n_signals, n_correct, accuracy, mean_ev, realized_roi` — auch für Signale die nie platziert wurden. Dieses Signal-Performance-Dict ersetzt/ergänzt den Ledger-basierten Feedback aus I8 und liefert 5–10× mehr Datenpunkte.
- **Warum**: I8 lernt nur aus platzierten Bets — typischerweise 30–50% aller Signale. Alle verworfenen Signale landen im Nichts, obwohl das Modell sie für Edge-positiv hielt: (a) Märkte mit wenigen platzierten Bets (z.B. `o/u3.0_over` 2 Bets) haben nach I8 keine statistische Basis — im Signal-Archiv wären es vielleicht 20+ Datenpunkte. (b) Systematische Modell-Schwächen (z.B. Under-Überschätzung) bleiben unsichtbar bis genug Geld verloren ist. (c) Kalibrierung kann nur verbessert werden wenn der Output auch gegen Outcomes gemessen wird, die nie gewettet wurden.
- **Impact**: 🟢 — reichste verfügbare Feedback-Quelle; Basis für Modell-Kalibrierung und Konföderation-Bias-Erkennung über I8 hinaus
- **Aufwand**: 🔴 (6–10 h: Signal-Archive-Writer in Scanner + Outcome-Backfill-Script + Aggregat-Berechnung + optionaler Dashboard-Tab)
- **Risiko**: 🟡 — Archive wächst schnell (→ JSONL-Rotation nach 90 Tagen nötig); Outcome-Lookup kann fehlschlagen wenn API-Daten nicht mehr verfügbar
- **Priorität**: P1 — dringend hochgestuft 2026-06-24; Signal-Archive ab sofort starten damit nach WM ausreichend Daten vorhanden sind
- **Dateien**: `data/cache/signal_history.jsonl` (neu, append-only), `src/scanner/output.py` (`_archive_signals()` — schreibt pro Signal: match_id, home, away, market, model_prob, fair_prob, ev_pct, odds, scan_ts), `scripts/backfill_signal_outcomes.py` (neu — liest Archive, holt Ergebnisse via martj42/ESPN, schreibt outcome zurück), `data/cache/signal_performance.json` (aggregiertes Dict, Input für I8-Feedback-Loop)
- **Abhängigkeiten**: I8 — I9 liefert reichere Datenbasis für dieselbe min_edge-Anpassungslogik; I9-Daten überschreiben I8-Daten wenn vorhanden
- **Verifikation**: (a) Nach Scan: `signal_history.jsonl` erhält neue Zeilen inkl. nicht-platzierter Signale. (b) Nach Backfill: Signale haben `outcome`-Feld. (c) `signal_performance.json` zeigt per-Markt-Accuracy mit 5–10× mehr Samples als Ledger.

### I10. ✅ erledigt 2026-06-24 Fußball Halbzeit-Simulation / Sub-Match-Level-Verteilung (P1 dringend)
- **Was**: Tennis hat `src/tennis/sim.py` (analytisch + Monte Carlo auf Set-/Game-Ebene). Fußball hat DC-Scoreline-Matrix (`predict_scoreline()`), aber keine Halbzeit-Simulation. Neues `src/analysis/halftime_sim.py`: aus DC-Lambdas Halbzeit-Tore unabhängig schätzen (λ_H1 ≈ λ_total × 0.45, λ_H2 ≈ × 0.55 — empirisch aus StatsBomb kalibrierbar), daraus H1-O/U-Verteilung, H2-O/U und H1-Correct-Score-Markt.
- **Warum**: H1-Märkte (`predict_half_goals_range()`) fehlt eine echte Kalibrierungsgrundlage für den Halbzeit-Split. Tennis hat diese analytische Tiefe auf Set-Ebene — Fußball sollte sie auf Halbzeit-Ebene haben. Korrekte H1-Lambdas ermöglichen außerdem einen neuen H1-Correct-Score-Markt (heute nicht vorhanden).
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡 (Kalibrierungsfaktor λ_H1/λ_total muss aus Daten kommen, nicht geraten)
- **Priorität**: P1 dringend — vor Bundesliga-Start 2026-08-15
- **Dateien**: `src/analysis/halftime_sim.py` (neu), `src/models/dixon_coles.py` (H1-Lambda-Kalibrierung via StatsBomb), `scripts/calibrate_halftime_split.py` (neu), `src/betting/value_detector.py` (H1-Correct-Score-Markt optional)
- **Verifikation**: H1-Split-Kalibrierung auf WM-2022-Daten: ΔBrier H1-O/U vs. aktuelle `predict_half_goals_range()` verbessert sich (Gate ≥ 0.001).

### I11. ✅ erledigt 2026-06-24 Fußball Liga/Wettbewerb Auto-Discovery via TheOddsAPI (P1 dringend)
- **Was**: Tennis hat `src/tennis/discovery.py` — ruft TheOddsAPI `/sports` auf, erkennt aktive Turniere automatisch, 1h-Cache, Fallback bei API-Down. Fußball hat eine statische Liga-Liste in `src/config.py`. Neues `src/data/football_discovery.py` analog `discovery.py`: aktive Soccer-Sports via `/sports?group=soccer` holen, gegen Whitelist filtern, unbekannte Sport-Keys loggen (analog `unknown_sport_key`).
- **Warum**: Nach WM kommen Bundesliga, Premier League, La Liga, Serie A gleichzeitig zurück. Statische Liste muss manuell gepflegt werden — Tennis-Auto-Discovery hat bereits bewiesen, dass sie robuster ist (J2-I live getestet). Verhindert verpasste Ligas bei Saison-Start.
- **Impact/Aufwand/Risiko**: 🟢 · 🟢 (~2 h, Pattern 1:1 von `src/tennis/discovery.py` übertragbar) · 🟢
- **Priorität**: P1 dringend — vor Bundesliga-Start 2026-08-15
- **Dateien**: `src/data/football_discovery.py` (neu, analog `src/tennis/discovery.py`), `src/config.py` (`FOOTBALL_LEAGUES_WHITELIST` statt hardcoded), `scripts/daily_scan.py` (Discovery-Call einbauen)
- **Verifikation**: `python3 -c "from src.data.football_discovery import discover_active_leagues; print(discover_active_leagues())"` gibt alle aktiven Soccer-Sport-Keys zurück.

### I12. ✅ erledigt 2026-06-24 Fußball Umwelt- und Untergrund-Faktoren (P1 dringend)
- **Was**: Tennis hat Surface-aware Elo (hard/clay/grass/overall getrennte Ratings). Fußball hat keinen Umwelt-Kontext. Drei Faktoren analog Tennis-Surface:
  1. **Höhenlage** (>2000m): Bogotá/Quito/Mexico City — Heim-Vorteil signifikant stärker, weniger Tore, Auswärts-Ermüdung. DC-Lambda-Adjustment analog `host_boost` (I6). Statische `ALTITUDE_BOOST_MAP`.
  2. **Kunstrasen-Penalty**: Teams auf Kunstrasen (bestimmte Ligen/Stadien). DC-Lambda-Malus für Auswärtsteams. Statische `ARTIFICIAL_TURF_STADIUMS`.
  3. **Wetter-Faktor** (P3, optional): Starkregen/Wind senkt xG ~8%. Datenquelle: Open-Meteo (kostenlos).
- **Warum**: Tennis-Surface ist der stärkste einzelne Predictor. Analoger Effekt im Fußball ist Höhe + Untergrund — aktuell komplett ignoriert. I6-Host-Boost deckt nur WM-Gastgeber, nicht allgemeine Höhenlage. Besonders relevant für CONMEBOL (Copa Libertadores) und Skandinavien/Russland-Kunstrasen-Ligen.
- **Impact/Aufwand/Risiko**: 🟡 · 🟡 · 🟡 (Kalibrierung zwingend; falscher Faktor verzerrt EV)
- **Priorität**: P1 dringend — besonders CONMEBOL-Märkte nach WM
- **Dateien**: `src/config.py` (`ALTITUDE_BOOST_MAP`, `ARTIFICIAL_TURF_STADIUMS`), `src/models/dixon_coles.py` (`altitude_boost`-Parameter analog `host_boost`, `turf_penalty`), `src/scanner/daily_scan.py` (Venue-Lookup für Höhe + Untergrund), `data/cache/venue_metadata.json` (neu, statisch), `scripts/calibrate_env_factors.py` (neu)
- **Verifikation**: Brier-Gate auf historischen CONMEBOL-Hochland-Matches (n ≥ 20): ΔBrier ≥ 0.001.

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
| ~~K5~~ | ~~`_git_safe_push.sh` weiter härten~~ | **VETO AUFGEHOBEN 2026-06-26** — siehe L5: Konflikt-Marker im Snapshot führten zu Cloud-Wipe. Weiteres Härten war nötig, jetzt umgesetzt in L5. |
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
| **7** | D1–D3 (Risiko & Multi-User Foundation) | ✅ erledigt 2026-06-21/22 | 2-3 h |
| **7b** | D4 (Onboarding-Bankroll) + D5 (Multi-User v2 End-to-End) + D6 (Invite-Link Self-Onboarding) | ✅ erledigt 2026-06-22 | 5-7 h |
| **8** | E1–E4 (Refactor) | Tag 8-12 | 6-8 h |
| **8b** | M5 (FIFA-Bracket-Mapping nach Auslosung) | ab 2026-06-27 (Auslosung), vor 2026-07-04 | 1-2 h |
| **9** | I6 (Home Advantage Gastgeber) | ✅ erledigt 2026-06-21 | 1-3 h |
| **9b** | I1–I5 (Post-WM Snapshot + Retrain) | 2026-07-20 bis 2026-07-31 | 8-12 h |
| **9d** | I8 (Market-Performance-Feedback-Loop) | nach WM-Ende, sobald 15+ Bets/Markt | 1-2 h (Var. A) |
| **9e** | **I9 (Signal-Archive — lernen aus allen Signalen) P1 dringend** | ab sofort starten (Archive braucht Vorlaufzeit) | 6-10 h |
| **9c** | I7 (Monte Carlo Sims) | ✅ erledigt 2026-06-22 | < 2 h |
| **10** | H1, H2 (Push-Deep-Link, Legal-Stub) ✅ · **H3 (Bet-Cancel-Flow) P1 dringend** | anytime ab Tag 10 | 2-4 h |
| **10b** | **F5 Live-Loops auf Cloudflare Worker Cron** — eliminiert Stale-Banner + Commit-Flut | **P1, sofort startbar** | 8-14 h |
| **10c** | **F6 Cloud-Healer No-Commit-Mode** — 70% Bot-Commits weg, evtl. obsolet durch F5 | parallel zu F5 oder vorher | 30 min |
| **10d** | **F7 tennis_scan Schedule-Race-Condition** — Sport-getrennter Schedule-Merge in write_signals_json | nach F5 | 1 h |
| **10e** | **L5 ✅ erledigt 2026-06-26** — signals.json Conflict-Marker Cloud-Wipe Hot-Fix (Guards in web_dashboard.py + _git_safe_push.sh) | erledigt | 1 h |
| **11** | J1 (Basketball) | ab 2026-08-15 | 30-50 h |
| **12** | J2 (Tennis-Ausbau) | nach Spec-Klärung | 8-12 h |
| **13** | J3, J4 (Brier, CONMEBOL Audit) | Q4 2026 | 2-3 h |
| **12b** | J2 Phase A-F ✅ (Tournament-Registry + Backtest + Markt-Erweiterung + Scanner + PWA + Roll-out) | erledigt 2026-06-23/24 | ~14 h |
| **12c** | J2-G ✅ Comprehensive Backtest (alle Märkte × Touren × Surfaces) + J2-I ✅ Fallback-Stack (XLSX-Elo + WebSearch + /events) | erledigt 2026-06-24 | ~6 h |
| **12e** | **J2-H Surface-aware MODE** — schaltet Bad Homburg / Eastbourne / Newport / Sommer-ATP250-hard live | **vor 2026-06-28** (Wimbledon-Vortag) | 3-4 h |
| **12f** | **J2-J Tennis Signal-Archive** — I9-Integration für Tennis-Signale (`sport="tennis"` in signal_history.jsonl) | **vor 2026-06-29** (Wimbledon) | ~1 h |
| **9f** | **I11 Fußball Liga Auto-Discovery** — statische config-Liste → TheOddsAPI-Discovery analog Tennis | **vor 2026-08-15** (Bundesliga-Start) | ~2 h |
| **9g** | **I10 Fußball Halbzeit-Simulation** — DC-H1-Lambda-Kalibrierung + halftime_sim.py analog tennis/sim.py | **nach WM, parallel zu I3** | 3-4 h |
| **9h** | **I12 Fußball Umwelt-Faktoren** — Höhenlage + Kunstrasen, DC-Adjustment analog host_boost/Tennis Surface | **nach I10/I11** | 3-4 h |
| **12g** | **J2-K Tennis ML-Modell** — LightGBM + H2H/Form/Serve-Features, Walk-forward-Gate vs. Elo-Baseline | **vor US-Open 2026-08-10** | 8-12 h |
| **12h** | **J2-L Tennis Walk-forward + CLV** — zeitstabiler Backtest + CLV-Backfill im Ledger für Tennis-Bets | **parallel zu J2-K** | 3-4 h |
| **13b** | **J2-M Tennis Live-Stats** — Tennis Abstract / Sofascore Tennis als zweite Datenquelle (Feature-Input J2-K) | **P3, nach J2-K** | 8-12 h |

---

## 📊 Statistik

- **Insgesamt**: 73 konkrete Items (+4 ggü. 2026-06-25: F5 Worker-Cron, F6 Healer-No-Commit, F7 Schedule-Race, L5 Conflict-Wipe-Hotfix)
- **P0**: 13 (sofort) — davon 13 ✅
- **P1**: 35 — davon 34 ✅ (neu: F5 offen, alle anderen P1 erledigt; M5 blockiert bis FIFA-Draw 2026-06-27)
- **P2**: 17 — davon 10 ✅ (E1, E2, E3, E4, H1, H2, I4, I5, I6, I7); offen: I1–I3, I8, J2-K, J2-L, J3, J4, F6, F7
- **P3**: 5 — J2-M (Tennis Live-Stats) + 4 weitere Q4 2026
- **Veto**: 10 (K5 aufgehoben 2026-06-26)

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
- **2026-06-22**: + D5 NEU (Multi-User v2 End-to-End) ✅ erledigt. D3-Foundation um echte Multi-Tenant-Trennung erweitert: Ledger-Split (`ledger_{user}.csv` mit Auto-Migration analog Snapshot-Pattern), per-user `signals_{user}.json` + `write_signals_json_all_users()`-Loop, Worker-Routing über `authResolve()` + KV-Keys `signals_json_{user}` & `pending_bets_{user}` (Default-User auf Legacy-Keys ohne Suffix für Backward-Compat), Master-Token mit `?user=`-Query, daily_scan/tennis_scan/post_match_update/consume_pending_bets/settle_bets loopen über `list_known_users()`. Architektur: einmal scoren, pro User filtern (Bankroll/Stake/Ledger). User-Onboarding implizit via `POST /rotate_token {user}` mit Master-Token. Worker-Deploy `2e3b3888`. 5 neue Tests in `test_ledger_multiuser.py`, 503/503 Suite grün. Nächste Phase: **8** (E1–E4 Refactor).
- **2026-06-24**: ✅ **H3 + I10 + I11 + I12 erledigt** (P1: 28 → 33 ✅ — alle P1-Items done). H3: `cancel_bet()` in ledger.py + Worker `POST /cancel_bet` + `GET/DELETE /cancel_requests` + consume_pending_bets.py-Integration + Cancel-Button in bets.js (Confirm-Dialog, sofortiger DOM-Remove). I10: `src/analysis/halftime_sim.py` (H1-Split=0.447 empirisch, `predict_halftime_ou()`, `predict_halftime_scoreline()`). I11: `src/data/football_discovery.py` analog tennis/discovery.py + `FOOTBALL_LEAGUES_WHITELIST` (20 Ligen) in config.py. I12: `ALTITUDE_BOOST_MAP` (16 Hochland-Teams, Bolivar 1.20/0.85, LigaQuito 1.15/0.90) + `ARTIFICIAL_TURF_STADIUMS` (14 Teams) + `TURF_AWAY_PENALTY=0.96` in config.py; `_lambdas()` in dixon_coles.py um `altitude_factors` + `turf_penalty` Parameter erweitert.
- **2026-06-24**: ✅ **I4 + I5 erledigt**. I4: `apply_live_filters=True` Default in `walk_forward.py` — EV-Cap (MAX_EV=0.40), Daily-Bet-Cap (MAX_ACTIVE_BETS=5 sortiert nach EV), Confederation-min_edge bereits aktiv. 6 neue Tests. I5: PPDA war bereits live seit 2026-06-21 (Sprint-2-Backtest Gate ✅), nur ROADMAP-Status nachgezogen. P2: 8 → 10 ✅.
- **2026-06-24**: ✅ **I10+I11+I12 vollständig verdrahtet** — altitude_factors/turf_penalty durch alle 11 predict_*-Funktionen; scoring.py liest Maps pro Match; _H1_FACTOR angleichen (0.43→0.447); Football Discovery Startup-Log; Worker Deploy 54e6b18e (H3 /cancel_bet live).
- **2026-06-24**: ✅ **J2-H + J2-J + I9 erledigt**. J2-H: `TENNIS_CATEGORY_SURFACE_MODE` live — atp500/grass, wta250/grass, wta1000/clay, wta500/grass jetzt im Live-Gate. J2-J + I9: `archive_signals()` bereits in beiden Scannern integriert (tennis_scan.py + daily_scan.py), Signal-History für Tennis + Football aktiv. P1-Count: 25 → 28 ✅.
- **2026-06-24**: ~ **J2-H Priorität P2 → P1 hochgestuft + Slot 12e in Umsetzungs-Reihenfolge eingetragen**. Begründung: Tennis-Saison läuft, Wimbledon-Hauptfeld startet Mo 29.06., danach Bad Homburg/Eastbourne/Newport/Sommer-Hardcourt-Saison ohne Pause. Mit aktuellem kategorie-only-MODE würde das Tennis-System bis weit ins Q3 inaktiv bleiben, weil 6 profitable Surface-Kombinationen (atp500 grass +18.6%, wta250 grass +16%, wta1000 clay +8.4%, wta500 grass +8.1%, atp250 clay/hard +3.8%/+5.3%) unter `shadow`-Default fallen. Deadline **2026-06-28** (Wimbledon-Vortag). Aufwand 3-4 h. Doku in J2-H-Item finalisiert (Lookup-Reihenfolge, Fallback-Kaskade, Test-Plan).
- **2026-06-24**: ~ **I9 + H3 Priorität P2 → P1 hochgestuft**. I9: Signal-Archive muss sofort starten damit nach WM ausreichend Daten vorhanden sind (~200–500 Signale brauchen 4 Wochen Vorlaufzeit). H3: Bet-Cancel-Flow ist im Multi-User-Setup (D5/D6) kein optionales Polish mehr — CSV-manuell-Edit ist für Freunde-User nicht praktikabel. P1-Zähler: 27 → 29.
- **2026-06-24**: + **I9 NEU** (Signal-Archive & Lernen aus allen generierten Signalen). Erweiterung zu I8: I8 lernt nur aus platzierten Bets (30–50% aller Signale). I9 archiviert ALLE generierten Signale in `signal_history.jsonl` (append-only), backfilliert Outcomes nach Match-Ende und aggregiert per-Markt-Accuracy/ROI aus 5–10× mehr Datenpunkten als das Ledger. Basis für Modell-Kalibrierung + Konföderation-Bias-Erkennung. P2, nach WM. Statistik: 60 → 61 Items.
- **2026-06-24**: + **I8 NEU** (Market-Performance-Feedback-Loop — System lernt aus Losern und Winnern). Root-Cause-Analyse: `MIN_EDGE=0.03` ist für alle Märkte fix; Ledger wird nie ausgewertet. Stand 56 Bets: `o/u2.5_under` −39.8% ROI, `o/u3.0_under` −44%, `btts_yes` −23% — das System schlägt diese Märkte trotzdem täglich vor. Lösung: Per-Markt-ROI aus Settle aggregieren → `market_performance.json` → per-Markt `min_edge_override` im Scanner. Variante A (regelbasiert) +5pp Penalty bei ROI <−20% nach ≥10 Bets. P2, nach WM. Statistik: 59 → 60 Items.
- **2026-06-24**: + **H3 NEU** (Bet-Cancel-Flow im Wetten-Tab). UX-Lücke: aktuell keine Möglichkeit, eine eingetragene Wette nach Umentscheidung zu verwerfen — nur manueller CSV-Edit, untauglich für Multi-User (D5/D6). H3 fügt `cancel_bet(bet_id, user)`-Helper im Ledger (`status=cancelled` statt Löschen → Audit-Trail), `POST /cancel_bet`-Worker-Route mit KV-Cleanup, Cancel-Button auf jeder Offen-Karte mit Confirm + Bankroll-Refund. Idempotenz über `bet_id`, Cancel-nach-Settle blockiert (409). P2 Polish. Statistik: 60 → 61 Items.
- **2026-06-24**: + **7 NEU — Strukturlücken Tennis + Fußball** aus Modul-Vergleich aufgenommen. **Tennis**: J2-J (Signal-Archive I9-Integration, P1 vor Wimbledon 29.06.), J2-K (Tennis ML-Modell LightGBM + H2H/Form/Serve-Features, P2 vor US-Open), J2-L (Tennis Walk-forward Backtest + CLV-Tracking, P2), J2-M (Tennis Live-Stats als zweite Datenquelle, P3). **Fußball**: I10 (Halbzeit-Simulation analog tennis/sim.py, P1 vor Bundesliga), I11 (Liga Auto-Discovery analog tennis/discovery.py, P1 vor Bundesliga), I12 (Umwelt-Faktoren Höhenlage + Kunstrasen analog Tennis Surface-Elo, P1 CONMEBOL). Statistik: 61 → 68 Items. P1: 29 → 33, P2: 13 → 15, P3: 4 → 5.
- **2026-06-24**: + **J2-I Tennis-Scanner Fallback-Stack ✅ erledigt** (nach J2-G). Live-Scan vom 06:34 UTC lieferte 0 Signale (Sackmann seit Juni down → Default-Elo; TheOddsAPI /odds liefert HTTP 422 für Wimbledon — Markt öffnet erst 1-3 Tage vor Start). Fix: **(a) Elo-Fallback** `src/tennis/elo_source.py` mit `load_match_history()` (Sackmann primary → tennis-data.co.uk XLSX fallback → "empty"-Tag für Logging). **(b) 2-way WebSearch-Fallback** für Tennis (analog Football, aber 2 Outcomes, Overround-Check 0.95-1.15) ersetzt den Stub in `_websearch_tennis_fallback`. **(c) 48h-/events-Pfad**: wenn `/odds` 422 zurückgibt, holt neuer `_fetch_events_only()` die `/events`-Liste; alle Matches mit `commence_time` ≤48h werden via WebSearch geparst (statt komplett skippen). 14 neue Tests, 662/662 Suite grün. Nächster Scan-Run loggt `[elo] XLSX-Fallback` bzw. `[websearch]`-Treffer pro Match.
- **2026-06-24**: + **J2-G Comprehensive Backtest ✅ erledigt** (nach Phase F). `scripts/tennis_full_backtest.py` (NEU) + `src/tennis/calibration.py` (NEU) erlauben Backtest über **alle Märkte × Touren × Surfaces × Kategorien**. Datenbasis tennis-data.co.uk XLSX 2019-2025 (32 707 Match-Zeilen, 27 335 Snapshot-Matches, 7 772 Match-Winner-Bets). **Bugfix tennis_odds.py**: `Surface`-Duplikat in `_KEEP+keep_extra` führte zu DataFrame-statt-Series-Crash (silent fail in try-Block); WTA-XLSX nutzt `Tier` statt `Series`-Spalte. **Sackmann-Repos sind ab 2026-06 nicht mehr öffentlich** → Elo direkt aus XLSX-Outcomes walk-forward. **Verdicts**: 6 LIVE (atp500 grass +18.6%, wta250 grass +16%, wta1000 clay +8.4% u.a.), 5 BLACKLIST (Wimbledon ATP -9.5%, m1000 ATP clay -9.4%, atp500 ATP hard -8.8%, tour_final WTA hard, wta1000 WTA hard). Set-Märkte Brier 65/127 kalibriert, Game-Märkte nur 3/21. Report: `results/audits/tennis_full_backtest_2026-06-24.md`. Memory `tennis_module.md` mit empfohlenem TENNIS_CATEGORY_MODE-Diff aktualisiert. Neue Sub-Phase **J2-H** (Surface-aware Mode) in Roadmap aufgenommen. 13 neue Tests, 648/648 Suite grün.
- **2026-06-24**: ~ J2 **Phase B-F ✅ erledigt** (in einer Sitzung nach Phase A). **B** (Backtest, Commit 99e4af1): `fetch_full_tour_odds()` + `categorize_series()` + `tennis_backtest.py --full-tour --use-category-edge --j2-report` mit Verdict-Tabelle. **C** (Märkte, Commit 98b03af): `src/tennis/sim.py` (closed-form Set-Distribution + Monte-Carlo Game-Total), neue Detector-Funktionen `detect_total_sets/games/set_betting`. **D** (Scanner, Commit ad34e15): `tennis_scan.py` Multi-Tournament-Dispatcher, Wimbledon-Hardcode entfernt, alle 6 Märkte aggregiert, --all-live/--tournament-Flags. **E** (PWA, Commit 2105a1b): tournament_meta-Pfad bis ins JSON + `renderSport('tennis')` mit Tournament-Gruppierung, Surface-Icons, Kategorie-Pille. **F** (CI/Roll-out): `tennis_scan.yml` ganzjährig 4×/Tag, `scripts/tennis_gate_review.py` (Live-vs-Backtest-Vergleich mit PROMOTE/DEMOTE/BLACKLIST-Empfehlungen). Suite 627/627 grün (+71 Tests Tennis gesamt seit Phase-A-Start). **Damit ist Tennis production-ready für alle ATP/WTA-Turniere ab 250 aufwärts ganzjährig** — nur grand_slam initial live, Rest shadow bis erster Backtest-Run mit `--full-tour --j2-report` Live-Daten liefert.
- **2026-06-23**: ~ J2 Phase A ✅ erledigt. Tennis-Modul von Wimbledon-Single-Tournament-Hardcode zur Tournament-Registry umgebaut. Neue Module: `src/tennis/tournaments.py` (49 Events: 8 Slams + 9 ATP Masters + 6 WTA 1000 + 13 ATP 500/250 + 10 WTA 500/250 + 2 Tour Finals; Dataclass + Lookup-Indizes), `src/tennis/discovery.py` (TheOddsAPI `/sports`-Pull mit 1h-Cache, Stale-Fallback bei API-Down, unknown_sport_key-Wrap für Drift). `src/config.py`: `TENNIS_MIN_EDGE_BY_CATEGORY` (grand_slam 5%, m1000 8%, wta1000 4%, atp500 10%, wta500 6%, atp250 12%, wta250 8%) und `TENNIS_CATEGORY_MODE` (alle außer grand_slam initial Shadow — wird durch Phase-B-Backtest entschieden). 30 neue Tests in `tests/tennis/`; 556/556 Suite grün. **Roadmap-Hochstufung J2: P2 → P1** (Saison-Gap WM-Ende → Bundesliga-Start). Nächste Phase: J2-B (Backtest-Erweiterung auf full-tour-Quoten, Per-Category-Gate-Verdicts).
- **2026-06-25**: + **L4 NEU ✅** (Stake-System v2 — Odds-Bucket-Cap + Korrelations-Adjustment). User-Audit der letzten 4 Wetten (CZE–MEX, ZAF–KOR) deckte zwei strukturelle Bugs auf: (a) Sizing skalierte nur über EV → €20 Stake auf 5.5er Quote möglich; (b) negative Korrelation (Mexico-AH + Hložek-Scorer) und positive Korrelation (Korea-Sieg + Over 3.0) wurden nicht erkannt. Fix: ODDS_BUCKET_CAPS (≤2.0→100% bis >5→35%), neues `correlation.py`-Modul mit Neg-Korr-Discount (Underdog-Leg ×0.50, markiert), Pos-Korr-Discount (beide Legs ×0.70) und Match-Exposure-Cap (Σ stake ≤ tier_hi×1.5). Kennzeichnung via `stake_reason`/`correlation_note`/orange „↓ Korr"-Badge in PWA + CLI `⚠ KORR-↓`. 265/265 Tests grün, 23 neue Tests. Statistik: 68 → 69 Items.
- **2026-06-26**: + **F5/F6/F7/L5 NEU** aus Vorfall-Analyse (PWA-Blackout + Stale-Daten + 14 Healer-Retry-Commits/48h). **F5** (P1): Live-Loops `live_score_push`/`consume_pending_bets`/`cloud_healer` raus aus GH Actions, rein in Cloudflare Worker Cron — GH-Cron-Unzuverlässigkeit ist Root-Cause für Stale-Banner + Commit-Flut. **F6** (P2): Cloud-Healer-Workflow soll Logs als Artifact statt als Commit speichern (70% Bot-Commits weg). **F7** (P2): `tennis_scan.py` übergibt `schedule=[]` und überschreibt damit Football-Schedule — Sport-getrennter Merge in `write_signals_json` nötig. **L5** (P1, ✅ erledigt heute): Hot-Fix für signals.json Conflict-Marker → Cloud-Wipe → PWA-Blackout. Guards in `web_dashboard.py` (`RuntimeError` statt stilles Wipe) + `_git_safe_push.sh` (Kaskade `--theirs`→`--ours`→`HEAD-reset` + Final-Guard gegen Markers-Staging). Commits `24ca28b`/`47839bf`. **K5 aus Veto-Liste entfernt** (`_git_safe_push.sh` Härtung war doch nötig). Statistik: 69 → 73 Items. P1: 33 → 35, P2: 15 → 17.
- **2026-06-22**: + D6 NEU ✅ (Invite-Link + Self-Onboarding). Admin generiert Invite via `POST /invite` (Master-Auth) → schickt Link `?invite=TOKEN` → Empfänger wählt eigenen Username im Onboarding → `POST /register {invite, user}` legt Worker-Slot mit gewähltem Namen an, alle nachgelagerten Dateien (`ledger_{user}.csv`, `signals_{user}.json`, KV `pending_bets_{user}`) verwenden diesen Namen. Invite-Tokens einmalig, KV `invites` mit used_by-Tracking. PWA-IIFE liest URL, räumt sie via replaceState. Worker-Deploy `f0f84701`. 503/503 Tests grün. **Damit ist „Link senden = Tool teilen" Realität.** Nächste Phase: **8** (E1–E4 Refactor) oder **9c** (I7 Monte Carlo).
