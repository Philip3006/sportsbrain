"""O1-7 — Live Odds Refresh Engine.

Reads all active signals from docs/data/signals.json, determines which ones
are due for a refresh based on time-to-kickoff cadence, fetches fresh odds
via sport-appropriate provider chain, then persists results to the sidecar.

The sidecar (data/cache/odds_state.json) is the sole writer authority for
refreshed odds. The next write_signals_json() call picks up all updates.

Provider strategy (football, Strategy B):
  TheOddsAPI (circuit-breaker) → Betfair → OddsPortal → cache → fail closed.
  WebSearch is NOT authoritative — signals with WebSearch-only odds get
  signal_status=UNREFRESHABLE (not ACTIVE) for Top Recommendation purposes.

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
    load_odds_state,
    make_signal_id,
    update_odds_state,
)

_log = logging.getLogger("sportsbrain.signals.odds_refresher")

ROOT = Path(__file__).resolve().parents[2]
_SIGNALS_JSON = ROOT / "docs" / "data" / "signals.json"

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
# Football refresh chain (Strategy B — no WebSearch as authoritative)
# ---------------------------------------------------------------------------

def _refresh_football(signal: dict) -> tuple[float | None, str, int]:
    """Fetch fresh football odds. Returns (odds, source_name, tier) or (None, "", 0)."""
    home, away = signal.get("match", " vs ").split(" vs ", 1)
    kickoff = signal.get("kickoff", "")
    match_hint = {
        "home_team": home.strip(),
        "away_team": away.strip(),
        "commence_time": kickoff,
        "sport_key": "soccer_germany_bundesliga2",
    }

    # Tier 1: Betfair (circuit-breaker aware)
    if is_provider_available("betfair"):
        try:
            from src.football.odds.betfair import fetch as betfair_fetch
            quote = betfair_fetch(match_hint)
            if quote and quote.h2h_home > 0:
                record_success("betfair")
                odds = _football_market_odds(quote, signal.get("market", "home"))
                if odds and odds > 1.0:
                    return odds, "betfair", 1
        except Exception as e:
            _log.debug("[refresher] betfair error: %s", e)
            record_error("betfair", 500)

    # Tier 2: OddsPortal (1X2 only)
    if is_provider_available("oddsportal"):
        try:
            from src.football.odds.oddsportal import fetch as op_fetch
            quote = op_fetch(match_hint)
            if quote and quote.h2h_home > 0:
                record_success("oddsportal")
                odds = _football_market_odds(quote, signal.get("market", "home"))
                if odds and odds > 1.0:
                    return odds, "oddsportal", 2
        except Exception as e:
            _log.debug("[refresher] oddsportal error: %s", e)
            record_error("oddsportal", 500)

    # Tier 2: TheOddsAPI (circuit-breaker will skip if exhausted)
    if is_provider_available("the_odds_api"):
        try:
            from src.football.odds.the_odds_api import fetch as toa_fetch
            quote = toa_fetch(match_hint)
            if quote and quote.h2h_home > 0:
                record_success("the_odds_api")
                odds = _football_market_odds(quote, signal.get("market", "home"))
                if odds and odds > 1.0:
                    return odds, "the_odds_api", 2
        except Exception as e:
            _log.debug("[refresher] the_odds_api error: %s", e)
            record_error("the_odds_api", 500)

    # Fail closed — no WebSearch for authoritative football pricing
    return None, "", 0


# ---------------------------------------------------------------------------
# Tennis refresh chain
# ---------------------------------------------------------------------------

def _refresh_tennis(
    signal: dict,
    quote_cache: dict[tuple[str, str, str], tuple[float | None, float | None, str, int]] | None = None,
    diagnostics: list[dict] | None = None,
) -> tuple[float | None, str, int]:
    """Fetch fresh tennis odds via existing 5-provider merger."""
    match_str = signal.get("match", " vs ")
    parts = match_str.split(" vs ", 1)
    player_a = parts[0].strip() if parts else ""
    player_b = parts[1].strip() if len(parts) > 1 else ""
    kickoff = signal.get("kickoff", "")
    market = signal.get("market", "home")

    match_hint = {
        "player_a": player_a,
        "player_b": player_b,
        "tournament": signal.get("tournament", ""),
        "commence_time": kickoff,
    }

    cache_key = (match_str, kickoff, match_hint["tournament"])
    cached = quote_cache.get(cache_key) if quote_cache is not None else None
    if cached is None:
        try:
            from src.tennis.odds.merger import fetch_best_odds_with_diagnostics
            # Tier-4 web search runs three broad internet queries per signal and
            # dominated the natural 25-minute cycle. Refresh stays authoritative
            # by using direct providers only; failure remains health-visible.
            quote, provider_outcomes = fetch_best_odds_with_diagnostics(
                match_hint,
                timeout_s=5.0,
                allow_implied=False,
                include_websearch=False,
            )
            if diagnostics is not None:
                diagnostics.extend(provider_outcomes)
            if quote and not quote.no_bet_flag:
                cached = (quote.h2h_a, quote.h2h_b, quote.source, quote.source_tier)
            else:
                cached = (None, None, "", 0)
        except Exception as e:
            _log.debug("[refresher] tennis merger error: %s", e)
            cached = (None, None, "", 0)
        if quote_cache is not None:
            quote_cache[cache_key] = cached

    odds = cached[0] if market in ("home", "ah-1.5_a") else cached[1]
    if odds and odds > 1.0:
        return odds, cached[2], cached[3]

    return None, "", 0


# ---------------------------------------------------------------------------
# Core refresh loop
# ---------------------------------------------------------------------------

def _load_signals() -> list[dict]:
    """Load all signals from docs/data/signals.json (football + tennis)."""
    if not _SIGNALS_JSON.exists():
        return []
    try:
        data = json.loads(_SIGNALS_JSON.read_text())
    except Exception:
        return []
    signals = []
    for section in ("football", "tennis"):
        section_data = data.get(section, [])
        if isinstance(section_data, list):
            signals.extend(section_data)
    return signals


def _retry_after(signal: dict, state_entry: dict | None, now: datetime) -> tuple[str, int]:
    """Persist bounded, visible backoff after an unsuccessful provider cycle."""
    kickoff = signal.get("kickoff", "")
    minutes_to_kickoff = float("inf")
    if kickoff:
        try:
            minutes_to_kickoff = (datetime.fromisoformat(kickoff.replace("Z", "+00:00")) - now).total_seconds() / 60
        except ValueError:
            pass
    if signal.get("event_status") in ("AWAITING_START", "DELAYED"):
        minutes_to_kickoff = max(minutes_to_kickoff, 0.0)
    failures = int((state_entry or {}).get("refresh_failure_count", 0) or 0) + 1
    base_minutes = max(_refresh_interval_minutes(minutes_to_kickoff), 15)
    retry_minutes = min(60, base_minutes * (2 ** min(failures - 1, 2)))
    return (now + timedelta(minutes=retry_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"), failures


def _publish_staged_signals(stage_dir: Path) -> None:
    paths = sorted(str(path.relative_to(stage_dir)) for path in (stage_dir / "docs" / "data").glob("signals*.json"))
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
        raise RuntimeError(f"staged signal publication failed (exit {result.returncode})")


def _retry_is_active(state_entry: dict | None) -> bool:
    if not state_entry or not state_entry.get("retry_after_ts"):
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(state_entry["retry_after_ts"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


def run_refresh(dry_run: bool = False) -> dict:
    """Refresh odds for all signals that are due.

    Returns summary dict: {refreshed, skipped, failed, elapsed_s}
    """
    t0 = time.monotonic()
    signals = _load_signals()
    if not signals:
        _log.info("[refresher] no signals to refresh")
        return {"refreshed": 0, "skipped": 0, "failed": 0, "elapsed_s": 0.0}

    odds_state = load_odds_state()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    refreshed = skipped = failed = 0
    due_by_sport = {"football": 0, "tennis": 0}
    refreshed_by_sport = {"football": 0, "tennis": 0}
    failed_by_sport = {"football": 0, "tennis": 0}
    retry_deferred = 0
    tennis_quote_cache: dict[tuple[str, str, str], tuple[float | None, float | None, str, int]] = {}
    tennis_provider_diagnostics: list[dict] = []
    failed_provider_examples: list[dict] = []

    for sig in signals:
        sport = sig.get("sport", "football")
        match = sig.get("match", "")
        market = sig.get("market", "")
        kickoff = sig.get("kickoff", "")

        sid = sig.get("signal_id") or make_signal_id(sport, match, market, kickoff)
        state_entry = odds_state.get(sid)

        if not _is_refresh_due(sig, state_entry):
            skipped += 1
            retry_deferred += int(_retry_is_active(state_entry))
            continue

        due_by_sport.setdefault(sport, 0)
        due_by_sport[sport] += 1

        _log.debug("[refresher] refreshing %s | %s | %s", sport, match, market)

        if sport == "tennis":
            signal_diagnostics: list[dict] = []
            current_odds, source, tier = _refresh_tennis(sig, tennis_quote_cache, signal_diagnostics)
            tennis_provider_diagnostics.extend(signal_diagnostics)
        else:
            current_odds, source, tier = _refresh_football(sig)

        if current_odds is None:
            # Use cached odds as fallback if still fresh enough
            cached = state_entry.get("current_odds") if state_entry else None
            cached_ts = state_entry.get("odds_ts") if state_entry else None
            status = evaluate_signal_status(sig, cached, cached_ts)
            _log.debug("[refresher] no fresh odds for %s → status=%s", match, status)
            if not dry_run:
                # Sidecar stores current_ev_pct in PERCENT; update_odds_state multiplies
                # its input by 100. Convert back to decimal to avoid 100× per-cycle
                # escalation on repeated refresh failures (produced 1e+59 values in prod).
                cached_ev_pct = state_entry.get("current_ev_pct") if state_entry else None
                cached_ev_decimal = (cached_ev_pct / 100.0) if cached_ev_pct is not None else None
                # Always persist lifecycle status so JS filter can exclude non-ACTIVE signals
                retry_after_ts, failure_count = _retry_after(sig, state_entry, datetime.now(timezone.utc))
                update_odds_state(
                    sid,
                    current_odds=cached,
                    odds_ts=cached_ts,
                    odds_source=state_entry.get("odds_source") if state_entry else None,
                    odds_fetch_tier=state_entry.get("odds_fetch_tier") if state_entry else 0,
                    signal_status=status,
                    current_ev_pct=cached_ev_decimal,
                    retry_after_ts=retry_after_ts,
                    refresh_failure_count=failure_count,
                )
            failed += 1
            failed_by_sport.setdefault(sport, 0)
            failed_by_sport[sport] += 1
            if sport == "tennis" and len(failed_provider_examples) < 5:
                failed_provider_examples.append({
                    "match": match,
                    "tournament": sig.get("tournament", ""),
                    "providers": signal_diagnostics,
                })
            continue

        ev = compute_current_ev(float(sig.get("model_prob", 0)), current_odds)
        status: SignalStatus = evaluate_signal_status(sig, current_odds, now_str)

        if not dry_run:
            update_odds_state(
                sid,
                current_odds=current_odds,
                odds_ts=now_str,
                odds_source=source,
                odds_fetch_tier=tier,
                signal_status=status,
                current_ev_pct=ev,
                retry_after_ts=None,
                refresh_failure_count=0,
            )
            _log.info(
                "[refresher] %s | %s | %s → odds=%.2f ev=%.1f%% status=%s src=%s",
                sport, match, market, current_odds, ev * 100, status, source,
            )

        refreshed += 1
        refreshed_by_sport.setdefault(sport, 0)
        refreshed_by_sport[sport] += 1

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
            if not stage_parent.is_absolute() or resolved_stage_parent == resolved_root or resolved_root in resolved_stage_parent.parents:
                raise RuntimeError("odds refresh staging directory must be external to the active checkout")
            stage_parent.mkdir(parents=True, exist_ok=True)
            stage_root = Path(tempfile.mkdtemp(prefix="odds-refresh-", dir=stage_parent))
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
                    os.environ["SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR"] = previous_stage
            _log.info("[refresher] signals staged and republished")
        except Exception as exc:
            _log.warning("[refresher] republish failed: %s", exc)
            summary["publication_failed"] = True

    return summary
