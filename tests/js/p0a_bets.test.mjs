/**
 * P0-A Focused Browser Contract Tests — Node.js test runner.
 *
 * Tests the isActionableValueSignal and computeSafeStake functions from
 * docs/js/bets.js — the browser-side gates that control whether the value
 * bet modal can open and whether a stake is accepted.
 *
 * Pure functions are extracted without executing DOM initialization code.
 * Covers V5 changes: sport-aware event_status, stale odds_ts, canonical
 * odds, manual path, 5% cap, and JS exception safety.
 *
 * Run: node --test tests/js/p0a_bets.test.mjs
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const BETS_JS = resolve(__dir, '../../docs/js/bets.js');

// Extract the pure contract section from bets.js.
// isActionableValueSignal and computeSafeStake are pure functions that appear
// before any DOM-touching code (the "Deep-link" section at line ~141).
const betsSource = readFileSync(BETS_JS, 'utf8');
const splitMarker = '// ── Deep-link:';
if (!betsSource.includes(splitMarker)) {
  throw new Error(`Expected boundary marker "${splitMarker}" in docs/js/bets.js`);
}
const pureSection = betsSource.slice(0, betsSource.indexOf(splitMarker));

if (!pureSection.includes('function isActionableValueSignal')) {
  throw new Error('Could not locate isActionableValueSignal in bets.js pure section');
}
if (!pureSection.includes('function computeSafeStake')) {
  throw new Error('Could not locate computeSafeStake in bets.js pure section');
}

// Evaluate pure section in a Function scope — no DOM, no globals needed.
const { isActionableValueSignal, computeSafeStake } = new Function(
  `${pureSection}; return { isActionableValueSignal, computeSafeStake };`
)();

// ── Helpers ───────────────────────────────────────────────────────────────────

const NOW_MS = Date.now();
const FRESH_TS = new Date(NOW_MS - 5 * 60 * 1000).toISOString();   // 5 min ago — fresh
const STALE_TS = new Date(NOW_MS - 35 * 60 * 1000).toISOString();  // 35 min ago — stale

/** Minimal valid tennis UPCOMING signal (canonical production fixture). */
function validSig(overrides = {}) {
  return {
    signal_id:      'sig_001',
    signal_status:  'ACTIVE',
    shadow:         false,
    is_shadow:      false,
    unsupported:    false,
    edge_lost:      false,
    stale:          false,
    no_bet_flag:    false,
    current_odds:   2.10,
    current_ev_pct: 15.2,
    odds_ts:        FRESH_TS,
    event_status:   'UPCOMING',
    sport:          'tennis',
    ...overrides,
  };
}

// ── 1. Tennis UPCOMING canonical value signal → value modal CAN open ──────────

describe('P0-A Browser: Tennis UPCOMING — value modal CAN open', () => {
  test('canonical tennis UPCOMING → actionable (modal opens)', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: 'UPCOMING' }), 100, 0);
    assert.equal(ok, true, `UPCOMING tennis must be actionable: ${reason}`);
  });

  test('tennis AWAITING_START → actionable', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: 'AWAITING_START' }), 100, 0);
    assert.equal(ok, true, `AWAITING_START must be accepted: ${reason}`);
  });

  test('tennis DELAYED → actionable', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: 'DELAYED' }), 100, 0);
    assert.equal(ok, true, `DELAYED must be accepted: ${reason}`);
  });

  test('does not throw JS exception for valid signal', () => {
    assert.doesNotThrow(() => isActionableValueSignal(validSig(), 100, 0));
  });
});

// ── 2. Tennis missing event_status → value modal CANNOT open ──────────────────

describe('P0-A Browser: Tennis missing event_status — value modal CANNOT open', () => {
  test('missing event_status → rejected fail-closed (sport-aware V5)', () => {
    const sig = validSig();
    delete sig.event_status;
    const { ok, reason } = isActionableValueSignal(sig, 100, 0);
    assert.equal(ok, false, 'tennis with missing event_status must be rejected fail-closed');
    assert.match(reason, /tennis.*fail|fail.*closed|explicit/i);
  });

  test('null event_status → rejected fail-closed', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: null }), 100, 0);
    assert.equal(ok, false, 'tennis with null event_status must be rejected');
    assert.match(reason, /tennis.*fail|fail.*closed|explicit/i);
  });

  test('empty string event_status → rejected fail-closed', () => {
    const { ok } = isActionableValueSignal(validSig({ event_status: '' }), 100, 0);
    assert.equal(ok, false, 'tennis with empty event_status must be rejected');
  });

  test('does not throw for missing event_status', () => {
    const sig = validSig();
    delete sig.event_status;
    assert.doesNotThrow(() => isActionableValueSignal(sig, 100, 0));
  });

  test('football with missing event_status → ACCEPTED (scanner does not emit it)', () => {
    const sig = validSig({ sport: 'football' });
    delete sig.event_status;
    const { ok } = isActionableValueSignal(sig, 100, 0);
    assert.equal(ok, true, 'football missing event_status must be accepted (scanner compat)');
  });
});

// ── 3. Tennis UNKNOWN → value modal CANNOT open ───────────────────────────────

describe('P0-A Browser: Tennis UNKNOWN event_status — value modal CANNOT open', () => {
  test('UNKNOWN event_status → rejected fail-closed', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: 'UNKNOWN' }), 100, 0);
    assert.equal(ok, false, 'UNKNOWN must be rejected fail-closed');
    assert.match(reason, /fail closed/i);
  });

  test('LIVE event_status → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ event_status: 'LIVE' }), 100, 0);
    assert.equal(ok, false, 'LIVE must be rejected');
  });

  test('IN_PROGRESS event_status → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ event_status: 'IN_PROGRESS' }), 100, 0);
    assert.equal(ok, false, 'IN_PROGRESS must be rejected');
  });

  test('FINISHED (terminal) → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ event_status: 'FINISHED' }), 100, 0);
    assert.equal(ok, false);
  });

  test('arbitrary unknown state → rejected fail-closed', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ event_status: 'SOME_NEW_STATE' }), 100, 0);
    assert.equal(ok, false);
    assert.match(reason, /fail closed/i);
  });

  test('does not throw for UNKNOWN event_status', () => {
    assert.doesNotThrow(() => isActionableValueSignal(validSig({ event_status: 'UNKNOWN' }), 100, 0));
  });
});

// ── 4. Stale signal → blocked ─────────────────────────────────────────────────

describe('P0-A Browser: Stale signal — blocked', () => {
  test('stale=true flag → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ stale: true }), 100, 0);
    assert.equal(ok, false, 'stale flag must block the signal');
  });

  test('stale odds_ts (35 min old) → rejected', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ odds_ts: STALE_TS }), 100, 0);
    assert.equal(ok, false, 'stale odds_ts must be rejected');
    assert.match(reason, /stale/i);
  });

  test('fresh odds_ts (5 min old) → accepted', () => {
    const { ok } = isActionableValueSignal(validSig({ odds_ts: FRESH_TS }), 100, 0);
    assert.equal(ok, true, 'fresh odds_ts must be accepted');
  });

  test('missing odds_ts → rejected', () => {
    const sig = validSig();
    delete sig.odds_ts;
    const { ok } = isActionableValueSignal(sig, 100, 0);
    assert.equal(ok, false, 'missing odds_ts must be rejected');
  });

  test('materially future odds_ts (+24h) → rejected fail-closed', () => {
    const futureTs = new Date(NOW_MS + 24 * 3600 * 1000).toISOString();
    const { ok, reason } = isActionableValueSignal(validSig({ odds_ts: futureTs }), 100, 0);
    assert.equal(ok, false, 'future odds_ts must be rejected');
    assert.match(reason, /future|fail closed/i);
  });
});

// ── 5. Current canonical odds used ────────────────────────────────────────────

describe('P0-A Browser: Canonical odds freshness', () => {
  test('valid current_odds (2.10) → accepted', () => {
    const { ok } = isActionableValueSignal(validSig({ current_odds: 2.10 }), 100, 0);
    assert.equal(ok, true);
  });

  test('current_odds = 1.0 (at floor, not above) → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ current_odds: 1.0 }), 100, 0);
    assert.equal(ok, false, 'current_odds must be > 1.0');
  });

  test('null current_odds → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ current_odds: null }), 100, 0);
    assert.equal(ok, false, 'missing current_odds must be rejected');
  });

  test('EV > 40% (canonical ceiling) → rejected', () => {
    const { ok, reason } = isActionableValueSignal(validSig({ current_ev_pct: 41 }), 100, 0);
    assert.equal(ok, false);
    assert.match(reason, /MAX_EV|40/i);
  });

  test('EV exactly at 40% → accepted', () => {
    const { ok } = isActionableValueSignal(validSig({ current_ev_pct: 40 }), 100, 0);
    assert.equal(ok, true);
  });

  test('negative EV → rejected', () => {
    const { ok } = isActionableValueSignal(validSig({ current_ev_pct: -1 }), 100, 0);
    assert.equal(ok, false);
  });
});

// ── 6. Manual path still works ────────────────────────────────────────────────

describe('P0-A Browser: Manual path still works', () => {
  test('computeSafeStake within 5% → returns stake, capApplied=false', () => {
    const { stake, capApplied } = computeSafeStake(100, 4);
    assert.equal(capApplied, false);
    assert.equal(stake, 4);
  });

  test('computeSafeStake exactly at 5% → accepted without cap', () => {
    const { stake, capApplied } = computeSafeStake(100, 5);
    assert.equal(capApplied, false);
    assert.equal(stake, 5);
  });

  test('football signal null event_status → accepted (manual/football compat)', () => {
    const { ok } = isActionableValueSignal(validSig({ sport: 'football', event_status: null }), 100, 0);
    assert.equal(ok, true, 'football with null event_status must be accepted');
  });

  test('computeSafeStake does not throw', () => {
    assert.doesNotThrow(() => computeSafeStake(100, 10));
  });
});

// ── 7. >5% stake blocked ──────────────────────────────────────────────────────

describe('P0-A Browser: >5% stake blocked', () => {
  test('stake > 5% bankroll → capped to 5%', () => {
    const { stake, capApplied } = computeSafeStake(100, 10);
    assert.equal(capApplied, true, 'cap must be applied for stake > 5%');
    assert.ok(Math.abs(stake - 5) < 0.001, `capped stake must be 5.0, got ${stake}`);
  });

  test('stake = 5% exactly → NOT capped', () => {
    const { capApplied } = computeSafeStake(100, 5);
    assert.equal(capApplied, false);
  });

  test('stake = 5.01 → capped', () => {
    const { capApplied } = computeSafeStake(100, 5.01);
    assert.equal(capApplied, true);
  });

  test('very large stake → capped, does not throw', () => {
    assert.doesNotThrow(() => computeSafeStake(100, 999));
    const { capApplied } = computeSafeStake(100, 999);
    assert.equal(capApplied, true);
  });
});

// ── 8. Submit produces no JS exception ────────────────────────────────────────

describe('P0-A Browser: Submit path — no JS exceptions', () => {
  test('null signal → does not throw, returns {ok: false}', () => {
    assert.doesNotThrow(() => isActionableValueSignal(null, 100, 0));
    const { ok } = isActionableValueSignal(null, 100, 0);
    assert.equal(ok, false);
  });

  test('empty object signal → does not throw', () => {
    assert.doesNotThrow(() => isActionableValueSignal({}, 100, 0));
  });

  test('partial signal (only signal_id) → does not throw', () => {
    assert.doesNotThrow(() => isActionableValueSignal({ signal_id: 'x' }, 100, 0));
  });

  test('undefined bankroll → does not throw, returns {ok: false}', () => {
    assert.doesNotThrow(() => isActionableValueSignal(validSig(), undefined, 0));
    const { ok } = isActionableValueSignal(validSig(), undefined, 0);
    assert.equal(ok, false);
  });

  test('all valid canonical fields → does not throw, returns {ok: true}', () => {
    assert.doesNotThrow(() => isActionableValueSignal(validSig(), 100, 0));
    const { ok } = isActionableValueSignal(validSig(), 100, 0);
    assert.equal(ok, true);
  });

  test('tennis UPCOMING + all canonical fields → no exception + accepted', () => {
    const { ok } = isActionableValueSignal(
      validSig({ event_status: 'UPCOMING', sport: 'tennis', odds_ts: FRESH_TS }),
      100,
      0,
    );
    assert.equal(ok, true);
  });
});
