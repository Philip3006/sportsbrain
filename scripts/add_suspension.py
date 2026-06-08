"""
Quick CLI to add/remove player suspensions for WM 2026.

Usage:
    python3 scripts/add_suspension.py "Brazil" "Rodrygo" add
    python3 scripts/add_suspension.py "Brazil" "Rodrygo" remove
    python3 scripts/add_suspension.py --list
    python3 scripts/add_suspension.py "Brazil" --list
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SUSPENSIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "suspensions.json"


def _load() -> dict:
    if not _SUSPENSIONS_FILE.exists():
        return {"_comment": "Manual suspension tracking for WM 2026. Update before each match.",
                "_format": "team_name: [player_name, ...]"}
    try:
        return json.loads(_SUSPENSIONS_FILE.read_text())
    except Exception as e:
        print(f"Error reading {_SUSPENSIONS_FILE}: {e}")
        sys.exit(1)


def _save(data: dict) -> None:
    _SUSPENSIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _list_all(data: dict) -> None:
    teams = {k: v for k, v in data.items() if not k.startswith("_")}
    if not teams:
        print("No suspensions currently tracked.")
        return
    for team, players in sorted(teams.items()):
        print(f"{team}:")
        for p in players:
            print(f"  - {p}")


def _list_team(data: dict, team: str) -> None:
    teams = {k: v for k, v in data.items() if not k.startswith("_")}
    # case-insensitive lookup
    matched = next((k for k in teams if k.lower() == team.lower()), None)
    if not matched:
        print(f"No suspensions for {team!r}.")
        return
    print(f"{matched}:")
    for p in teams[matched]:
        print(f"  - {p}")


def _add(data: dict, team: str, player: str) -> None:
    # Find existing team key (case-insensitive)
    existing_key = next(
        (k for k in data if not k.startswith("_") and k.lower() == team.lower()), None
    )
    if existing_key is None:
        data[team] = [player]
        print(f"Added: {team} → {player}")
    else:
        if player in data[existing_key]:
            print(f"{player!r} already suspended for {existing_key!r}.")
            return
        data[existing_key].append(player)
        print(f"Added: {existing_key} → {player}")
    _save(data)


def _remove(data: dict, team: str, player: str) -> None:
    existing_key = next(
        (k for k in data if not k.startswith("_") and k.lower() == team.lower()), None
    )
    if existing_key is None:
        print(f"No suspensions found for {team!r}.")
        return
    if player not in data[existing_key]:
        print(f"{player!r} not in suspension list for {existing_key!r}.")
        return
    data[existing_key].remove(player)
    if not data[existing_key]:
        del data[existing_key]
        print(f"Removed: {existing_key} → {player} (team entry deleted, no more suspensions)")
    else:
        print(f"Removed: {existing_key} → {player}")
    _save(data)


def main() -> None:
    args = sys.argv[1:]

    if not args or args == ["--list"]:
        data = _load()
        _list_all(data)
        return

    if len(args) == 2 and args[1] == "--list":
        data = _load()
        _list_team(data, args[0])
        return

    if len(args) == 3:
        team, player, action = args
        if action not in ("add", "remove"):
            print(f"Unknown action {action!r}. Use 'add' or 'remove'.")
            sys.exit(1)
        data = _load()
        if action == "add":
            _add(data, team, player)
        else:
            _remove(data, team, player)
        return

    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
