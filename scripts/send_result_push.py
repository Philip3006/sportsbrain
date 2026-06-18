#!/usr/bin/env python3
"""One-shot: sendet einen Test-Push (Tor oder Ergebnis).

Umgebungsvariablen:
  HOME_TEAM, AWAY_TEAM, HOME_SCORE, AWAY_SCORE
  PUSH_TYPE  = "goal" (default) | "result" | "halftime"
  MINUTE     = Spielminute für Tor-Push (default: 23)
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

home       = os.getenv("HOME_TEAM",  "Iraq")
away       = os.getenv("AWAY_TEAM",  "Norway")
home_score = int(os.getenv("HOME_SCORE", "1"))
away_score = int(os.getenv("AWAY_SCORE", "0"))
minute     = os.getenv("MINUTE", "23")
push_type  = os.getenv("PUSH_TYPE", "goal")

from src.notifications.web_push import _send_notification

if push_type == "goal":
    scorer = home if home_score > away_score else away
    title  = f"⚽ TOR — {scorer}"
    body   = f"{minute}'   {home} {home_score} : {away_score} {away}"
    kind, tag = "goal", f"goal-sim-{home}-{away}-{home_score}-{away_score}"
elif push_type == "halftime":
    title  = f"⏸️ HALBZEIT — {home} {home_score} : {away_score} {away}"
    body   = "Erste Hälfte vorbei. Tap für Match-Details."
    kind, tag = "halftime", f"ht-sim-{home}-{away}"
else:
    title  = f"⚽ Abpfiff — {home} {home_score} : {away_score} {away}"
    body   = "Spielende. Deine offenen Wetten werden bald abgerechnet."
    kind, tag = "result", f"result-sim-{home}-{away}"

sent = _send_notification(title=title, body=body, url="/sportsbrain/#bets",
                          kind=kind, tag=tag, require=False)
print(f"Push gesendet an {sent} Subscriber(s).")
sys.exit(0 if sent > 0 else 1)
