"""Central registry for time-boxed / phase-specific flags.

Motivation: Verhindert Zombie-Configs wie WC2026_BOOST=3.0, die nach dem
Event weiter im Code sitzen und bei Retrain wieder greifen können. Jeder Flag
hat sunset_date + owner + intent. Bei jedem Retrain-Snapshot wird
`assert_flags_valid()` aufgerufen und meldet abgelaufene Flags.

Konvention:
- `active` = darf gerade Effekte haben (Default False nach Sunset).
- `sunset_date` = ISO-Datum ab dem der Flag NICHT mehr default-active sein darf.
- `retry_capable` = darf temporär reaktiviert werden (z.B. bei Fit-Divergenz).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PhaseFlag:
    name: str
    active: bool
    sunset_date: str  # ISO
    retry_capable: bool
    intent: str


# Registry — jedes Phase-Flag hier eintragen, damit CI/Retrain es finden.
FLAGS: tuple[PhaseFlag, ...] = (
    PhaseFlag(
        name="WC2026_BOOST",
        active=False,  # neutralisiert seit 2026-07-30 (Config-Wert 1.0)
        sunset_date="2026-07-30",
        retry_capable=True,
        intent=(
            "WM-2026 Training-Gewichte-Boost. Retry-Logic (train_dixon_coles) darf "
            "temporär auf 0.5–1.5 setzen bei Fit-Divergenz. Nach WM neutralisiert; "
            "wird bei EM 2028 / WM 2030 als Template wiederverwendet."
        ),
    ),
    PhaseFlag(
        name="STACKER_ENABLED",
        active=True,
        sunset_date="",
        retry_capable=False,
        intent="Ensemble-Stacking-Layer (Phase 2.1). Kein Sunset geplant.",
    ),
    PhaseFlag(
        name="CONFORMAL_ENABLED",
        active=True,
        sunset_date="",
        retry_capable=False,
        intent="Conformal-Prediction-Intervalle. Kein Sunset geplant.",
    ),
    PhaseFlag(
        name="PPDA_LIVE_G1",
        active=True,
        sunset_date="",
        retry_capable=False,
        intent="PPDA-Live-Feature im 1X2-Stacker (Brier +0.0035, ROI +11.85pp). Rollback-Flag falls Signal degradiert.",
    ),
)


def get_flag(name: str) -> PhaseFlag | None:
    return next((f for f in FLAGS if f.name == name), None)


def is_active(name: str) -> bool:
    f = get_flag(name)
    return bool(f and f.active)


def expired_flags(today: date | None = None) -> list[PhaseFlag]:
    today = today or date.today()
    out: list[PhaseFlag] = []
    for f in FLAGS:
        if not f.sunset_date:
            continue
        if f.active and today.isoformat() > f.sunset_date:
            out.append(f)
    return out


def assert_flags_valid(today: date | None = None) -> None:
    """Raise wenn ein Flag active=True ist obwohl sunset_date überschritten."""
    bad = expired_flags(today)
    if bad:
        names = ", ".join(f.name for f in bad)
        raise RuntimeError(
            f"Expired phase flags still active: {names}. "
            f"Set active=False in src/phase_flags.py or extend sunset_date."
        )
