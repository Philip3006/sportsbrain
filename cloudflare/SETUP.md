# Cloudflare Worker Setup (einmalig, ~10 Min)

## Voraussetzungen
- Kostenloses Cloudflare-Konto: https://dash.cloudflare.com/sign-up
- Node.js installiert (für Wrangler CLI)

---

## Schritt 1 — Wrangler CLI installieren

```bash
npm install -g wrangler
wrangler login   # öffnet Browser-Login
```

---

## Schritt 2 — KV Namespace erstellen

```bash
cd cloudflare/
wrangler kv:namespace create SIGNALS
```

Ausgabe sieht so aus:
```
✅ Created KV namespace "SIGNALS" with id "abc123..."
```

KV-ID in wrangler.toml eintragen:
```toml
id = "abc123..."   # ← hier ersetzen
```

---

## Schritt 3 — API Token als Secret setzen

Wähle ein sicheres Passwort (z.B. mit `openssl rand -hex 32`):
```bash
wrangler secret put API_TOKEN
# → Passwort eingeben, Enter
```

Dasselbe Passwort in .env eintragen:
```
SIGNALS_API_TOKEN=dein_passwort_hier
```

---

## Schritt 4 — Worker deployen

```bash
wrangler deploy
```

Ausgabe:
```
✅ Deployed sportsbrain-signals
   https://sportsbrain-signals.DEIN_NAME.workers.dev
```

---

## Schritt 5 — URLs eintragen

### In `.env`:
```
SIGNALS_CLOUD_URL=https://sportsbrain-signals.DEIN_NAME.workers.dev/signals.json
SIGNALS_API_TOKEN=dein_passwort_hier
```

### In `docs/index.html` (Zeile ~416):
```javascript
const CLOUD_URL = 'https://sportsbrain-signals.DEIN_NAME.workers.dev/signals.json';
```

---

## Schritt 6 — GitHub Secrets eintragen

Auf GitHub → Repository → Settings → Secrets → Actions → New secret:

| Name | Wert |
|------|------|
| `SIGNALS_CLOUD_URL` | `https://sportsbrain-signals.DEIN_NAME.workers.dev/signals.json` |
| `SIGNALS_API_TOKEN` | dein Passwort |

---

## Schritt 7 — Testen

```bash
# Upload testen:
python3 -c "
from src.notifications.web_dashboard import upload_signals_to_cloud
ok = upload_signals_to_cloud()
print('OK' if ok else 'FEHLER — .env gesetzt?')
"

# Fetch testen (ergibt aktuelles signals.json):
curl https://sportsbrain-signals.DEIN_NAME.workers.dev/signals.json | python3 -m json.tool | head -5
```

---

## Wie es danach funktioniert

```
Lokaler Scan  →  write_signals_json()  →  HTTP POST → Cloudflare KV
                                                             ↓
PWA (Homescreen)  ←  GET alle 10 Min  ←  Cloudflare Worker
```

- Kein `git push` nötig für Daten-Updates
- PWA lädt sich alle 10 Minuten automatisch neu
- Auf iOS: von oben nach unten wischen = sofortige Aktualisierung (Pull-to-Refresh)
- GitHub Pages bleibt als Fallback wenn Worker nicht erreichbar

---

# Web Push Setup (Ersatz für Telegram, einmalig ~15 Min)

## Schritt 1 — VAPID-Keypair generieren

```bash
python3 scripts/gen_vapid_keys.py
```

Liefert zwei Strings: `VAPID_PUBLIC_KEY` (kurz, URL-safe base64) und `VAPID_PRIVATE_KEY` (mehrzeilige PEM).

## Schritt 2 — Public Key ins Frontend eintragen

In `docs/index.html` den Platzhalter ersetzen:

```js
const VAPID_PUBLIC_KEY = 'REPLACE_ME_WITH_VAPID_PUBLIC_KEY';
//                        ↑↑↑ durch den Output ersetzen
```

Committen + pushen — GitHub Pages aktualisiert sich automatisch.

## Schritt 3 — GitHub Secrets anlegen

Im Repo unter `Settings → Secrets and variables → Actions → New repository secret`:

| Name | Wert |
|---|---|
| `VAPID_PRIVATE_KEY` | Komplette PEM-Block, inkl. `-----BEGIN/END PRIVATE KEY-----` |
| `VAPID_SUB` | `mailto:deine@email.tld` |

Optional auch lokal in `.env` für manuelle Test-Sends:

```
VAPID_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIG...
-----END PRIVATE KEY-----"
VAPID_SUB=mailto:deine@email.tld
```

## Schritt 4 — Telegram-Secrets aufräumen

Telegram-Bot ist seit Roadmap-B6 (2026-06-20) komplett retired (PWA-Push ist primärer Kanal).
GitHub-Secrets `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` können gefahrlos gelöscht werden.

## Schritt 5 — PWA-Notifications aktivieren

1. PWA öffnen (idealerweise als Home-Screen-App auf iOS)
2. ⚙ rechts oben → **🔔 Push-Benachrichtigungen** → "AUS" antippen → System-Dialog "Erlauben"
3. Toggle steht auf "AN" — Setup fertig

## Was wird verschickt

| Event | Notification |
|---|---|
| Neue HIGH/MEDIUM Confidence Value-Bets nach Scan | `⚡ N Value Bets — 2026-06-17` mit Top 3 Bets im Body |
| Wette gewonnen | `✅ Belgien vs Ägypten · +€29.39` |
| Wette verloren | `❌ Kanada vs Bosnien · −€8` |
| Odds-API Quota niedrig | (deaktiviert — Telegram-Restfeature, nicht migriert) |

## iOS-Caveat ⚠️

Web Push auf iPhone funktioniert **nur** wenn die PWA über Safari → Share → "Zum Home-Bildschirm" hinzugefügt und dann als App (nicht im Safari-Tab) gestartet wird.

