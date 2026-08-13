// P0-A: Canonical validation contract.
// Pure functions — no Cloudflare Worker APIs.
// Imported by worker.js AND by Node.js tests.

export const TERMINAL_STATUSES = new Set([
  'FINISHED', 'CANCELLED', 'POSTPONED', 'ABANDONED', 'TERMINATED', 'COMPLETED',
]);
export const LIVE_STATUSES = new Set(['LIVE', 'IN_PROGRESS']);

// P1.5-H canonical freshness: 30 minutes
export const MAX_ODDS_AGE_MS = 1800 * 1000;
// MAX_EV = 0.40 -> 40pp
export const MAX_EV_PCT = 40.0;
export const MAX_ACTIVE_BETS = 3;
// Absolute euro ceiling (regardless of bankroll)
export const WORKER_ABSOLUTE_MAX = 25.0;
// Hard bankroll cap: 5%
export const MAX_STAKE_PCT = 0.05;
// Authoritative state must be published within 2 hours
export const AUTH_STATE_MAX_AGE_MS = 7200 * 1000;

/**
 * Validate a trusted KV signal for actionability.
 * sig must come from the trusted published signals_json KV, NOT from the client.
 * Returns {ok: boolean, reason: string}.
 */
export function validateSignalActionability(sig, nowMs = Date.now()) {
  if (!sig || typeof sig !== 'object') {
    return { ok: false, reason: 'signal is null or not an object' };
  }

  const sid = String(sig.signal_id || '').trim();
  if (!sid) return { ok: false, reason: 'signal_id missing or empty' };

  if (sig.signal_status !== 'ACTIVE') {
    return { ok: false, reason: `signal_status must be 'ACTIVE', got '${sig.signal_status}'` };
  }

  if (sig.shadow === true || sig.is_shadow === true) {
    return { ok: false, reason: 'shadow signal — not actionable' };
  }

  if (sig.unsupported === true) return { ok: false, reason: 'unsupported signal' };
  if (sig.edge_lost === true)    return { ok: false, reason: 'edge_lost' };
  if (sig.stale === true)        return { ok: false, reason: 'stale signal' };
  if (sig.no_bet_flag === true)  return { ok: false, reason: 'no_bet_flag set' };

  const odds = Number(sig.current_odds);
  if (!Number.isFinite(odds) || odds <= 1.0) {
    return { ok: false, reason: `current_odds must be > 1.0, got ${sig.current_odds}` };
  }

  const ev = Number(sig.current_ev_pct);
  if (!Number.isFinite(ev)) return { ok: false, reason: 'current_ev_pct not finite' };
  if (ev > MAX_EV_PCT) {
    return { ok: false, reason: `current_ev_pct exceeds canonical MAX_EV (${MAX_EV_PCT}%): ${ev}` };
  }
  if (ev <= -100) return { ok: false, reason: `current_ev_pct implausibly low: ${ev}` };
  if (ev <= 0)    return { ok: false, reason: `current_ev_pct must be > 0, got ${ev}` };

  const oddsTs = sig.odds_ts;
  if (!oddsTs) return { ok: false, reason: 'odds_ts missing' };
  const tsMs = new Date(oddsTs).getTime();
  if (!Number.isFinite(tsMs)) return { ok: false, reason: 'odds_ts invalid date' };
  const ageMs = nowMs - tsMs;
  if (ageMs > MAX_ODDS_AGE_MS) {
    return {
      ok: false,
      reason: `odds_ts stale: ${Math.round(ageMs / 1000)}s old (max ${MAX_ODDS_AGE_MS / 1000}s)`,
    };
  }

  const evStatus = sig.event_status;
  if (LIVE_STATUSES.has(evStatus)) {
    return { ok: false, reason: `event_status is live: ${evStatus}` };
  }
  if (TERMINAL_STATUSES.has(evStatus)) {
    return { ok: false, reason: `event_status is terminal: ${evStatus}` };
  }

  return { ok: true, reason: 'ok' };
}

/**
 * Find a signal by signal_id in the trusted published signals JSON.
 * signalsJson: the parsed KV signals object { tennis: [...], football: [...] }
 * Returns the signal object, or null if not found.
 */
export function resolveCanonicalSignal(signalsJson, signalId) {
  if (!signalsJson || !signalId) return null;
  const tennis   = Array.isArray(signalsJson.tennis)   ? signalsJson.tennis   : [];
  const football = Array.isArray(signalsJson.football) ? signalsJson.football : [];
  const id = String(signalId);
  for (const sig of [...tennis, ...football]) {
    if (sig && String(sig.signal_id || '') === id) return sig;
  }
  return null;
}

/**
 * Check that the authoritative state is fresh enough for risk enforcement.
 * publishedAt: ISO string from bankroll_state.published_at in the KV snapshot.
 * Returns {ok: boolean, reason: string}.
 *
 * Threshold: 2 hours (AUTH_STATE_MAX_AGE_MS).
 * Rationale: consume_pending_bets runs every 5 min; signal publication runs after
 * each scan; 2h is permissive enough for weekend gaps but strict enough to catch
 * a backend outage before a bet is accepted on stale risk data.
 */
export function validateAuthStateFreshness(publishedAt, nowMs = Date.now()) {
  if (!publishedAt) {
    return { ok: false, reason: 'authoritative state has no published_at — fail closed' };
  }
  const pubMs = new Date(publishedAt).getTime();
  if (!Number.isFinite(pubMs)) {
    return { ok: false, reason: 'authoritative state published_at is invalid — fail closed' };
  }
  const ageMs = nowMs - pubMs;
  if (ageMs > AUTH_STATE_MAX_AGE_MS) {
    return {
      ok: false,
      reason: `authoritative state is stale: ${Math.round(ageMs / 1000)}s old (max ${AUTH_STATE_MAX_AGE_MS / 1000}s) — fail closed`,
    };
  }
  return { ok: true, reason: 'ok' };
}

/**
 * Validate basic request body fields: source (exact match), stake, signal_id.
 * Returns {ok, error, status, source} where source is the validated string.
 */
export function validateBetBodyBasic(body) {
  const stake = Number(body.stake_eur);
  if (!Number.isFinite(stake)) {
    return { ok: false, error: 'invalid stake_eur', status: 400 };
  }
  if (stake < 0.5 || stake > WORKER_ABSOLUTE_MAX) {
    return { ok: false, error: `stake_eur out of range (0.5–${WORKER_ABSOLUTE_MAX})`, status: 400 };
  }

  // P0-A: source must be exactly "value" or "manual" — never coerce invalid input to "value"
  const rawSource = String(body.source || '').toLowerCase().trim();
  if (rawSource !== 'value' && rawSource !== 'manual') {
    return {
      ok: false,
      error: `source must be "value" or "manual", got "${String(body.source || '')}"`,
      status: 400,
    };
  }

  if (rawSource === 'value') {
    const signalId = String(body.signal_id || '').trim();
    if (!signalId) {
      return {
        ok: false,
        error: 'source=value requires signal_id — use explicit source=manual for manual bets',
        status: 400,
      };
    }
  }

  return { ok: true, error: '', status: 200, source: rawSource };
}

/**
 * Validate 5% bankroll cap.
 * Returns {ok, error}.
 */
export function validateBankrollCap(authBankroll, stake) {
  if (authBankroll === null || authBankroll === undefined || !Number.isFinite(authBankroll) || authBankroll <= 0) {
    return { ok: false, error: 'authoritative bankroll unavailable — fail closed' };
  }
  const cap = authBankroll * MAX_STAKE_PCT;
  if (stake > cap + 0.001) {
    return {
      ok: false,
      error: `stake_eur ${stake.toFixed(2)} exceeds 5% cap (${cap.toFixed(2)}) of authoritative bankroll ${authBankroll.toFixed(2)}`,
    };
  }
  return { ok: true, error: '' };
}

/**
 * Validate active bet count against canonical MAX_ACTIVE_BETS.
 * Returns {ok, error}.
 */
export function validateActiveBets(authOpenBets, pendingCount) {
  if (authOpenBets === null || authOpenBets === undefined) {
    return { ok: false, error: 'authoritative open-bet count unavailable — fail closed' };
  }
  const total = Number(authOpenBets) + Number(pendingCount);
  if (total >= MAX_ACTIVE_BETS) {
    return {
      ok: false,
      error: `max active bets (${MAX_ACTIVE_BETS}) reached — ${authOpenBets} in ledger + ${pendingCount} pending`,
    };
  }
  return { ok: true, error: '' };
}

/**
 * For source=value: check submitted odds match canonical current_odds from KV.
 * Tolerance: +-0.01 (floating-point rounding).
 * Returns {ok, reason}.
 */
export function validateOddsMatchCanonical(submittedOdds, canonicalOdds) {
  const sub = Number(submittedOdds);
  const can = Number(canonicalOdds);
  if (!Number.isFinite(sub) || !Number.isFinite(can)) {
    return { ok: false, reason: 'invalid odds values' };
  }
  if (Math.abs(sub - can) > 0.01) {
    return {
      ok: false,
      reason: `submitted odds ${sub.toFixed(2)} do not match canonical current_odds ${can.toFixed(2)} — use source=manual for custom odds`,
    };
  }
  return { ok: true, reason: 'ok' };
}
