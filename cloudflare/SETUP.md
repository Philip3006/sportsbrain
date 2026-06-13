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
