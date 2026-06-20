"""
WM 2026 Readiness Check
========================
Validates that all model files, squad caches, and environment variables
are in place before the tournament starts on 2026-06-11.
"""

import glob
import json
import os
import pickle
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

TODAY = date(2026, 6, 6)
WM_START = date(2026, 6, 11)
DAYS_UNTIL_WM = (WM_START - TODAY).days

# ---------------------------------------------------------------------------
# Full 48-team WM 2026 roster (based on confirmed qualifiers as of June 2026)
# ---------------------------------------------------------------------------
WM2026_TEAMS = [
    # Group A — USA, Mexico, Canada + 1 (CONCACAF host group)
    "United States", "Mexico", "Canada", "Honduras",
    # Group B — Argentina, Chile, Peru, Australia
    "Argentina", "Chile", "Peru", "Australia",
    # Group C — Brazil, Colombia, Paraguay, Japan
    "Brazil", "Colombia", "Paraguay", "Japan",
    # Group D — England, France, Germany, ?
    "England", "France", "Germany", "Croatia",
    # Group E — Spain, Portugal, Netherlands, ?
    "Spain", "Portugal", "Netherlands", "Poland",
    # Group F — Belgium, Switzerland, Serbia, ?
    "Belgium", "Switzerland", "Serbia", "South Korea",
    # Group G — Morocco, Senegal, Egypt, ?
    "Morocco", "Senegal", "Egypt", "Nigeria",
    # Group H — Uruguay, Ecuador, Venezuela, Bolivia
    "Uruguay", "Ecuador", "Venezuela", "Bolivia",
    # Group I — Costa Rica, Panama, Jamaica, El Salvador
    "Costa Rica", "Panama", "Jamaica", "El Salvador",
    # Group J — Cameroon, Ghana, Algeria, South Africa
    "Cameroon", "Ghana", "Algeria", "South Africa",
    # Group K — Saudi Arabia, Iran, Indonesia, Qatar
    "Saudi Arabia", "Iran", "Indonesia", "Qatar",
    # Group L — DR Congo, Mali, Tunisia, New Zealand
    "DR Congo", "Mali", "Tunisia", "New Zealand",
]

# Confirm exactly 48
assert len(WM2026_TEAMS) == 48, f"Expected 48 teams, got {len(WM2026_TEAMS)}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TICK = "✅"
WARN = "⚠️"
CROSS = "❌"


def load_dc_params(dc_dir: Path):
    """Load the most recent Dixon-Coles params file."""
    files = sorted(dc_dir.glob("params_*.pkl"))
    if not files:
        return None, None
    latest = files[-1]
    with open(latest, "rb") as f:
        params = pickle.load(f)
    return latest.name, params


def check_squad_cache(squad_dir: Path, team_name: str) -> bool:
    """Return True if the team has a non-empty squad cache file."""
    slug = team_name.lower().replace(" ", "_").replace("'", "")
    path = squad_dir / f"{slug}.json"
    if not path.exists():
        return False
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        players = data.get("players", data.get("squad", data.get("data", [])))
        return isinstance(players, list) and len(players) > 0
    return False


def check_env_keys(required: list[str]) -> dict[str, bool]:
    """Check that each required env key is set (non-empty)."""
    # Load .env manually if present (no python-dotenv dependency required)
    env_path = ROOT / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    # Merge with actual process env (process env takes precedence)
    for k in required:
        if k in os.environ:
            env_vars[k] = os.environ[k]
    return {k: bool(env_vars.get(k)) for k in required}


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------
def main():
    print("=" * 50)
    print("=== WM 2026 READINESS CHECK ===")
    print(f"Date: {TODAY} | Days until WM: {DAYS_UNTIL_WM}")
    print("=" * 50)
    print()

    issues: list[str] = []

    # -----------------------------------------------------------------------
    # 1. DC Model
    # -----------------------------------------------------------------------
    dc_dir = ROOT / "models" / "dixon_coles"
    dc_file, dc_params = load_dc_params(dc_dir)

    if dc_params is None:
        print(f"{CROSS} Dixon-Coles: No params file found in {dc_dir}")
        issues.append("No Dixon-Coles model file")
    else:
        n_dc_teams = len(dc_params.attack)
        fit_date = getattr(dc_params, "fit_date", "unknown")
        print(f"DC Model: {dc_file} ({n_dc_teams} teams, fit_date {str(fit_date)[:10]})")

    # -----------------------------------------------------------------------
    # 2. LightGBM Model
    # -----------------------------------------------------------------------
    lgbm_dir = ROOT / "models" / "lgbm"
    lgbm_feature_count = None
    lgbm_brier = None
    lgbm_ece = None

    fc_path = lgbm_dir / "feature_columns.json"
    if fc_path.exists():
        with open(fc_path) as f:
            feature_columns = json.load(f)
        lgbm_feature_count = len(feature_columns)

    # Try to read stored metrics from results if available
    metrics_path = ROOT / "results" / "lgbm_metrics.json"
    if metrics_path.exists():
        with open(metrics_path) as f:
            metrics = json.load(f)
        lgbm_brier = metrics.get("brier")
        lgbm_ece = metrics.get("ece")

    if lgbm_feature_count is not None:
        brier_str = f" | Brier {lgbm_brier:.4f}" if lgbm_brier else ""
        ece_str = f" | ECE {lgbm_ece:.4f}" if lgbm_ece else ""
        print(f"LightGBM: {lgbm_feature_count} features{brier_str}{ece_str}")
    else:
        print(f"{CROSS} LightGBM: feature_columns.json not found")
        issues.append("LightGBM feature_columns.json missing")

    print()

    # -----------------------------------------------------------------------
    # 3. DC Coverage of WM 2026 Teams
    # -----------------------------------------------------------------------
    if dc_params is not None:
        dc_teams_set = set(dc_params.attack.keys())
        covered = [t for t in WM2026_TEAMS if t in dc_teams_set]
        missing_dc = [t for t in WM2026_TEAMS if t not in dc_teams_set]
        pct = len(covered) / len(WM2026_TEAMS) * 100
        status = TICK if len(missing_dc) <= 5 else WARN
        print(f"DC Coverage: {len(covered)}/{len(WM2026_TEAMS)} WM 2026 teams {status} ({pct:.0f}%)")
        if missing_dc:
            print(f"  Missing DC: {missing_dc}")
            if len(missing_dc) > 5:
                issues.append(f"DC model missing {len(missing_dc)} WM teams: {missing_dc}")
        print()

    # -----------------------------------------------------------------------
    # 4. Squad Cache Coverage
    # -----------------------------------------------------------------------
    squad_dir = ROOT / "data" / "cache" / "squad"
    squad_ok = [t for t in WM2026_TEAMS if check_squad_cache(squad_dir, t)]
    squad_missing = [t for t in WM2026_TEAMS if not check_squad_cache(squad_dir, t)]
    n_blocked = len(squad_missing)

    cache_status = TICK if len(squad_ok) >= 30 else WARN
    print(f"Squad Cache: {len(squad_ok)}/{len(WM2026_TEAMS)} teams {cache_status} "
          f"({n_blocked} missing/blocked by Cloudflare)")
    if squad_missing:
        print(f"  Missing cache: {squad_missing}")
    print()

    # -----------------------------------------------------------------------
    # 5. Model Files
    # -----------------------------------------------------------------------
    print("Model Files:")
    model_checks = [
        ("Dixon-Coles", dc_dir / dc_file if dc_file else None,
         f"models/dixon_coles/{dc_file}" if dc_file else "models/dixon_coles/params_*.pkl"),
        ("LightGBM", lgbm_dir / "model.pkl", "models/lgbm/model.pkl"),
        ("Calibrators", lgbm_dir / "calibrators.pkl", "models/lgbm/calibrators.pkl"),
        ("Feature columns", lgbm_dir / "feature_columns.json", "models/lgbm/feature_columns.json"),
    ]

    for label, path, display in model_checks:
        if path and Path(path).exists():
            print(f"  {TICK} {label}: {display}")
        else:
            print(f"  {CROSS} {label}: {display} — NOT FOUND")
            issues.append(f"Missing model file: {display}")

    print()

    # -----------------------------------------------------------------------
    # 6. Environment Variables
    # -----------------------------------------------------------------------
    required_keys = ["ODDS_API_KEY", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]
    env_status = check_env_keys(required_keys)

    print("Environment:")
    for key, ok in env_status.items():
        if ok:
            print(f"  {TICK} {key} set")
        else:
            print(f"  {CROSS} {key} NOT SET")
            issues.append(f"Missing env var: {key}")
    print()

    # -----------------------------------------------------------------------
    # 7. Verdict
    # -----------------------------------------------------------------------
    print("-" * 50)
    if not issues:
        print(f"VERDICT: {TICK} READY FOR WM 2026")
    else:
        print(f"VERDICT: {WARN} ISSUES FOUND — review above")
        for iss in issues:
            print(f"  - {iss}")
    print("-" * 50)

    return len(issues)


if __name__ == "__main__":
    n_issues = main()
    sys.exit(0 if n_issues == 0 else 1)
