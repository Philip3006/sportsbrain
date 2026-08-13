// P0-A: Canonical validation contract.
// Pure functions — no Cloudflare Worker APIs.
// Imported by worker.js AND by Node.js tests.

export const TERMINAL_STATUSES = new Set([
  'FINISHED', 'CANCELLED', 'POSTPONED', 'ABANDONED', 'TERMINATED', 'COMPLETED',
]);
export const LIVE_STATUSES = new Set(['LIVE', 'IN_PROGRESS']);

// P0-A (item C): Explicit allowlist of pre-match states that the production backend emits.
// Any state not in this set — including null, undefined, 'UNKNOWN', or arbitrary strings —
// is rejected fail-closed when the signal carries signal_status=ACTIVE.
// Sources: TennisEventState (wave3c) and football pre-match state models.
export const ALLOWED_PREMATCH_STATUSES = new Set([
  'PREMATCH',       // canonical pre-match for both sports
  'AWAITING_START', // between announced and first point / kickoff
  'SCHEDULED',      // announced but well before start (football, some tennis feeds)
]);

// P1.5-H canonical freshness: 30 minutes
export const MAX_ODDS_AGE_MS = 1800 * 1000;
// MAX_EV = 0.40 -> 40pp
export const MAX_EV_PCT = 40.0;
export const MAX_ACTIVE_BETS = 3;
// Absolute euro ceiling (regardless of bankroll)
export const WORKER_ABSOLUTE_MAX = 25.0;
// Hard bankroll cap: 5%
export const MAX_STAKE_PCT = 0.05;

// P0-A (item E): Authoritative state must be published within 90 minutes.
// Producer: tennis_scan (9×/day at 0,2,4,6,9,12,15,18,21 UTC), consume_pending_bets
// (5-min Worker cron when bets pending), post_match_update, daily_scan.
// With the 04:00 UTC scan filling the night gap, the maximum publication gap is
// approximately 2 hours. A threshold of 90 minutes is intentionally tighter so
// that a genuine backend outage is detected before the threshold expires mid-gap.
// Failure behavior: Worker returns 503 — bet cannot be placed until state is refreshed.
export const AUTH_STATE_MAX_AGE_MS = 5400 * 1000; // 90 minutes

// P0-A (item D): Maximum allowed clock skew in either direction.
// Timestamps more than MAX_CLOCK_SKEW_MS into the future are rejected fail-closed.
// A small operational tolerance covers NTP drift between backend and Worker.
export const MAX_CLOCK_SKEW_MS = 60 * 1000; // 60 seconds

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

  // P0-A (item D): reject materially future odds_ts
  const skewMs = tsMs - nowMs;
  if (skewMs > MAX_CLOCK_SKEW_MS) {
    return {
      ok: false,
      reason: `odds_ts is ${Math.round(skewMs / 1000)}s in the future — fail closed (max skew ${MAX_CLOCK_SKEW_MS / 1000}s)`,
    };
  }

  const ageMs = nowMs - tsMs;
  if (ageMs > MAX_ODDS_AGE_MS) {
    return {
      ok: false,
      reason: `odds_ts stale: ${Math.round(ageMs / 1000)}s old (max ${MAX_ODDS_AGE_MS / 1000}s)`,
    };
  }

  // P0-A (item C): Explicit pre-match allowlist — fail closed on UNKNOWN/missing/arbitrary states.
  const evStatus = sig.event_status;
  if (LIVE_STATUSES.has(evStatus)) {
    return { ok: false, reason: `event_status is live: ${evStatus}` };
  }
  if (TERMINAL_STATUSES.has(evStatus)) {
    return { ok: false, reason: `event_status is terminal: ${evStatus}` };
  }
  if (!ALLOWED_PREMATCH_STATUSES.has(evStatus)) {
    return {
      ok: false,
      reason: `event_status '${evStatus}' not in allowed pre-match set — fail closed`,
    };
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
 * P0-A (item A): Validate that client-supplied identity fields exactly match the
 * canonical signal resolved from KV. Prevents a valid signal_id from authorizing
 * a bet on a different match/market/sport.
 *
 * Returns {ok: boolean, reason: string}.
 * Called only for source=value after resolveCanonicalSignal succeeds.
 */
export function validateCanonicalIdentity(canonicalSig, body) {
  // match: must exactly match canonical (canonical is authoritative)
  const canonMatch = String(canonicalSig.match || '').trim();
  const clientMatch = String(body.match || '').trim();
  if (canonMatch && clientMatch !== canonMatch) {
    return { ok: false, reason: `match mismatch: client '${clientMatch}' ≠ canonical '${canonMatch}'` };
  }

  // market: must exactly match canonical
  const canonMarket = String(canonicalSig.market || '').trim();
  const clientMarket = String(body.market || '').trim();
  if (canonMarket && clientMarket !== canonMarket) {
    return { ok: false, reason: `market mismatch: client '${clientMarket}' ≠ canonical '${canonMarket}'` };
  }

  // sport: must exactly match canonical
  const canonSport = String(canonicalSig.sport || '').trim();
  const clientSport = String(body.sport || '').trim();
  if (canonSport && clientSport && clientSport !== canonSport) {
    return { ok: false, reason: `sport mismatch: client '${clientSport}' ≠ canonical '${canonSport}'` };
  }

  // fixture_key: if canonical has it, client must match (or omit)
  const canonFk = String(canonicalSig.fixture_key || '').trim();
  const clientFk = String(body.fixture_key || '').trim();
  if (canonFk && clientFk && clientFk !== canonFk) {
    return { ok: false, reason: `fixture_key mismatch: client '${clientFk}' ≠ canonical '${canonFk}'` };
  }

  // selection: if canonical has it, client must match (or omit)
  const canonSel = String(canonicalSig.selection || '').trim();
  const clientSel = String(body.selection || '').trim();
  if (canonSel && clientSel && clientSel !== canonSel) {
    return { ok: false, reason: `selection mismatch: client '${clientSel}' ≠ canonical '${canonSel}'` };
  }

  return { ok: true, reason: 'ok' };
}

/**
 * Check that the authoritative state is fresh enough for risk enforcement.
 * publishedAt: ISO string from bankroll_state.published_at in the KV snapshot.
 * Returns {ok: boolean, reason: string}.
 *
 * Threshold: 90 minutes (AUTH_STATE_MAX_AGE_MS).
 * Rationale: tennis_scan publishes 9×/day (max 2h gap with 04:00 UTC entry).
 * consume_pending_bets refreshes within 5 min after any ledger change.
 * 90-min threshold is tighter than the 2h max gap, catching genuine backend
 * outages before they can exceed the threshold in all but the rarest overnight window.
 */
export function validateAuthStateFreshness(publishedAt, nowMs = Date.now()) {
  if (!publishedAt) {
    return { ok: false, reason: 'authoritative state has no published_at — fail closed' };
  }
  const pubMs = new Date(publishedAt).getTime();
  if (!Number.isFinite(pubMs)) {
    return { ok: false, reason: 'authoritative state published_at is invalid — fail closed' };
  }

  // P0-A (item D): reject materially future published_at
  const skewMs = pubMs - nowMs;
  if (skewMs > MAX_CLOCK_SKEW_MS) {
    return {
      ok: false,
      reason: `authoritative state published_at is ${Math.round(skewMs / 1000)}s in the future — fail closed`,
    };
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
 * Validate basic request body fields: source (exact literal only), stake, signal_id.
 * Returns {ok, error, status, source} where source is the validated string.
 *
 * P0-A (item J): source must be the literal string "value" or "manual".
 * No case normalization, no trimming — any deviation is rejected at the security boundary.
 */
export function validateBetBodyBasic(body) {
  const stake = Number(body.stake_eur);
  if (!Number.isFinite(stake)) {
    return { ok: false, error: 'invalid stake_eur', status: 400 };
  }
  if (stake < 0.5 || stake > WORKER_ABSOLUTE_MAX) {
    return { ok: false, error: `stake_eur out of range (0.5–${WORKER_ABSOLUTE_MAX})`, status: 400 };
  }

  // P0-A (item J): strict literal match only — no toLowerCase, no trim.
  // "VALUE", " value", "Value" are all rejected. API contract is exact.
  const rawSource = body.source;
  if (rawSource !== 'value' && rawSource !== 'manual') {
    return {
      ok: false,
      error: `source must be exactly "value" or "manual", got "${String(rawSource ?? '')}"`,
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
