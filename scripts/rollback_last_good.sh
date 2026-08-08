#!/usr/bin/env bash
# CEO-Roadmap O0-5 — Rollback Capability
#
# Zeigt die letzten N Commits, für die ci_gates.yml grün war,
# und optional den Rollback-Befehl (git revert, kein hard-reset).
#
# Usage:
#   scripts/rollback_last_good.sh              # zeigt letzte 5 grüne Commits
#   scripts/rollback_last_good.sh --revert     # revertiert bis zum letzten grünen
#
# Sicherheits-Regel: KEIN git reset --hard auf pushed history.
# Rollback erfolgt über git revert HEAD..<sha> → neuer Commit → normaler Push
# → nächster ci_gates-Run bestätigt Grün.

set -euo pipefail

N=${LIMIT:-5}

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI benötigt" >&2
    exit 2
fi

echo "── letzte $N grüne ci_gates-Runs auf main ──────────────────────────"
gh run list --workflow=ci_gates.yml --branch main --limit 50 \
    --json headSha,conclusion,createdAt \
    | python3 -c "
import json, sys
runs = [r for r in json.load(sys.stdin)
        if r['conclusion'] == 'success']
for r in runs[:$N]:
    sha = r['headSha']
    print(f'  {sha[:8]}  {r[\"createdAt\"]}')
"

CURRENT=$(git rev-parse HEAD)
LAST_GOOD=$(gh run list --workflow=ci_gates.yml --branch main --limit 50 \
    --json headSha,conclusion \
    | python3 -c "
import json, sys
runs = json.load(sys.stdin)
good = [r['headSha'] for r in runs if r['conclusion'] == 'success']
print(good[0] if good else '')
")

if [[ -z "$LAST_GOOD" ]]; then
    echo "WARN: kein grüner ci_gates-Run in den letzten 50 Runs gefunden." >&2
    exit 1
fi

echo
echo "HEAD                = $CURRENT"
echo "Last-known-good SHA = $LAST_GOOD"

if [[ "$CURRENT" == "$LAST_GOOD" ]]; then
    echo "→ HEAD ist bereits der last-known-good Stand. Kein Rollback nötig."
    exit 0
fi

if [[ "${1:-}" != "--revert" ]]; then
    echo
    echo "Nächster Schritt (nach CEO-Freigabe):"
    echo "  scripts/rollback_last_good.sh --revert"
    echo
    echo "Das führt aus:"
    echo "  git revert --no-commit ${LAST_GOOD}..HEAD"
    echo "  git commit -m 'revert: rollback to last-known-good ${LAST_GOOD:0:8}'"
    echo "  git push origin main"
    exit 0
fi

echo
read -p "Wirklich alle Commits nach ${LAST_GOOD:0:8} reverten? (yes/N) " -r
if [[ "$REPLY" != "yes" ]]; then
    echo "Abgebrochen."
    exit 0
fi

git revert --no-commit "${LAST_GOOD}..HEAD"
git commit -m "revert: rollback to last-known-good ${LAST_GOOD:0:8}

CEO-Roadmap O0-5: Rollback ausgeführt.
Frühere HEAD: $CURRENT
Rückgängig gemacht bis: $LAST_GOOD (letzter grüner ci_gates-Run)"
echo "→ prüfe git log, dann: git push origin main"
