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
from src.notifications.flags import flag

fh, fa = flag(home), flag(away)
score_line = f"{fh} {home}  {home_score} : {away_score}  {away} {fa}".strip()

if push_type == "goal":
    scorer = home if home_score > away_score else away
    scorer_flag = fh if scorer == home else fa
    is_equalizer = home_score == away_score
    goal_emoji = "⚖️" if is_equalizer else "⚽"
    goal_label = "AUSGLEICH" if is_equalizer else "TOR!"
    title  = f"{goal_emoji} {goal_label} {minute}' — {scorer_flag} {scorer}".strip()
    body   = score_line
    kind, tag = "goal", f"goal-sim-{home}-{away}-{home_score}-{away_score}"
elif push_type == "halftime":
    title  = "⏸️ Halbzeit"
    body   = score_line
    kind, tag = "halftime", f"ht-sim-{home}-{away}"
else:
    title  = "🏁 Abpfiff"
    body   = f"{score_line}\nOffene Wetten werden bald abgerechnet."
    kind, tag = "result", f"result-sim-{home}-{away}"

sent = _send_notification(title=title, body=body, url="/sportsbrain/#bets",
                          kind=kind, tag=tag, require=False)
print(f"Push gesendet an {sent} Subscriber(s).")
sys.exit(0 if sent > 0 else 1)
