#!/usr/bin/env python3
"""One-shot: sendet einen Ergebnis-Push für ein abgeschlossenes Match.

Nutzung (via GH Actions mit VAPID-Secrets):
  HOME=France AWAY=Senegal HOME_SCORE=3 AWAY_SCORE=1 python3 scripts/send_result_push.py
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

home       = os.getenv("HOME_TEAM",   "France")
away       = os.getenv("AWAY_TEAM",   "Senegal")
home_score = os.getenv("HOME_SCORE",  "?")
away_score = os.getenv("AWAY_SCORE",  "?")

from src.notifications.web_push import _send_notification

sent = _send_notification(
    title=f"⚽ Abpfiff — {home} {home_score} : {away_score} {away}",
    body=f"Spielende. Deine offenen Wetten auf dieses Spiel werden bald abgerechnet.",
    url="/sportsbrain/#bets",
    kind="result",
    tag=f"result-{home}-{away}",
    require=False,
)
print(f"Push gesendet an {sent} Subscriber(s).")
sys.exit(0 if sent > 0 else 1)
