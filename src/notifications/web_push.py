"""Web Push Notifications via Cloudflare KV + pywebpush.

Ersetzt den Telegram-Notifier. Liest Push-Subscriptions vom Cloudflare
Worker (/push/list, Auth-Token), verschlüsselt das Payload mit pywebpush
(VAPID + aes128gcm) und sendet an alle subscribed Browsers.

Bei abgelaufenen Subscriptions (HTTP 404/410) ruft prune() den Worker, der
diese aus dem KV entfernt.

Setup:
    1. python3 scripts/gen_vapid_keys.py  → Keypair generieren
    2. Public-Key in docs/index.html eintragen
    3. .env / GitHub Secrets: VAPID_PRIVATE_KEY, VAPID_SUB,
       SIGNALS_CLOUD_URL, SIGNALS_API_TOKEN

Triggers (von daily_scan.py + post_match_update.py aufgerufen):
    send_scan_alert(...)         → "🔔 N neue Value Bets"
    send_settlement_alert(...)   → "✅ Match X gewonnen +€Y" / "❌ verloren"
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import requests

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover — soft import, only fail when actually sending
    webpush = None
    WebPushException = Exception  # type: ignore

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore
        return None

from src.betting.value_detector import BetSignal
from src.config import MAX_ACTIVE_BETS

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


# ── Worker-API Helpers ───────────────────────────────────────────
def _worker_base() -> str:
    """Liefert die Worker-Basis-URL (ohne /signals.json)."""
    url = os.getenv("SIGNALS_CLOUD_URL", "").strip()
    if not url:
        return ""
    return url.rstrip("/").removesuffix("/signals.json")


def _list_subscriptions() -> list[dict]:
    base = _worker_base()
    token = os.getenv("SIGNALS_API_TOKEN", "").strip()
    if not base or not token:
        return []
    try:
        r = requests.get(
            f"{base}/push/list",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("subs", [])
    except requests.RequestException as e:
        print(f"  [web_push] list_subscriptions failed: {e}")
        return []


def _prune_subscriptions(endpoints: Iterable[str]) -> None:
    base = _worker_base()
    token = os.getenv("SIGNALS_API_TOKEN", "").strip()
    eps = [e for e in endpoints if e]
    if not base or not token or not eps:
        return
    try:
        requests.post(
            f"{base}/push/prune",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"endpoints": eps}),
            timeout=10,
        )
    except requests.RequestException:
        pass


# ── Core Send ────────────────────────────────────────────────────
def _send_notification(title: str, body: str, *, url: str = "/", kind: str = "generic",
                       tag: str | None = None, require: bool = False) -> int:
    """Sendet Notification an alle subscribed Browser.

    Returns: Anzahl erfolgreich zugestellter Notifications.
    Bei expired Subscriptions (404/410) wird /push/prune aufgerufen.
    """
    if webpush is None:
        print("  [web_push] pywebpush nicht installiert — Notification übersprungen.")
        return 0
    load_dotenv(dotenv_path=_ENV_PATH)
    private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip().strip('"')
    sub_claim = os.getenv("VAPID_SUB", "").strip() or "mailto:noreply@sportsbrain"
    if not private_key:
        print("  [web_push] VAPID_PRIVATE_KEY fehlt — Notification übersprungen.")
        return 0
    # pywebpush akzeptiert base64url-encoded raw 32-Byte EC-Key.
    # GitHub-Secrets können den Key in drei Formaten enthalten:
    #   (a) PEM (PKCS8) mit BEGIN/END-Header   (lokales gen_vapid_keys.py Output)
    #   (b) Base64-Body ohne Header, evtl. einzeilig  (Secret-UI strippt Markers)
    #   (c) bereits raw base64url-32-Byte
    # Wir normalisieren alles auf (c).
    import base64, re
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key, load_der_private_key,
    )
    from cryptography.hazmat.primitives.asymmetric import ec

    def _key_obj_to_raw_b64url(key_obj) -> str | None:
        # EC-P-256 hat keine Encoding.Raw-Form — wir extrahieren die 32-Byte
        # private_value direkt aus dem Skalar 'd'.
        if not isinstance(key_obj, ec.EllipticCurvePrivateKey):
            return None
        d_int = key_obj.private_numbers().private_value
        raw = d_int.to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _to_raw_b64url(key_str: str) -> str | None:
        key_str = key_str.strip().strip('"').strip("'")
        if "\\n" in key_str and "\n" not in key_str:
            key_str = key_str.replace("\\n", "\n")
        # Case (a): PEM mit BEGIN/END
        if "-----BEGIN" in key_str:
            # Extrahiere NUR den ersten PEM-Block — ignoriert extra Zeilen nach -----END
            pm = re.search(r"(-+BEGIN[^-]+-+)(.*?)(-+END[^-]+-+)", key_str, re.DOTALL)
            if pm:
                h, body, foot = pm.groups()
                clean = "".join(body.split())
                if not clean:
                    # Body leer → schon auf Zeilenstruktur aufgebaut, direkt nehmen
                    clean_pem = f"{h}{body}{foot}\n"
                else:
                    lines = "\n".join(clean[i:i+64] for i in range(0, len(clean), 64))
                    clean_pem = f"{h}\n{lines}\n{foot}\n"
                try:
                    return _key_obj_to_raw_b64url(
                        load_pem_private_key(clean_pem.encode(), password=None)
                    )
                except Exception as _e:
                    print(f"  [web_push] PEM-Parse Fehler: {_e}")
        # Case (c)/(b): kompakter Base64-String — kann raw 32-Byte oder DER-PKCS8 sein
        compact = re.sub(r"\s+", "", key_str)
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                pad = "=" * (-len(compact) % 4)
                decoded = decoder(compact + pad)
            except Exception:
                continue
            if len(decoded) == 32:
                return base64.urlsafe_b64encode(decoded).rstrip(b"=").decode()
            try:
                return _key_obj_to_raw_b64url(load_der_private_key(decoded, password=None))
            except Exception:
                continue
        return None

    normalized = _to_raw_b64url(private_key)
    if normalized is None:
        print("  [web_push] VAPID-Key-Parse fehlgeschlagen: Format unbekannt "
              "(weder PEM noch Base64-DER noch raw base64url)")
        return 0
    private_key = normalized

    subs = _list_subscriptions()
    if not subs:
        return 0

    payload = json.dumps({
        "title":   title,
        "body":    body,
        "url":     url,
        "kind":    kind,
        "tag":     tag or kind,
        "require": require,
    })

    sent = 0
    expired: list[str] = []
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": sub_claim},
                ttl=3600,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                expired.append(sub.get("endpoint", ""))
            else:
                print(f"  [web_push] send failed ({code}): {e}")
        except Exception as e:
            print(f"  [web_push] unexpected error: {e}")

    if expired:
        _prune_subscriptions(expired)
        print(f"  [web_push] {len(expired)} expired subscriptions pruned.")
    return sent


# ── Public API: Scan Alert ───────────────────────────────────────
def _market_label(market: str, home: str, away: str) -> str:
    m = market.lower()
    if m == "home":     return f"{home} gewinnt"
    if m == "away":     return f"{away} gewinnt"
    if m == "draw":     return "Unentschieden"
    if m == "btts_yes": return "BTTS: Ja"
    if m == "btts_no":  return "BTTS: Nein"
    if m == "dc_1x":    return f"DC: {home}/Draw"
    if m == "dc_x2":    return f"DC: Draw/{away}"
    if m == "dc_12":    return f"DC: {home}/{away}"
    if "_over" in m or "_under" in m:
        return market.replace("_", " ").upper()
    return market.upper()


def send_scan_alert(
    signals: list[BetSignal],
    summary: dict,
    scan_date: str,
    bankroll: float = 100.0,
    match_contexts: dict | None = None,
) -> bool:
    """Sendet Push für neue Value Bets (HIGH + MEDIUM Confidence)."""
    actionable = [s for s in signals if s.confidence in ("HIGH", "MEDIUM")]
    if not actionable:
        return False

    top = sorted(actionable, key=lambda s: s.ev, reverse=True)[:3]
    n_high = sum(1 for s in actionable if s.confidence == "HIGH")
    n_med  = sum(1 for s in actionable if s.confidence == "MEDIUM")

    title = f"⚡ {len(actionable)} Value Bet{'s' if len(actionable) != 1 else ''} — {scan_date}"
    body_lines = []
    for s in top:
        stake = s.stake_pct * bankroll
        body_lines.append(
            f"{s.confidence[0]} {s.home}–{s.away} · "
            f"{_market_label(s.market, s.home, s.away)} @ {s.decimal_odds:.2f} · "
            f"EV +{s.ev*100:.1f}% · €{stake:.0f}"
        )
    if len(actionable) > 3:
        body_lines.append(f"… +{len(actionable) - 3} weitere")
    body_lines.append(f"({n_high} HIGH, {n_med} MEDIUM)")

    # Deep-link: bei genau 1 Signal direkt das Bet-Modal verlinken
    from urllib.parse import quote
    if len(top) == 1:
        bet_id = quote(f"{top[0].home} vs {top[0].away}:{top[0].market}", safe="")
        push_url = f"/sportsbrain/?bet={bet_id}#football"
    else:
        push_url = "/sportsbrain/#football"

    sent = _send_notification(
        title=title,
        body="\n".join(body_lines),
        url=push_url,
        kind="scan",
        tag=f"scan-{scan_date}",
        require=False,
    )
    return sent > 0


# ── Public API: Settlement Alert ─────────────────────────────────
def send_settlement_alert(record: dict, summary: dict) -> bool:
    """Sendet Push wenn Wette gewonnen/verloren wurde."""
    status = str(record.get("status", ""))
    if status not in ("won", "lost"):
        return False

    icon = {"won": "✅", "lost": "❌"}.get(status, "")
    home = str(record.get("home", ""))
    away = str(record.get("away", ""))
    market = str(record.get("market", ""))
    odds = float(record.get("decimal_odds", 0) or 0)
    stake = float(record.get("stake_amount", 0) or 0)
    pnl = float(record.get("pnl", 0) or 0)

    title = f"{icon} {home} vs {away}"
    body_lines = [
        f"{_market_label(market, home, away)} @ {odds:.2f}",
        f"Einsatz €{stake:.0f} · P&L {pnl:+.2f}€",
    ]
    n_open = summary.get("n_open", 0)
    total_pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    body_lines.append(
        f"Portfolio: {n_open}/{MAX_ACTIVE_BETS} aktiv · {total_pnl:+.2f}€ · ROI {roi:+.1f}%"
    )

    sent = _send_notification(
        title=title,
        body="\n".join(body_lines),
        url="/sportsbrain/#journal",
        kind="settlement",
        tag=f"settle-{home}-{away}-{market}",
        require=False,
    )
    return sent > 0


# ── Public API: Quota Alert (für Compatibility) ──────────────────
def send_quota_alert(remaining: int) -> bool:
    """No-op — Quota-Alerts wurden mit Web Push nicht migriert (siehe Plan)."""
    return False
