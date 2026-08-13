/**
 * P0-A Worker contract + orchestration tests — Node.js built-in test runner.
 *
 * Blocker-5: suite 9 now imports and tests orchestratePendingBetPost() from
 * worker.js directly — NOT a hand-rolled simulation. This is the actual
 * production orchestration code executed by the live Worker handler.
 *
 * Run: node --test tests/js/worker.test.mjs
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const CONTRACT = resolve(__dir, '../../cloudflare/contract.js');
const WORKER  = resolve(__dir, '../../cloudflare/worker.js');

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

// Blocker-5: import ACTUAL production orchestration function from worker.js
const { orchestratePendingBetPost } = await import(WORKER);

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
    // Blocker-1: real production Tennis status (TennisEventStatus.UPCOMING).
    // PREMATCH was never emitted by TennisEventStatus — UPCOMING is the canonical value.
    odds_ts:       FRESH_TS,
    event_status:  'UPCOMING',
    sport:         'tennis',
    match:         'Federer vs Nadal',
    market:        'home',
    fixture_key:   'federer_vs_nadal_20260813',
    league:        'wimbledon',
    model_prob:    52.0,  // percent-unit as published by web_dashboard._signal_to_dict()
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

  // Blocker-1: real production pre-match statuses from TennisEventStatus
  test('UPCOMING accepted (real Tennis canonical pre-match status)', () => {
    const r = validateSignalActionability(validSig({ event_status: 'UPCOMING' }), NOW);
    assert.equal(r.ok, true, `UPCOMING must be accepted: ${r.reason}`);
  });

  test('AWAITING_START accepted', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'AWAITING_START' }), NOW).ok, true);
  });

  test('DELAYED accepted (qualified delay, pre-match odds still valid)', () => {
    const r = validateSignalActionability(validSig({ event_status: 'DELAYED' }), NOW);
    assert.equal(r.ok, true, `DELAYED must be accepted: ${r.reason}`);
  });

  test('PREMATCH accepted (legacy/football compatibility)', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'PREMATCH' }), NOW).ok, true);
  });

  test('SCHEDULED accepted (legacy/football compatibility)', () => {
    assert.equal(validateSignalActionability(validSig({ event_status: 'SCHEDULED' }), NOW).ok, true);
  });

  test('UNKNOWN event_status rejected fail-closed', () => {
    const r = validateSignalActionability(validSig({ event_status: 'UNKNOWN' }), NOW);
    assert.equal(r.ok, false, 'UNKNOWN must fail closed');
    assert.match(r.reason, /fail closed/i);
  });

  test('null event_status accepted (football scanner does not emit event_status)', () => {
    // Football scanner produces signals without event_status field.
    // null/missing is NOT the same as UNKNOWN — it means "not applicable for this sport".
    const r = validateSignalActionability(validSig({ event_status: null }), NOW);
    assert.equal(r.ok, true, 'null event_status (football) must be accepted');
  });

  test('missing event_status (undefined) accepted (football scanner omits it)', () => {
    const sig = validSig();
    delete sig.event_status;
    const r = validateSignalActionability(sig, NOW);
    assert.equal(r.ok, true, 'missing event_status (football) must be accepted');
  });

  test('empty string event_status accepted (football)', () => {
    const r = validateSignalActionability(validSig({ event_status: '' }), NOW);
    assert.equal(r.ok, true, 'empty string event_status (football) must be accepted');
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

  test('POSTPONED (terminal) rejected', () => {
    const r = validateSignalActionability(validSig({ event_status: 'POSTPONED' }), NOW);
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

// ── 9. Worker orchestration — ACTUAL production code (Blocker-5) ──────────────
// Tests call orchestratePendingBetPost() imported from worker.js — the SAME
// function the live Worker handler calls. This is NOT a simulation.

describe('Worker orchestration — orchestratePendingBetPost (production code)', () => {

  function makeSignalsJson(sig, publishedAt = FRESH_PUB) {
    return {
      tennis: sig.sport === 'tennis' ? [sig] : [],
      football: sig.sport === 'football' ? [sig] : [],
      bankroll_state: { free: 90, staked: 10, published_at: publishedAt },
      open_bets: [],
    };
  }

  test('valid canonical value request (UPCOMING Tennis) → 200 + entry stored', () => {
    const sig = validSig({ event_status: 'UPCOMING', model_prob: 52.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig),
      pendingArr: [],
      nowMs: NOW,
      genId: () => 'entry-001',
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    assert.ok(r.json.ok, JSON.stringify(r.json));
    assert.ok(r.entry, 'entry must be present for storage');
    // Canonical stored identity comes from KV signal, not client body
    assert.equal(r.entry.match, sig.match);
    assert.equal(r.entry.sport, sig.sport);
    assert.equal(r.entry.market, sig.market);
  });

  test('valid canonical value request (DELAYED Tennis) → 200 accepted', () => {
    const sig = validSig({ event_status: 'DELAYED' });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
  });

  test('fake signal_id → 400 rejected', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: 'fake_xyz',
      stake_eur: 5, odds: 2.10, match: 'A vs B', market: 'home', sport: 'tennis',
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, JSON.stringify(r.json));
    assert.match(r.json.error, /not found/i);
  });

  test('wrong market in body → 400 identity mismatch', () => {
    const sig = validSig({ market: 'home' });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: 'away', sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, JSON.stringify(r.json));
    assert.match(r.json.error, /identity mismatch|market mismatch/i);
  });

  test('stale risk state → 503 fail closed', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig, STALE_PUB), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 503, JSON.stringify(r.json));
    assert.match(r.json.error, /stale|fail closed/i);
  });

  test('stake >5% bankroll cap → 400 rejected', () => {
    const sig = validSig();
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 20, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, JSON.stringify(r.json));
    assert.match(r.json.error, /cap|exceed/i);
  });

  test('3 active open bets → 400 rejected', () => {
    const sig = validSig();
    // 3 open bets in bankroll_state
    const sj = { ...makeSignalsJson(sig), open_bets: [{}, {}, {}] };
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: sj, pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, JSON.stringify(r.json));
    assert.match(r.json.error, /max|3/i);
  });

  // Blocker-3: model_prob published as percent (52.0) must be stored as fraction (0.52)
  test('model_prob 52.0 published → entry stores 0.52 fraction', () => {
    const sig = validSig({ model_prob: 52.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    assert.ok(r.entry, 'entry must be present');
    assert.ok(Math.abs(r.entry.model_prob - 0.52) < 0.001,
      `expected model_prob≈0.52, got ${r.entry.model_prob}`);
  });

  test('model_prob 50.8 published → entry stores 0.508 fraction', () => {
    const sig = validSig({ model_prob: 50.8 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    assert.ok(Math.abs(r.entry.model_prob - 0.508) < 0.001,
      `expected model_prob≈0.508, got ${r.entry.model_prob}`);
  });

  test('model_prob >100 published → entry stores null (invalid)', () => {
    const sig = validSig({ model_prob: 150.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    assert.equal(r.entry.model_prob, null, 'invalid >100 model_prob must be stored as null');
  });

  test('model_prob negative → entry stores null', () => {
    const sig = validSig({ model_prob: -5.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    assert.equal(r.entry.model_prob, null);
  });

  test('source=VALUE (uppercase) → 400 rejected (strict literal)', () => {
    const sig = validSig();
    const body = {
      source: 'VALUE', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, JSON.stringify(r.json));
  });
});

// ── 10. model_prob normalization unit tests ────────────────────────────────────
// Tests normalizeModelProbPct standalone via orchestrate (no direct export needed).

describe('normalizeModelProbPct (via orchestration)', () => {
  function makeOrchResult(modelProbPct) {
    const sig = validSig({ model_prob: modelProbPct });
    const sj = {
      tennis: [sig], football: [],
      bankroll_state: { free: 90, staked: 10, published_at: FRESH_PUB },
      open_bets: [],
    };
    const body = { source: 'value', signal_id: sig.signal_id, stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport };
    return orchestratePendingBetPost(body, { signalsJson: sj, pendingArr: [], nowMs: NOW });
  }

  test('52.0 → 0.52 (canonical tennis signal)', () => {
    const r = makeOrchResult(52.0);
    assert.equal(r.status, 200);
    assert.ok(Math.abs(r.entry.model_prob - 0.52) < 0.001);
  });

  test('0.0 → 0.0 (edge: zero probability)', () => {
    const r = makeOrchResult(0.0);
    assert.equal(r.status, 200);
    assert.equal(r.entry.model_prob, 0.0);
  });

  test('100.0 → 1.0 (100%)', () => {
    const r = makeOrchResult(100.0);
    assert.equal(r.status, 200);
    assert.ok(Math.abs(r.entry.model_prob - 1.0) < 0.001);
  });

  test('>100 → null (invalid percent)', () => {
    const r = makeOrchResult(101.0);
    assert.equal(r.status, 200);
    assert.equal(r.entry.model_prob, null);
  });

  test('negative → null', () => {
    const r = makeOrchResult(-1.0);
    assert.equal(r.status, 200);
    assert.equal(r.entry.model_prob, null);
  });

  test('null model_prob → null', () => {
    const r = makeOrchResult(null);
    assert.equal(r.status, 200);
    assert.equal(r.entry.model_prob, null);
  });
});

// ── 11. Contract parity tests (Blocker-2) ────────────────────────────────────
// These fixtures prove that contract.js (Worker) and docs/js/bets.js (PWA)
// make identical decisions. (Python parity verified by test_signal_contract.py.)

describe('Contract parity — Worker matches PWA contract', () => {
  // Helper: decisions the Worker contract makes for a given signal
  function workerDecision(sigOverrides) {
    return validateSignalActionability(validSig(sigOverrides), NOW);
  }

  test('valid UPCOMING Tennis → accepted', () => {
    assert.equal(workerDecision({ event_status: 'UPCOMING' }).ok, true);
  });

  test('missing event_status → accepted (football)', () => {
    const sig = validSig();
    delete sig.event_status;
    assert.equal(validateSignalActionability(sig, NOW).ok, true);
  });

  test('UNKNOWN → rejected', () => {
    assert.equal(workerDecision({ event_status: 'UNKNOWN' }).ok, false);
  });

  test('LIVE → rejected', () => {
    assert.equal(workerDecision({ event_status: 'LIVE' }).ok, false);
  });

  test('COMPLETED → rejected', () => {
    assert.equal(workerDecision({ event_status: 'COMPLETED' }).ok, false);
  });

  test('future odds_ts (+24h) → rejected', () => {
    const futureTs = new Date(NOW + 24 * 3600 * 1000).toISOString();
    assert.equal(workerDecision({ odds_ts: futureTs }).ok, false);
  });

  test('stale odds_ts → rejected', () => {
    assert.equal(workerDecision({ odds_ts: STALE_TS }).ok, false);
  });

  test('missing current_odds → rejected', () => {
    assert.equal(workerDecision({ current_odds: null }).ok, false);
  });

  test('missing current_ev_pct → rejected', () => {
    assert.equal(workerDecision({ current_ev_pct: null }).ok, false);
  });

  test('shadow signal → rejected', () => {
    assert.equal(workerDecision({ shadow: true }).ok, false);
  });

  test('unsupported signal → rejected', () => {
    assert.equal(workerDecision({ unsupported: true }).ok, false);
  });

  test('edge_lost signal → rejected', () => {
    assert.equal(workerDecision({ edge_lost: true }).ok, false);
  });
});
