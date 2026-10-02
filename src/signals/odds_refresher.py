"""O1-7 — Live Odds Refresh Engine.

Reads all active signals from docs/data/signals.json, determines which ones
are due for a refresh based on time-to-kickoff cadence, fetches fresh odds
via sport-appropriate provider chain, then persists results to the sidecar.

The sidecar (data/cache/odds_state.json) is the sole writer authority for
refreshed odds. The next write_signals_json() call picks up all updates.

Provider strategy (football):
  The Odds API is the only authoritative odds provider.  Unavailable,
  exhausted, unauthorized, malformed, or stale data fails closed.

Provider strategy (tennis):
  src/tennis/odds/merger.py — 5 providers, parallel, zero quota cost.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals.provider_budget import (
    is_provider_available,
    record_error,
    record_success,
)
from src.signals.signal_status import (
    SignalStatus,
    compute_current_ev,
    evaluate_signal_status,
    load_current_match_odds,
    load_odds_state,
    make_signal_id,
    update_current_match_odds,
    update_odds_state,
)

_log = logging.getLogger("sportsbrain.signals.odds_refresher")

ROOT = Path(__file__).resolve().parents[2]
_SIGNALS_JSON = ROOT / "docs" / "data" / "signals.json"
_SCHEDULE_ONLY_WINDOW_HOURS = 24
_MAX_SCHEDULE_ONLY_MATCHES = 25

# ---------------------------------------------------------------------------
# Refresh cadence
# ---------------------------------------------------------------------------


def _refresh_interval_minutes(minutes_to_kickoff: float) -> int:
    """Return the desired minimum interval between refreshes for this signal."""
    if minutes_to_kickoff < 60:
        return 5
    if minutes_to_kickoff < 180:
        return 10
    if minutes_to_kickoff < 720:
        return 15
    if minutes_to_kickoff < 1440:
        return 20
    return 30  # >24h: slower cadence for non-top candidates


def _is_refresh_due(signal: dict, odds_state_entry: dict | None) -> bool:
    """Return True if this signal should be refreshed now.

    Wave 3C: respects canonical event_status so delayed/awaiting matches keep
    receiving odds updates. Hard stops on authoritative LIVE/terminal states only.
    """
    kickoff = signal.get("kickoff", "")
    event_status = signal.get("event_status", "")
    now = datetime.now(timezone.utc)

    # Authoritative match-in-progress or terminal → no more pre-match refresh
    if event_status in ("LIVE", "COMPLETED", "CANCELLED"):
        return False

    minutes_to_kickoff = float("inf")
    if kickoff:
        try:
            ko_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            minutes_to_kickoff = (ko_dt - now).total_seconds() / 60
        except ValueError:
            pass

    # AWAITING_START / DELAYED: match hasn't started despite time elapsed.
    # Treat as effectively 0 min to kickoff so normal cadence applies.
    if event_status in ("AWAITING_START", "DELAYED"):
        minutes_to_kickoff = max(minutes_to_kickoff, 0.0)
    elif minutes_to_kickoff < -5:
        # Past kickoff by more than 5 min, no canonical state → skip
        return False

    interval = _refresh_interval_minutes(minutes_to_kickoff)

    if not odds_state_entry:
        return True  # Never refreshed

    retry_after_str = odds_state_entry.get("retry_after_ts")
    if retry_after_str:
        try:
            retry_after = datetime.fromisoformat(retry_after_str.replace("Z", "+00:00"))
            if now < retry_after:
                return False
        except ValueError:
            # An invalid retry timestamp must not suppress a necessary refresh.
            pass

    last_ts_str = odds_state_entry.get("odds_ts")
    if not last_ts_str:
        return True

    try:
        last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
        age_min = (now - last_ts).total_seconds() / 60
        return age_min >= interval
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Market → odds field mapping for FootballOddsQuote
# ---------------------------------------------------------------------------


def _football_market_odds(quote, market: str) -> float | None:
    """Extract the single-sided decimal odds for a football market from a quote."""
    if market == "home":
        return quote.h2h_home or None
    if market == "away":
        return quote.h2h_away or None
    if market == "draw":
        return quote.h2h_draw or None
    if market in ("o/u2.5_over", "o/u2.5_o"):
        return quote.ou_over or None
    if market in ("o/u2.5_under", "o/u2.5_u"):
        return quote.ou_under or None
    if market in ("o/u1.5_over",):
        return quote.ou15_over or None
    if market in ("o/u1.5_under",):
        return quote.ou15_under or None
    if market in ("o/u3.5_over",):
        return quote.ou35_over or None
    if market in ("o/u3.5_under",):
        return quote.ou35_under or None
    if market == "btts_yes":
        return quote.btts_yes or None
    if market == "btts_no":
        return quote.btts_no or None
    if market in ("dc_1x", "1x"):
        return quote.dc_1x or None
    if market in ("dc_x2", "x2"):
        return quote.dc_x2 or None
    if market in ("dc_12", "12"):
        return quote.dc_12 or None
    return None


# ---------------------------------------------------------------------------
# Football refresh path — The Odds API only
# ---------------------------------------------------------------------------


def _fetch_football_quote(signal: dict):
    """Fetch one authoritative football quote for a match."""
    home, away = signal.get("match", " vs ").split(" vs ", 1)
    kickoff = signal.get("kickoff", "")
    match_hint = {
        "home_team": home.strip(),
        "away_team": away.strip(),
        "commence_time": kickoff,
        "sport_key": "soccer_germany_bundesliga2",
    }

    # The Odds API is the sole football odds authority.
    if is_provider_available("the_odds_api"):
        try:
            from src.football.odds.the_odds_api import fetch as toa_fetch

            quote = toa_fetch(match_hint)
            if quote and quote.h2h_home > 0:
                record_success("the_odds_api")
                return quote, "the_odds_api", 1
        except Exception as e:  # noqa: BLE001 - football provider boundary fails closed
            _log.debug("[refresher] the_odds_api error: %s", e)
            record_error("the_odds_api", 500)

    # Fail closed — no WebSearch for authoritative football pricing
    return None, "", 0


def _refresh_football(signal: dict) -> tuple[float | None, str, int]:
    """Fetch fresh football odds for the requested signal market."""
    quote, source, tier = _fetch_football_quote(signal)
    if quote is None:
        return None, "", 0
    odds = _football_market_odds(quote, signal.get("market", "home"))
    if odds and odds > 1.0:
        return odds, source, tier
    return None, "", 0


def _quote_timestamp(value) -> str:
    """Serialize a provider quote timestamp as an explicit UTC ISO value."""
    if not isinstance(value, datetime):
        return ""
    timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match_identity(signal: dict) -> tuple[str, str, str]:
    """Return stable (key, home, away) identity for a signal/schedule entry."""
    match = str(signal.get("match", ""))
    parts = match.split(" vs ", 1)
    home = str(signal.get("home", parts[0] if parts else "")).strip()
    away = str(signal.get("away", parts[1] if len(parts) > 1 else "")).strip()
    sport = str(signal.get("sport", "football")).strip().lower() or "football"
    key = f"{sport}:{home.casefold()} vs {away.casefold()}"
    return key, home, away


def _match_snapshot(
    signal: dict,
    *,
    outcomes: dict,
    source: str,
    tier: int,
    source_ts: str,
    captured_at: str,
    signal_markets: dict | None = None,
) -> dict:
    """Build the public-safe match snapshot plus private signal-market context."""
    match_key, home, away = _match_identity(signal)
    fixture_key = signal.get("fixture_key") or f"{match_key}"
    snapshot = {
        "sport": signal.get("sport", "football"),
        "match": f"{home} vs {away}",
        "home": home,
        "away": away,
        "fixture_key": fixture_key,
        "outcomes": outcomes,
        "odds_ts": captured_at,
        "captured_at": captured_at,
        "source_ts": source_ts,
        "source": source,
        "bookmaker": signal.get("bookmaker", "") or "consensus",
        "odds_fetch_tier": tier,
        "freshness": "current",
        "current": True,
    }
    if signal_markets:
        snapshot["signal_markets"] = signal_markets
    return snapshot


def _refresh_football_snapshot(signal: dict) -> dict | None:
    quote, source, tier = _fetch_football_quote(signal)
    if quote is None or not quote.has_1x2():
        return None
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signal_markets = {
        market: odds
        for market in (
            "home",
            "draw",
            "away",
            "o/u2.5_over",
            "o/u2.5_under",
            "o/u1.5_over",
            "o/u1.5_under",
            "o/u3.5_over",
            "o/u3.5_under",
            "btts_yes",
            "btts_no",
            "dc_1x",
            "dc_x2",
            "dc_12",
        )
        if (odds := _football_market_odds(quote, market)) and odds > 1.0
    }
    snapshot = _match_snapshot(
        signal,
        outcomes={
            "home": quote.h2h_home,
            "draw": quote.h2h_draw,
            "away": quote.h2h_away,
        },
        source=source,
        tier=tier,
        source_ts=_quote_timestamp(getattr(quote, "ts", None)),
        captured_at=captured_at,
        signal_markets=signal_markets,
    )
    snapshot["bookmaker"] = getattr(quote, "bookmaker", "") or "consensus"
    return snapshot


# ---------------------------------------------------------------------------
# Tennis refresh chain
# ---------------------------------------------------------------------------


def _refresh_tennis(
    signal: dict,
    quote_cache: dict[
        tuple[str, str, str], tuple[float | None, float | None, str, int, str, str]
    ]
    | None = None,
    diagnostics: list[dict] | None = None,
) -> tuple[float | None, str, int]:
    """Fetch fresh tennis odds via existing 5-provider merger."""
    snapshot = _refresh_tennis_snapshot(signal, quote_cache, diagnostics)
    if snapshot is None:
        return None, "", 0
    market = signal.get("market", "home")
    side = "home" if market in ("home", "ah-1.5_a") else "away"
    odds = snapshot.get("outcomes", {}).get(side)
    if isinstance(odds, (int, float)) and odds > 1.0:
        return odds, snapshot["source"], snapshot["odds_fetch_tier"]
    return None, "", 0


def _refresh_tennis_snapshot(
    signal: dict,
    quote_cache: dict[
        tuple[str, str, str], tuple[float | None, float | None, str, int, str, str]
    ]
    | None = None,
    diagnostics: list[dict] | None = None,
) -> dict | None:
    """Fetch one tennis quote and preserve both primary outcomes."""
    match_str = signal.get("match", " vs ")
    parts = match_str.split(" vs ", 1)
    player_a = parts[0].strip() if parts else ""
    player_b = parts[1].strip() if len(parts) > 1 else ""
    kickoff = signal.get("kickoff", "")
    tournament = signal.get("tournament", "")
    cache_key = (match_str, kickoff, tournament)
    cached = quote_cache.get(cache_key) if quote_cache is not None else None
    if cached is None:
        try:
            from src.tennis.odds.merger import fetch_best_odds_with_diagnostics

            quote, provider_outcomes = fetch_best_odds_with_diagnostics(
                {
                    "player_a": player_a,
                    "player_b": player_b,
                    "tournament": tournament,
                    "commence_time": kickoff,
                },
                timeout_s=5.0,
                allow_implied=False,
                include_websearch=False,
            )
            if diagnostics is not None:
                diagnostics.extend(provider_outcomes)
            if quote and not quote.no_bet_flag:
                cached = (
                    quote.h2h_a,
                    quote.h2h_b,
                    quote.source,
                    quote.source_tier,
                    _quote_timestamp(getattr(quote, "ts", None)),
                    getattr(quote, "bookmaker", ""),
                )
            else:
                cached = (None, None, "", 0, "", "")
        except Exception as e:  # noqa: BLE001 - provider boundary fails closed
            _log.debug("[refresher] tennis merger error: %s", e)
            cached = (None, None, "", 0, "", "")
        if quote_cache is not None:
            quote_cache[cache_key] = cached
    if not cached[0] or not cached[1] or cached[0] <= 1.0 or cached[1] <= 1.0:
        return None
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = _match_snapshot(
        signal,
        outcomes={"home": cached[0], "away": cached[1]},
        source=cached[2],
        tier=cached[3],
        source_ts=cached[4],
        captured_at=captured_at,
    )
    snapshot["bookmaker"] = cached[5] or ""
    return snapshot


# ---------------------------------------------------------------------------
# Core refresh loop
# ---------------------------------------------------------------------------


def _load_signals() -> list[dict]:
    """Load all signals from docs/data/signals.json (football + tennis)."""
    if not _SIGNALS_JSON.exists():
        return []
    try:
        data = json.loads(_SIGNALS_JSON.read_text())
    except Exception:  # noqa: BLE001 - malformed signal input fails closed
        return []
    signals = []
    for section in ("football", "tennis"):
        section_data = data.get(section, [])
        if isinstance(section_data, list):
            signals.extend(section_data)
    return signals


def _load_schedule() -> list[dict]:
    """Load the canonical scheduled-match inventory from signals.json."""
    if not _SIGNALS_JSON.exists():
        return []
    try:
        data = json.loads(_SIGNALS_JSON.read_text())
    except Exception:  # noqa: BLE001 - malformed schedule fails closed
        return []
    schedule = data.get("schedule", [])
    return (
        [entry for entry in schedule if isinstance(entry, dict)]
        if isinstance(schedule, list)
        else []
    )


def _match_groups(
    signals: list[dict], schedule: list[dict], now: datetime
) -> dict[str, dict]:
    """Group signal and scheduled inventory into one refresh per match.

    Signal matches retain their existing scope.  Schedule-only refreshes are
    deliberately bounded to the next 24 hours and the next 25 kickoffs so a
    large display schedule cannot become an unbounded provider poll.
    """
    groups: dict[str, dict] = {}
    for signal in signals:
        key, _home, _away = _match_identity(signal)
        group = groups.setdefault(key, {"context": dict(signal), "signals": []})
        group["signals"].append(signal)

    schedule_only: list[tuple[datetime, str, dict]] = []
    for entry in schedule:
        sport = str(entry.get("sport", "football")).strip().lower()
        if sport not in {"football", "tennis"}:
            continue
        kickoff = entry.get("scheduled_start_current") or entry.get("kickoff", "")
        context = {
            **entry,
            "sport": sport,
            "kickoff": kickoff,
            "match": f"{entry.get('home', '')} vs {entry.get('away', '')}",
            "tournament": entry.get("tournament") or entry.get("tour", ""),
        }
        key, _home, _away = _match_identity(context)
        if key in groups:
            # Schedule status/time is authoritative when present, while signal
            # model fields and fixture identity remain untouched.
            if context.get("event_status"):
                groups[key]["context"]["event_status"] = context["event_status"]
            if context.get("kickoff"):
                groups[key]["context"]["kickoff"] = context["kickoff"]
            for field in ("fixture_key", "league", "tournament"):
                if context.get(field) and not groups[key]["context"].get(field):
                    groups[key]["context"][field] = context[field]
            continue
        if not kickoff or context.get("event_status") in {
            "LIVE",
            "COMPLETED",
            "CANCELLED",
        }:
            continue
        try:
            kickoff_dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now <= kickoff_dt <= now + timedelta(hours=_SCHEDULE_ONLY_WINDOW_HOURS):
            schedule_only.append((kickoff_dt, key, context))

    for _kickoff, key, context in sorted(schedule_only)[:_MAX_SCHEDULE_ONLY_MATCHES]:
        groups[key] = {"context": context, "signals": []}
    return groups


def _retry_after(
    signal: dict, state_entry: dict | None, now: datetime
) -> tuple[str, int]:
    """Persist bounded, visible backoff after an unsuccessful provider cycle."""
    kickoff = signal.get("kickoff", "")
    minutes_to_kickoff = float("inf")
    if kickoff:
        try:
            minutes_to_kickoff = (
                datetime.fromisoformat(kickoff.replace("Z", "+00:00")) - now
            ).total_seconds() / 60
        except ValueError:
            pass
    if signal.get("event_status") in ("AWAITING_START", "DELAYED"):
        minutes_to_kickoff = max(minutes_to_kickoff, 0.0)
    failures = int((state_entry or {}).get("refresh_failure_count", 0) or 0) + 1
    base_minutes = max(_refresh_interval_minutes(minutes_to_kickoff), 15)
    retry_minutes = min(60, base_minutes * (2 ** min(failures - 1, 2)))
    return (now + timedelta(minutes=retry_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ), failures


def _publish_staged_signals(stage_dir: Path) -> None:
    paths = sorted(
        str(path.relative_to(stage_dir))
        for path in (stage_dir / "docs" / "data").glob("signals*.json")
    )
    if not paths:
        raise RuntimeError("no staged signal artifacts to publish")
    log_path = Path.home() / "Library" / "Logs" / "sportsbrain_odds_refresh.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "publish_runtime_artifacts.sh"),
            "publish-staged",
            str(ROOT),
            str(stage_dir),
            str(log_path),
            "auto: odds refresh",
            *paths,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"staged signal publication failed (exit {result.returncode})"
        )


def _retry_is_active(state_entry: dict | None) -> bool:
    if not state_entry or not state_entry.get("retry_after_ts"):
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(
            state_entry["retry_after_ts"].replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False


def run_refresh(dry_run: bool = False) -> dict:
    """Refresh odds for all signals that are due.

    Returns summary dict: {refreshed, skipped, failed, elapsed_s}
    """
    t0 = time.monotonic()
    signals = _load_signals()
    schedule = _load_schedule()
    groups = _match_groups(signals, schedule, datetime.now(timezone.utc))
    if not groups:
        _log.info("[refresher] no signals to refresh")
        return {"refreshed": 0, "skipped": 0, "failed": 0, "elapsed_s": 0.0}

    odds_state = load_odds_state()
    match_odds_state = load_current_match_odds()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    refreshed = skipped = failed = 0
    due_by_sport = {"football": 0, "tennis": 0}
    refreshed_by_sport = {"football": 0, "tennis": 0}
    failed_by_sport = {"football": 0, "tennis": 0}
    retry_deferred = 0
    tennis_quote_cache: dict[
        tuple[str, str, str], tuple[float | None, float | None, str, int, str, str]
    ] = {}
    tennis_provider_diagnostics: list[dict] = []
    failed_provider_examples: list[dict] = []

    for match_key, group in groups.items():
        context = group["context"]
        grouped_signals = group["signals"]
        sport = context.get("sport", "football")
        match = context.get("match", "")
        state_entry = match_odds_state.get(match_key)

        if not _is_refresh_due(context, state_entry):
            skipped += 1
            retry_deferred += int(_retry_is_active(state_entry))
            continue

        due_by_sport.setdefault(sport, 0)
        due_by_sport[sport] += 1

        _log.debug("[refresher] refreshing match %s | %s", sport, match)
        signal_diagnostics: list[dict] = []
        if sport == "tennis":
            snapshot = _refresh_tennis_snapshot(
                context, tennis_quote_cache, signal_diagnostics
            )
            tennis_provider_diagnostics.extend(signal_diagnostics)
        else:
            snapshot = _refresh_football_snapshot(context)

        if snapshot is None:
            failed += 1
            failed_by_sport.setdefault(sport, 0)
            failed_by_sport[sport] += 1
            retry_after_ts, failure_count = _retry_after(
                context, state_entry, datetime.now(timezone.utc)
            )
            if not dry_run and state_entry:
                failed_snapshot = dict(state_entry)
                failed_snapshot.update(
                    {
                        "current": False,
                        "freshness": "stale",
                        "retry_after_ts": retry_after_ts,
                        "refresh_failure_count": failure_count,
                    }
                )
                update_current_match_odds(match_key, failed_snapshot)
            for sig in grouped_signals:
                sid = sig.get("signal_id") or make_signal_id(
                    sport,
                    sig.get("match", ""),
                    sig.get("market", ""),
                    sig.get("kickoff", ""),
                )
                signal_state = odds_state.get(sid)
                cached = signal_state.get("current_odds") if signal_state else None
                cached_ts = signal_state.get("odds_ts") if signal_state else None
                status = evaluate_signal_status(sig, cached, cached_ts)
                if not dry_run:
                    cached_ev_pct = (
                        signal_state.get("current_ev_pct") if signal_state else None
                    )
                    cached_ev_decimal = (
                        cached_ev_pct / 100.0 if cached_ev_pct is not None else None
                    )
                    retry_after_ts, failure_count = _retry_after(
                        sig, signal_state, datetime.now(timezone.utc)
                    )
                    update_odds_state(
                        sid,
                        current_odds=cached,
                        odds_ts=cached_ts,
                        odds_source=(
                            signal_state.get("odds_source") if signal_state else None
                        ),
                        odds_fetch_tier=(
                            signal_state.get("odds_fetch_tier") if signal_state else 0
                        ),
                        signal_status=status,
                        current_ev_pct=cached_ev_decimal,
                        retry_after_ts=retry_after_ts,
                        refresh_failure_count=failure_count,
                    )
            if sport == "tennis" and len(failed_provider_examples) < 5:
                failed_provider_examples.append(
                    {
                        "match": match,
                        "tournament": context.get("tournament", ""),
                        "providers": signal_diagnostics,
                    }
                )
            continue

        if not dry_run:
            update_current_match_odds(match_key, snapshot)
        refreshed += 1
        refreshed_by_sport.setdefault(sport, 0)
        refreshed_by_sport[sport] += 1

        for sig in grouped_signals:
            sid = sig.get("signal_id") or make_signal_id(
                sport,
                sig.get("match", ""),
                sig.get("market", ""),
                sig.get("kickoff", ""),
            )
            selected = (snapshot.get("signal_markets") or {}).get(sig.get("market"))
            if selected is None:
                selected = (snapshot.get("outcomes") or {}).get(sig.get("market"))
            if not isinstance(selected, (int, float)) or selected <= 1.0:
                continue
            source = snapshot.get("source", "")
            tier = int(snapshot.get("odds_fetch_tier", 0) or 0)
            ev = compute_current_ev(float(sig.get("model_prob", 0)), selected)
            status: SignalStatus = evaluate_signal_status(
                sig, selected, snapshot.get("odds_ts")
            )
            if not dry_run:
                update_odds_state(
                    sid,
                    current_odds=selected,
                    odds_ts=snapshot.get("odds_ts", now_str),
                    odds_source=source,
                    odds_fetch_tier=tier,
                    signal_status=status,
                    current_ev_pct=ev,
                    retry_after_ts=None,
                    refresh_failure_count=0,
                )
            _log.info(
                "[refresher] %s | %s | %s → odds=%.2f ev=%.1f%% status=%s src=%s",
                sport,
                sig.get("match", match),
                sig.get("market", ""),
                selected,
                ev * 100,
                status,
                source,
            )

    elapsed = round(time.monotonic() - t0, 2)
    provider_outcome_counts: dict[str, dict[str, int]] = {}
    for outcome in tennis_provider_diagnostics:
        provider = str(outcome.get("provider", "unknown"))
        status_class = str(outcome.get("status_class", "unknown"))
        provider_outcome_counts.setdefault(provider, {})[status_class] = (
            provider_outcome_counts.setdefault(provider, {}).get(status_class, 0) + 1
        )
    summary = {
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
        "elapsed_s": elapsed,
        "due_by_sport": due_by_sport,
        "refreshed_by_sport": refreshed_by_sport,
        "failed_by_sport": failed_by_sport,
        "retry_deferred": retry_deferred,
        "tennis_provider_outcome_counts": provider_outcome_counts,
        "failed_provider_examples": failed_provider_examples,
    }
    _log.info("[refresher] done: %s", summary)

    # Re-publish signals.json so current_odds/signal_status are visible immediately.
    # Passes no new scanner signals — write_signals_json preserves existing signals
    # and just re-merges the updated sidecar. Skip in dry_run to avoid side effects.
    if not dry_run and (refreshed > 0 or failed > 0):
        try:
            stage_parent = Path(
                os.getenv(
                    "SPORTSBRAIN_RUNTIME_STAGE_BASE",
                    str(Path.home() / "Library" / "Caches" / "SportsBrain"),
                )
            )
            resolved_stage_parent = stage_parent.resolve()
            resolved_root = ROOT.resolve()
            if (
                not stage_parent.is_absolute()
                or resolved_stage_parent == resolved_root
                or resolved_root in resolved_stage_parent.parents
            ):
                raise RuntimeError(
                    "odds refresh staging directory must be external to the active checkout"
                )
            stage_parent.mkdir(parents=True, exist_ok=True)
            stage_root = Path(
                tempfile.mkdtemp(prefix="odds-refresh-", dir=stage_parent)
            )
            previous_stage = os.environ.get("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR")
            os.environ["SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR"] = str(stage_root)
            try:
                from src.notifications.web_dashboard import write_signals_json_all_users

                failed_users = write_signals_json_all_users(football=[], tennis=[])
                if failed_users:
                    raise RuntimeError(f"cloud upload failed for users={failed_users}")
                _publish_staged_signals(stage_root)
            finally:
                if previous_stage is None:
                    os.environ.pop("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", None)
                else:
                    os.environ["SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR"] = (
                        previous_stage
                    )
            _log.info("[refresher] signals staged and republished")
        except Exception as exc:  # noqa: BLE001 - publication failure is visible
            _log.warning("[refresher] republish failed: %s", exc)
            summary["publication_failed"] = True

    return summary
