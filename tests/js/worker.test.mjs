/**
 * P0-A Worker contract tests — Node.js 24 built-in test runner.
 * Tests execute the SAME validation functions used by worker.js (via contract.js).
 * Run: node --test tests/js/worker.test.mjs
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const CONTRACT = resolve(__dir, '../../cloudflare/contract.js');

const {
  validateBetBodyBasic,
  validateSignalActionability,
  validateBankrollCap,
  validateActiveBets,
  validateAuthStateFreshness,
  validateOddsMatchCanonical,
  resolveCanonicalSignal,
  validateCanonicalIdentity,
  MAX_ACTIVE_BETS,
  MAX_EV_PCT,
  WORKER_ABSOLUTE_MAX,
  ALLOWED_PREMATCH_STATUSES,
  MAX_CLOCK_SKEW_MS,
} = await import(CONTRACT);

// ── Helpers ───────────────────────────────────────────────────────────────────

const NOW = Date.now();
const FRESH_TS  = new Date(NOW - 5 * 60 * 1000).toISOString();   // 5 min old
const STALE_TS  = new Date(NOW - 35 * 60 * 1000).toISOString();  // 35 min old
const FRESH_PUB = new Date(NOW - 10 * 60 * 1000).toISOString();  // 10 min old (fresh auth state)
const STALE_PUB = new Date(NOW - 3 * 3600 * 1000).toISOString(); // 3 h old (stale auth state)

function validSig(overrides = {}) {
  return {
    signal_id:     'sig_001',
    signal_status: 'ACTIVE',
    shadow:        false,
    is_shadow:     false,
    unsupported:   false,
    edge_lost:     false,
    stale:         false,
    no_bet_flag:   false,
    current_odds:  2.10,
    current_ev_pct: 15.2,
    odds_ts:       FRESH_TS,
    event_status:  'PREMATCH',
    sport:         'tennis',
    match:         'Federer vs Nadal',
    market:        'home',
    fixture_key:   'federer_vs_nadal_20260813',
    league:        'wimbledon',
    ...overrides,
  };
}

function validSignalsJson(sig = validSig()) {
  return { tennis: [sig], football: [] };
}

// ── 1. validateBetBodyBasic ───────────────────────────────────────────────────

describe('validateBetBodyBasic', () => {
  test('valid value bet accepted', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'value', signal_id: 'sig_001' });
    assert.equal(r.ok, true);
    assert.equal(r.source, 'value');
  });

  test('valid manual bet accepted', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'manual' });
    assert.equal(r.ok, true);
    assert.equal(r.source, 'manual');
  });

  // Blocker 6 regression: invalid source must NOT become "value"
  test('source=robot is REJECTED, not coerced to value', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'robot', signal_id: 'sig_001' });
    assert.equal(r.ok, false, 'robot source must be rejected');
    assert.match(r.error, /source.*must.*be.*value.*manual/i);
  });

  test('source=auto is REJECTED', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'auto', signal_id: 'sig_001' });
    assert.equal(r.ok, false);
  });

  // P0-A item J regression: source must be EXACT literal — no normalization
  test('source=VALUE (uppercase) is REJECTED — strict literal contract', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'VALUE', signal_id: 'sig_001' });
    assert.equal(r.ok, false, 'uppercase VALUE must be rejected — API contract is exact');
    assert.match(r.error, /source.*must.*exactly/i);
  });

  test('source=Manual (mixed case) is REJECTED', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'Manual' });
    assert.equal(r.ok, false, 'mixed-case Manual must be rejected');
  });

  test('source=" value" (leading space) is REJECTED', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: ' value', signal_id: 'sig_001' });
    assert.equal(r.ok, false, 'source with leading space must be rejected');
  });

  test('empty source is REJECTED', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: '' });
    assert.equal(r.ok, false);
  });

  test('missing source is REJECTED', () => {
    const r = validateBetBodyBasic({ stake_eur: 5 });
    assert.equal(r.ok, false);
  });

  test('source=value without signal_id rejected', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'value' });
    assert.equal(r.ok, false);
    assert.match(r.error, /signal_id/i);
  });

  test('source=value with empty signal_id rejected', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'value', signal_id: '' });
    assert.equal(r.ok, false);
  });

  test('stake below 0.5 rejected', () => {
    const r = validateBetBodyBasic({ stake_eur: 0.4, source: 'manual' });
    assert.equal(r.ok, false);
  });

  test('stake above absolute max rejected', () => {
    const r = validateBetBodyBasic({ stake_eur: WORKER_ABSOLUTE_MAX + 1, source: 'manual' });
    assert.equal(r.ok, false);
  });

  test('stake exactly at absolute max accepted', () => {
    const r = validateBetBodyBasic({ stake_eur: WORKER_ABSOLUTE_MAX, source: 'manual' });
    assert.equal(r.ok, true);
  });
});

// ── 2. validateSignalActionability ───────────────────────────────────────────

describe('validateSignalActionability (trusted KV signal)', () => {
  test('valid signal accepted', () => {
    const r = validateSignalActionability(validSig(), NOW);
    assert.equal(r.ok, true, r.reason);
  });

  test('ACTIVE required', () => {
    const r = validateSignalActionability(validSig({ signal_status: 'EXPIRED' }), NOW);
    assert.equal(r.ok, false);
    assert.match(r.reason, /ACTIVE/i);
  });

  test('shadow signal rejected', () => {
    const r = validateSignalActionability(validSig({ shadow: true }), NOW);
    assert.equal(r.ok, false);
    assert.match(r.reason, /shadow/i);
  });

  test('unsupported signal rejected', () => {
    const r = validateSignalActionability(validSig({ unsupported: true }), NOW);
    assert.equal(r.ok, false);
  });

  test('edge_lost signal rejected', () => {
    const r = validateSignalActionability(validSig({ edge_lost: true }), NOW);
    assert.equal(r.ok, false);
  });

  test('stale signal rejected', () => {
    const r = validateSignalActionability(validSig({ stale: true }), NOW);
    assert.equal(r.ok, false);
  });

  test('no_bet_flag signal rejected', () => {
    const r = validateSignalActionability(validSig({ no_bet_flag: true }), NOW);
    assert.equal(r.ok, false);
  });

  // Blocker 2 regression: stale odds_ts
  test('stale odds_ts rejected', () => {
    const r = validateSignalActionability(validSig({ odds_ts: STALE_TS }), NOW);
    assert.equal(r.ok, false, 'stale odds_ts must be rejected');
    assert.match(r.reason, /stale/i);
  });

  test('fresh odds_ts accepted', () => {
    const r = validateSignalActionability(validSig({ odds_ts: FRESH_TS }), NOW);
    assert.equal(r.ok, true, r.reason);
  });

  // P0-A item D: future timestamp rejection
  test('odds_ts +24h in future rejected', () => {
    const futureTs = new Date(NOW + 24 * 3600 * 1000).toISOString();
    const r = validateSignalActionability(validSig({ odds_ts: futureTs }), NOW);
    assert.equal(r.ok, false, 'future odds_ts must be rejected fail-closed');
    assert.match(r.reason, /future|fail closed/i);
  });

  test('odds_ts small clock skew (within tolerance) accepted', () => {
    const slightlyFuture = new Date(NOW + MAX_CLOCK_SKEW_MS - 1000).toISOString();
    const r = validateSignalActionability(validSig({ odds_ts: slightlyFuture }), NOW);
    assert.equal(r.ok, true, `small clock skew within ${MAX_CLOCK_SKEW_MS}ms should be accepted`);
  });

  test('odds_ts just over skew tolerance rejected', () => {
    const justOver = new Date(NOW + MAX_CLOCK_SKEW_MS + 5000).toISOString();
    const r = validateSignalActionability(validSig({ odds_ts: justOver }), NOW);
    assert.equal(r.ok, false, 'timestamp over skew tolerance must be rejected');
  });

  // Blocker 2 regression: LIVE signal
  test('LIVE event_status rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'LIVE' }), NOW);
    assert.equal(r.ok, false, 'LIVE signal must be rejected');
    assert.match(r.reason, /live/i);
  });

  test('IN_PROGRESS rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'IN_PROGRESS' }), NOW);
    assert.equal(r.ok, false);
  });

  test('FINISHED (terminal) rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'FINISHED' }), NOW);
    assert.equal(r.ok, false);
  });

  // P0-A item C: explicit pre-match allowlist
  test('PREMATCH accepted', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'PREMATCH' }), NOW).ok, true);
  });

  test('AWAITING_START accepted', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'AWAITING_START' }), NOW).ok, true);
  });

  test('SCHEDULED accepted', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'SCHEDULED' }), NOW).ok, true);
  });

  test('UNKNOWN event_status rejected fail-closed', () => {
    const r = validateSignalActionability(validSig({ event_status: 'UNKNOWN' }), NOW);
    assert.equal(r.ok, false, 'UNKNOWN must fail closed');
    assert.match(r.reason, /fail closed/i);
  });

  test('null event_status rejected fail-closed', () => {
    const r = validateSignalActionability(validSig({ event_status: null }), NOW);
    assert.equal(r.ok, false, 'null event_status must fail closed');
    assert.match(r.reason, /fail closed/i);
  });

  test('missing event_status (undefined) rejected fail-closed', () => {
    const sig = validSig();
    delete sig.event_status;
    const r = validateSignalActionability(sig, NOW);
    assert.equal(r.ok, false, 'missing event_status must fail closed');
  });

  test('arbitrary string event_status rejected fail-closed', () => {
    const r = validateSignalActionability(validSig({ event_status: 'SOME_NEW_STATE' }), NOW);
    assert.equal(r.ok, false, 'arbitrary event_status must fail closed');
    assert.match(r.reason, /fail closed/i);
  });

  test('CANCELLED (terminal) rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'CANCELLED' }), NOW);
    assert.equal(r.ok, false);
  });

  test('COMPLETED (terminal) rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'COMPLETED' }), NOW);
    assert.equal(r.ok, false);
  });

  test('EV > 40% rejected', () => {
    const r = validateSignalActionability(validSig({ current_ev_pct: 41 }), NOW);
    assert.equal(r.ok, false);
    assert.match(r.reason, /MAX_EV|40/i);
  });

  test('EV exactly 40% accepted', () => {
    const r = validateSignalActionability(validSig({ current_ev_pct: MAX_EV_PCT }), NOW);
    assert.equal(r.ok, true, r.reason);
  });

  test('negative EV rejected', () => {
    const r = validateSignalActionability(validSig({ current_ev_pct: -5 }), NOW);
    assert.equal(r.ok, false);
  });

  test('current_odds <= 1.0 rejected', () => {
    const r = validateSignalActionability(validSig({ current_odds: 1.0 }), NOW);
    assert.equal(r.ok, false);
  });
});

// ── 3. resolveCanonicalSignal ─────────────────────────────────────────────────

describe('resolveCanonicalSignal', () => {
  test('known signal_id found', () => {
    const sig = validSig({ signal_id: 'sig_tennis_001' });
    const json = { tennis: [sig], football: [] };
    const found = resolveCanonicalSignal(json, 'sig_tennis_001');
    assert.deepEqual(found, sig);
  });

  // Blocker 2 regression: fake signal_id
  test('fake signal_id returns null', () => {
    const json = validSignalsJson();
    const found = resolveCanonicalSignal(json, 'fake_signal_xyz');
    assert.equal(found, null, 'fake signal_id must not be found');
  });

  test('football signal found', () => {
    const sig = { ...validSig(), signal_id: 'foot_001', sport: 'football' };
    const json = { tennis: [], football: [sig] };
    assert.deepEqual(resolveCanonicalSignal(json, 'foot_001'), sig);
  });

  test('null signalsJson returns null', () => {
    assert.equal(resolveCanonicalSignal(null, 'sig_001'), null);
  });
});

// ── 4. validateAuthStateFreshness ─────────────────────────────────────────────

describe('validateAuthStateFreshness', () => {
  test('fresh published_at accepted', () => {
    const r = validateAuthStateFreshness(FRESH_PUB, NOW);
    assert.equal(r.ok, true, r.reason);
  });

  test('stale published_at fails closed', () => {
    const r = validateAuthStateFreshness(STALE_PUB, NOW);
    assert.equal(r.ok, false, 'stale auth state must fail closed');
    assert.match(r.reason, /stale|fail closed/i);
  });

  test('missing published_at fails closed', () => {
    const r = validateAuthStateFreshness(null, NOW);
    assert.equal(r.ok, false);
    assert.match(r.reason, /fail closed/i);
  });

  test('undefined published_at fails closed', () => {
    const r = validateAuthStateFreshness(undefined, NOW);
    assert.equal(r.ok, false);
  });

  // P0-A item D: future timestamp for risk published_at
  test('risk published_at +24h in future rejected', () => {
    const futureTs = new Date(NOW + 24 * 3600 * 1000).toISOString();
    const r = validateAuthStateFreshness(futureTs, NOW);
    assert.equal(r.ok, false, 'future risk published_at must fail closed');
    assert.match(r.reason, /future|fail closed/i);
  });

  test('risk published_at small clock skew accepted', () => {
    const slightlyFuture = new Date(NOW + MAX_CLOCK_SKEW_MS - 1000).toISOString();
    const r = validateAuthStateFreshness(slightlyFuture, NOW);
    assert.equal(r.ok, true, `small skew within ${MAX_CLOCK_SKEW_MS}ms tolerance should be accepted`);
  });
});

// ── 5. validateBankrollCap ────────────────────────────────────────────────────

describe('validateBankrollCap', () => {
  test('stake within 5% cap accepted', () => {
    const r = validateBankrollCap(100, 4);
    assert.equal(r.ok, true, r.error);
  });

  test('stake exactly at 5% cap accepted', () => {
    const r = validateBankrollCap(100, 5);
    assert.equal(r.ok, true, r.error);
  });

  // Blocker: client bankroll spoof
  test('stake exceeding 5% cap rejected (spoof resistance)', () => {
    const r = validateBankrollCap(100, 20);
    assert.equal(r.ok, false, 'spoofed stake must be rejected');
    assert.match(r.error, /cap|exceed/i);
  });

  test('null bankroll fails closed', () => {
    const r = validateBankrollCap(null, 5);
    assert.equal(r.ok, false);
    assert.match(r.error, /fail closed/i);
  });

  test('zero bankroll fails closed', () => {
    const r = validateBankrollCap(0, 5);
    assert.equal(r.ok, false);
  });

  test('5% cap on different bankroll', () => {
    assert.equal(validateBankrollCap(200, 10).ok, true);
    assert.equal(validateBankrollCap(200, 11).ok, false);
  });

  test('>5% manual bet rejected (manual bets respect cap too)', () => {
    const r = validateBankrollCap(100, 10);
    assert.equal(r.ok, false, '>5% must be rejected for all bet types');
  });
});

// ── 6. validateActiveBets ─────────────────────────────────────────────────────

describe('validateActiveBets', () => {
  test('0 open + 0 pending allows new bet', () => {
    assert.equal(validateActiveBets(0, 0).ok, true);
  });

  test('2 open + 0 pending allows new bet', () => {
    assert.equal(validateActiveBets(2, 0).ok, true);
  });

  test('3 open (max) rejects new bet', () => {
    const r = validateActiveBets(MAX_ACTIVE_BETS, 0);
    assert.equal(r.ok, false, 'max active bets must block a new bet');
    assert.match(r.error, /max|3/i);
  });

  // Blocker: client count spoof
  test('authoritative 3 open rejects even if client sends 0', () => {
    const r = validateActiveBets(3, 0);
    assert.equal(r.ok, false, 'authoritative count=3 must reject regardless of client hint');
  });

  test('null authoritative count fails closed', () => {
    const r = validateActiveBets(null, 0);
    assert.equal(r.ok, false);
    assert.match(r.error, /fail closed/i);
  });
});

// ── 7. validateOddsMatchCanonical ─────────────────────────────────────────────

describe('validateOddsMatchCanonical', () => {
  test('exact match accepted', () => {
    assert.equal(validateOddsMatchCanonical(1.80, 1.80).ok, true);
  });

  test('within tolerance accepted', () => {
    assert.equal(validateOddsMatchCanonical(1.80, 1.801).ok, true);
  });

  // Blocker 4 regression: edited odds
  test('canonical 1.80, client 2.50 → REJECT', () => {
    const r = validateOddsMatchCanonical(2.50, 1.80);
    assert.equal(r.ok, false, 'edited odds must be rejected for value bets');
    assert.match(r.reason, /2\.50|1\.80|canonical/i);
  });

  test('canonical 1.80, client 1.80 → accepted', () => {
    const r = validateOddsMatchCanonical(1.80, 1.80);
    assert.equal(r.ok, true, r.reason);
  });
});

// ── 8. validateCanonicalIdentity (P0-A item A) ────────────────────────────────

describe('validateCanonicalIdentity', () => {
  test('matching identity accepted', () => {
    const sig = validSig();
    const body = { match: sig.match, market: sig.market, sport: sig.sport, fixture_key: sig.fixture_key };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, true, r.reason);
  });

  test('real signal_id + wrong match → REJECT', () => {
    const sig = validSig({ match: 'Federer vs Nadal' });
    const body = { match: 'Djokovic vs Murray', market: sig.market, sport: sig.sport };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, false, 'wrong match must be rejected');
    assert.match(r.reason, /match mismatch/i);
  });

  test('real signal_id + wrong market → REJECT', () => {
    const sig = validSig({ market: 'home' });
    const body = { match: sig.match, market: 'away', sport: sig.sport };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, false, 'wrong market must be rejected');
    assert.match(r.reason, /market mismatch/i);
  });

  test('real signal_id + wrong sport → REJECT', () => {
    const sig = validSig({ sport: 'tennis' });
    const body = { match: sig.match, market: sig.market, sport: 'football' };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, false, 'wrong sport must be rejected');
    assert.match(r.reason, /sport mismatch/i);
  });

  test('real signal_id + conflicting fixture_key → REJECT', () => {
    const sig = validSig({ fixture_key: 'federer_vs_nadal_20260813' });
    const body = { match: sig.match, market: sig.market, sport: sig.sport, fixture_key: 'djokovic_vs_murray_20260813' };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, false, 'conflicting fixture_key must be rejected');
    assert.match(r.reason, /fixture_key mismatch/i);
  });

  test('real signal_id + wrong selection → REJECT where selection applies', () => {
    const sig = validSig({ selection: 'player_a' });
    const body = { match: sig.match, market: sig.market, sport: sig.sport, selection: 'player_b' };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, false, 'wrong selection must be rejected');
    assert.match(r.reason, /selection mismatch/i);
  });

  test('real signal_id + correct canonical identity → ACCEPT', () => {
    const sig = validSig();
    const body = {
      match: sig.match,
      market: sig.market,
      sport: sig.sport,
      fixture_key: sig.fixture_key,
      selection: sig.selection || '',
    };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, true, r.reason);
  });

  test('client omits fixture_key (canonical has it) → ACCEPT', () => {
    // Client may omit optional fields — only reject on explicit mismatch
    const sig = validSig({ fixture_key: 'federer_vs_nadal_20260813' });
    const body = { match: sig.match, market: sig.market, sport: sig.sport, fixture_key: '' };
    const r = validateCanonicalIdentity(sig, body);
    assert.equal(r.ok, true, 'omitting fixture_key is allowed');
  });
});

// ── 9. Worker integration regression (P0-A item I) ────────────────────────────
// Tests the full orchestration: resolveCanonicalSignal → validateSignalActionability
// → validateCanonicalIdentity → validateOddsMatchCanonical → validateBankrollCap
// → validateActiveBets. Mirrors the actual pending-bet request path in worker.js.

describe('Worker integration — pending-bet path', () => {

  function simulatePendingBetRequest({ body, signalsData, publishedAt = FRESH_PUB, openBetsCount = 0, pendingCount = 0 }) {
    const bankrollState = { free: 90, staked: 10, published_at: publishedAt };
    const signalsJson = signalsData || validSignalsJson();
    const data = { ...signalsJson, bankroll_state: bankrollState };

    // Step 1: basic validation
    const basic = validateBetBodyBasic(body);
    if (!basic.ok) return { ok: false, stage: 'basic', reason: basic.error };

    const { source } = basic;
    const signalId = String(body.signal_id || '').trim();
    const stake = Number(body.stake_eur);
    const odds = Number(body.odds || 0);

    // Step 2: auth state freshness
    const fresh = validateAuthStateFreshness(publishedAt);
    if (!fresh.ok) return { ok: false, stage: 'freshness', reason: fresh.reason };

    // Step 3: bankroll cap
    const bankroll = Number(bankrollState.free) + Number(bankrollState.staked);
    const capR = validateBankrollCap(bankroll, stake);
    if (!capR.ok) return { ok: false, stage: 'bankroll_cap', reason: capR.error };

    // Step 4: active bet count
    const activeBetsR = validateActiveBets(openBetsCount, pendingCount);
    if (!activeBetsR.ok) return { ok: false, stage: 'active_bets', reason: activeBetsR.error };

    // Step 5 (source=value): resolve + validate canonical signal
    let storedIdentity = {};
    if (source === 'value') {
      const canonicalSig = resolveCanonicalSignal(signalsJson, signalId);
      if (!canonicalSig) return { ok: false, stage: 'resolve', reason: `signal_id '${signalId}' not found` };

      const sigR = validateSignalActionability(canonicalSig, NOW);
      if (!sigR.ok) return { ok: false, stage: 'actionability', reason: sigR.reason };

      const identR = validateCanonicalIdentity(canonicalSig, body);
      if (!identR.ok) return { ok: false, stage: 'identity', reason: identR.reason };

      const oddsR = validateOddsMatchCanonical(odds, canonicalSig.current_odds);
      if (!oddsR.ok) return { ok: false, stage: 'odds_match', reason: oddsR.reason };

      // Stored entry uses canonical identity
      storedIdentity = {
        match: canonicalSig.match,
        market: canonicalSig.market,
        sport: canonicalSig.sport,
        fixture_key: canonicalSig.fixture_key || '',
        ev_pct: canonicalSig.current_ev_pct,
      };
    }

    return { ok: true, stage: 'accepted', storedIdentity };
  }

  test('fully canonical value request → accepted with canonical stored identity', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig) });
    assert.equal(r.ok, true, `stage=${r.stage}: ${r.reason}`);
    // Stored identity must come from canonical signal
    assert.equal(r.storedIdentity.match, sig.match);
    assert.equal(r.storedIdentity.sport, sig.sport);
    assert.equal(r.storedIdentity.ev_pct, sig.current_ev_pct);
  });

  test('fake signal_id → rejected at resolve stage', () => {
    const body = { source: 'value', signal_id: 'fake_xyz', stake_eur: 5, odds: 2.10, match: 'A vs B', market: 'home', sport: 'tennis' };
    const r = simulatePendingBetRequest({ body });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'resolve');
  });

  test('real signal_id + wrong market → rejected at identity stage', () => {
    const sig = validSig({ market: 'home' });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: 'away', sport: sig.sport,
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig) });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'identity');
    assert.match(r.reason, /market mismatch/i);
  });

  test('real signal_id + wrong sport → rejected at identity stage', () => {
    const sig = validSig({ sport: 'tennis' });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: 'football',
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig) });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'identity');
  });

  test('real signal_id + stale risk state → rejected at freshness stage', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig), publishedAt: STALE_PUB });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'freshness');
  });

  test('real signal_id + stake >5% → rejected at bankroll_cap stage', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 20, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig) });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'bankroll_cap');
  });

  test('real signal_id + 3 active bets → rejected at active_bets stage', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = simulatePendingBetRequest({ body, signalsData: validSignalsJson(sig), openBetsCount: 3 });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'active_bets');
  });

  test('manual bet without source=manual → rejected at basic stage', () => {
    const body = { source: 'VALUE', signal_id: 'sig_001', stake_eur: 5, odds: 2.10, match: 'A vs B', market: 'home', sport: 'tennis' };
    const r = simulatePendingBetRequest({ body });
    assert.equal(r.ok, false);
    assert.equal(r.stage, 'basic');
  });
});
