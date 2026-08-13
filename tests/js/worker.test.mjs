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
  MAX_ACTIVE_BETS,
  MAX_EV_PCT,
  WORKER_ABSOLUTE_MAX,
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

  test('source=VALUE (uppercase) accepted', () => {
    const r = validateBetBodyBasic({ stake_eur: 5, source: 'VALUE', signal_id: 'sig_001' });
    assert.equal(r.ok, true);
    assert.equal(r.source, 'value');
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
    // bankroll=200 -> cap=10; stake=10 ok, stake=11 rejected
    assert.equal(validateBankrollCap(200, 10).ok, true);
    assert.equal(validateBankrollCap(200, 11).ok, false);
  });

  test('>5% manual bet rejected (manual bets respect cap too)', () => {
    // Cap applies regardless of source — source is a caller responsibility
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
