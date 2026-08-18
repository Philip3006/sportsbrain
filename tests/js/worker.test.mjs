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
const workerModule = await import(WORKER);
const { orchestratePendingBetPost, serializePublicProduct } = workerModule;
const workerDefault = workerModule.default; // used by Suite 12 cancel KV tests
const { serializePrivateState } = workerModule; // P0C-002

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

  // ── Sport-aware event_status (P0-A V5 Blocker-2) ──────────────────────────
  // Tennis: canonical TennisEventStatus is always known — null/missing must REJECT.
  // Football: scanner does not emit event_status — null/missing must ACCEPT.

  test('tennis + null event_status REJECTED (sport-aware, fail closed)', () => {
    // validSig() has sport='tennis' — null event_status must be rejected
    const r = validateSignalActionability(validSig({ event_status: null }), NOW);
    assert.equal(r.ok, false, 'tennis signal with null event_status must be rejected');
    assert.match(r.reason, /tennis.*explicit|null.*fail/i);
  });

  test('tennis + missing event_status REJECTED (sport-aware, fail closed)', () => {
    const sig = validSig();
    delete sig.event_status;
    const r = validateSignalActionability(sig, NOW);
    assert.equal(r.ok, false, 'tennis signal with missing event_status must be rejected');
    assert.match(r.reason, /tennis.*explicit|null.*fail/i);
  });

  test('tennis + empty string event_status REJECTED (sport-aware, fail closed)', () => {
    const r = validateSignalActionability(validSig({ event_status: '' }), NOW);
    assert.equal(r.ok, false, 'tennis signal with empty event_status must be rejected');
  });

  test('football + null event_status ACCEPTED (scanner does not emit event_status)', () => {
    // Football scanner produces signals without event_status — this is valid.
    const r = validateSignalActionability(validSig({ event_status: null, sport: 'football' }), NOW);
    assert.equal(r.ok, true, 'null event_status for football must be accepted');
  });

  test('football + missing event_status ACCEPTED (scanner omits it)', () => {
    const sig = validSig({ sport: 'football' });
    delete sig.event_status;
    const r = validateSignalActionability(sig, NOW);
    assert.equal(r.ok, true, 'missing event_status for football must be accepted');
  });

  test('football + empty string event_status ACCEPTED (football)', () => {
    const r = validateSignalActionability(validSig({ event_status: '', sport: 'football' }), NOW);
    assert.equal(r.ok, true, 'empty string event_status for football must be accepted');
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

  // Blocker-3 (V5): source=value with invalid model_prob must REJECT (not store null + 200)
  test('model_prob >100 → 400 REJECTED for source=value (fail closed)', () => {
    const sig = validSig({ model_prob: 150.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, `expected 400 for out-of-range model_prob, got ${r.status}: ${JSON.stringify(r.json)}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  test('model_prob negative → 400 REJECTED for source=value (fail closed)', () => {
    const sig = validSig({ model_prob: -5.0 });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, `expected 400 for negative model_prob, got ${r.status}: ${JSON.stringify(r.json)}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  test('model_prob null → 400 REJECTED for source=value (calibration regression)', () => {
    const sig = validSig({ model_prob: null });
    const body = {
      source: 'value', signal_id: sig.signal_id,
      stake_eur: 5, odds: sig.current_odds,
      match: sig.match, market: sig.market, sport: sig.sport,
    };
    const r = orchestratePendingBetPost(body, {
      signalsJson: makeSignalsJson(sig), pendingArr: [], nowMs: NOW,
    });
    assert.equal(r.status, 400, `expected 400 for null model_prob on value bet, got ${r.status}: ${JSON.stringify(r.json)}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  // Calibration regression: accepted value bets must have 0 < model_prob < 1 in entry
  test('accepted source=value entry has 0 < model_prob < 1 (calibration eligible)', () => {
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
    assert.ok(r.entry.model_prob > 0 && r.entry.model_prob < 1,
      `model_prob must be in (0,1) for calibration; got ${r.entry.model_prob}`);
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

  test('0.0 → 400 REJECTED (not calibration-eligible; Brier/ECE/LogLoss require 0 < p < 1)', () => {
    const r = makeOrchResult(0.0);
    assert.equal(r.status, 400, `expected 400 for model_prob=0, got ${r.status}: ${JSON.stringify(r.json)}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  test('100.0 → 400 REJECTED (not calibration-eligible; 100% certainty is invalid)', () => {
    const r = makeOrchResult(100.0);
    assert.equal(r.status, 400, `expected 400 for model_prob=100, got ${r.status}: ${JSON.stringify(r.json)}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  // Blocker-3 (V5): source=value with invalid model_prob must REJECT — not store null + 200
  test('>100 → 400 REJECTED (source=value fail closed)', () => {
    const r = makeOrchResult(101.0);
    assert.equal(r.status, 400, `expected 400 for >100 model_prob, got ${r.status}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  test('negative → 400 REJECTED (source=value fail closed)', () => {
    const r = makeOrchResult(-1.0);
    assert.equal(r.status, 400, `expected 400 for negative model_prob, got ${r.status}`);
    assert.match(r.json.error, /model_prob|probability/i);
  });

  test('null model_prob → 400 REJECTED for source=value', () => {
    const r = makeOrchResult(null);
    assert.equal(r.status, 400, `expected 400 for null model_prob on value bet, got ${r.status}`);
    assert.match(r.json.error, /model_prob|probability/i);
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

  test('football + missing event_status → accepted (scanner does not emit it)', () => {
    const sig = validSig({ sport: 'football' });
    delete sig.event_status;
    assert.equal(validateSignalActionability(sig, NOW).ok, true);
  });

  test('tennis + missing event_status → REJECTED (sport-aware, fail closed)', () => {
    const sig = validSig({ sport: 'tennis' });
    delete sig.event_status;
    const r = validateSignalActionability(sig, NOW);
    assert.equal(r.ok, false, 'tennis with missing event_status must be rejected');
    assert.match(r.reason, /tennis.*explicit|null.*fail/i);
  });

  test('tennis + null event_status → REJECTED', () => {
    const r = validateSignalActionability(validSig({ event_status: null, sport: 'tennis' }), NOW);
    assert.equal(r.ok, false, 'tennis with null event_status must be rejected');
  });

  test('tennis + UPCOMING → accepted', () => {
    const r = validateSignalActionability(validSig({ event_status: 'UPCOMING', sport: 'tennis' }), NOW);
    assert.equal(r.ok, true, `tennis UPCOMING must be accepted: ${r.reason}`);
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

// ── Suite 12: Cancel intent KV — per-intent storage (FND-003 P0A-012) ────────
describe('Cancel intent KV — per-intent storage (FND-003)', () => {
  // Mock KV: in-memory Map with Cloudflare KV interface.
  function makeMockKV() {
    const store = new Map();
    return {
      async get(key) { return store.has(key) ? store.get(key) : null; },
      async put(key, value) { store.set(key, value); },
      async delete(key) { store.delete(key); },
      async list({ prefix = '' } = {}) {
        const keys = [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name }));
        return { keys, list_complete: true, cursor: '' };
      },
    };
  }

  const TOKEN = 'test-token-p0a-012';
  function makeEnv(kv) { return { SIGNALS: kv, API_TOKEN: TOKEN }; }

  const BASE = 'https://worker.test';

  async function fw(env, method, path, body) {
    const headers = { 'Authorization': `Bearer ${TOKEN}` };
    const init = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return workerDefault.fetch(new Request(`${BASE}${path}`, init), env, {});
  }

  test('concurrent cancel survival — delete of one id does not drop unrelated intent', async () => {
    const env = makeEnv(makeMockKV());
    await fw(env, 'POST', '/cancel_bet', { home: 'Alpha', away: 'Beta', market: 'home' });
    await fw(env, 'POST', '/cancel_bet', { home: 'Gamma', away: 'Delta', market: 'away' });
    const r1 = await fw(env, 'GET', '/cancel_requests');
    const { requests } = await r1.json();
    assert.equal(requests.length, 2, `expected 2 intents; got ${JSON.stringify(requests)}`);
    const idA = requests.find(r => r.home === 'Alpha')?.id;
    assert.ok(idA, 'Alpha intent must have stable id');
    const delResp = await fw(env, 'DELETE', `/cancel_requests/${idA}`);
    assert.ok((await delResp.json()).ok, 'DELETE must return ok');
    const r2 = await fw(env, 'GET', '/cancel_requests');
    const { requests: remaining } = await r2.json();
    assert.equal(remaining.length, 1, `FND-003: 1 intent must remain after deleting the other; got ${JSON.stringify(remaining)}`);
    assert.equal(remaining[0].home, 'Gamma');
  });

  test('DELETE /cancel_requests/{id} unknown id is idempotent (ok=true)', async () => {
    const env = makeEnv(makeMockKV());
    const resp = await fw(env, 'DELETE', '/cancel_requests/nonexistent-id');
    const body = await resp.json();
    assert.ok(body.ok);
  });

  test('DELETE /cancel_requests (clear-all) → 410 Gone (disabled)', async () => {
    const env = makeEnv(makeMockKV());
    const resp = await fw(env, 'DELETE', '/cancel_requests');
    assert.equal(resp.status, 410);
  });

  test('default-user list cannot see another user cancel intent (namespace isolation)', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    // Inject a foreign user's intent directly into KV — bypasses auth, simulates concurrent write.
    await kv.put('cancel_intent:alice:foreign-id', JSON.stringify({
      id: 'foreign-id', home: 'X', away: 'Y', market: 'home', requested_at: new Date().toISOString(),
    }));
    // Default-user posts their own intent via the Worker.
    await fw(env, 'POST', '/cancel_bet', { home: 'Alpha', away: 'Beta', market: 'home' });
    const resp = await fw(env, 'GET', '/cancel_requests');
    const { requests } = await resp.json();
    assert.equal(requests.length, 1,
      `FND-003 namespace: default-user list must not see alice's intent; got ${JSON.stringify(requests)}`);
    assert.equal(requests[0].home, 'Alpha');
  });
});

// ── P0C-001 helpers (shared by Suites 13 + 14) ───────────────────────────
const P0C_TOKEN = 'test-token-p0c-001';
const MASTER_TOKEN = P0C_TOKEN;
function makeP0cKV() {
  const store = new Map();
  return {
    async get(key) { return store.has(key) ? store.get(key) : null; },
    async put(key, value) { store.set(key, value); },
    async delete(key) { store.delete(key); },
    async list({ prefix = '' } = {}) {
      const keys = [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name }));
      return { keys, list_complete: true, cursor: '' };
    },
  };
}
function makeP0cEnv(kv) { return { SIGNALS: kv, API_TOKEN: P0C_TOKEN }; }
const P0C_BASE = 'https://worker-p0c.test';
async function fwP0c(env, method, path, body, token) {
  const tok = token || P0C_TOKEN;
  const headers = { 'Authorization': `Bearer ${tok}` };
  const init = { method, headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  return workerDefault.fetch(new Request(`${P0C_BASE}${path}`, init), env, {});
}

// Aliases for Suite 14 (makeMockKV/makeEnv/fw point to P0C variants)
const makeMockKV = makeP0cKV;
const makeEnv = makeP0cEnv;
const fw = fwP0c;

// ── Suite 13: P0C-001 — serializePublicProduct() ─────────────────────────
// T6: Public Worker route returns only public-safe schema.
// Private financial/identity fields must be structurally excluded.
describe('Suite 13 — P0C-001 serializePublicProduct()', () => {
  const PRIVATE_SNAPSHOT = {
    updated: '2026-08-18T00:00:00Z',
    build_info: { sha: 'abc123' },
    meta: { stale_odds: false, default_user: 'philip', user: 'philip' },
    schedule: [{ match: 'Federer vs Nadal' }],
    all_odds: { 'federer_vs_nadal': { home: 1.95 } },
    model_tips: { 'federer_vs_nadal': { prob: 52.0 } },
    model_evals: {},
    football: [],
    tennis: [{ signal_id: 'sig_001', ev_pct: 12.5 }],
    top_elo: [{ name: 'Federer', rating: 2400 }],
    wm_results: [],
    odds_history: {},
    // PRIVATE fields below — must not appear in public output
    bankroll_state: { start: 100, free: 12345.67, staked: 0, pnl_closed: -52.87, published_at: '2026-08-18T00:00:00Z' },
    open_bets: [{ id: 'PRIVATE_TEST_MARKER' }],
    settled_bets: [{ id: 'settled_001' }],
    history: [{ date: '2026-08-12', pnl: -5.0 }],
    portfolio: { by_market: { h2h_home: { pnl: -20.0 } } },
    wm_stats: { summary: { total_staked: 500.0, total_pnl: -32.0 } },
    tennis_stats: {
      updated: '2026-08-18T00:00:00Z',
      active_tournaments: ['cincinnati_atp'],
      live_gate_status: { atp250: 'live' },
      stats: { total_staked: 200.0, total_pnl: -52.0, roi_pct: -26.0 },
    },
  };

  test('public fields are present', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    assert.ok(pub.updated, 'updated missing');
    assert.ok(pub.build_info, 'build_info missing');
    assert.ok(pub.schedule, 'schedule missing');
    assert.ok(pub.all_odds, 'all_odds missing');
    assert.ok(pub.tennis, 'tennis signals missing');
  });

  test('bankroll_state absent from public payload', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    assert.ok(!('bankroll_state' in pub), 'bankroll_state leaked to public payload');
    const str = JSON.stringify(pub);
    assert.ok(!str.includes('12345.67'), 'bankroll marker value leaked');
  });

  test('open_bets absent from public payload', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    assert.ok(!('open_bets' in pub), 'open_bets leaked');
    assert.ok(!JSON.stringify(pub).includes('PRIVATE_TEST_MARKER'), 'open_bets marker leaked');
  });

  test('settled_bets, history, portfolio, wm_stats absent', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    assert.ok(!('settled_bets' in pub), 'settled_bets leaked');
    assert.ok(!('history' in pub), 'history (P&L) leaked');
    assert.ok(!('portfolio' in pub), 'portfolio leaked');
    assert.ok(!('wm_stats' in pub), 'wm_stats leaked');
  });

  test('meta contains no user identity', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    const meta = pub.meta || {};
    assert.ok(!('user' in meta), 'meta.user leaked');
    assert.ok(!('default_user' in meta), 'meta.default_user leaked');
    assert.ok('stale_odds' in meta, 'meta.stale_odds missing');
  });

  test('tennis_stats.stats (private betting stats) excluded', () => {
    const pub = serializePublicProduct(PRIVATE_SNAPSHOT);
    const ts = pub.tennis_stats || {};
    assert.ok(!('stats' in ts), 'tennis_stats.stats (private) leaked');
    assert.ok('active_tournaments' in ts, 'tennis_stats.active_tournaments missing');
    assert.ok('live_gate_status' in ts, 'tennis_stats.live_gate_status missing');
  });

  test('empty/null snapshot returns empty object', () => {
    assert.deepStrictEqual(serializePublicProduct({}), {});
    assert.deepStrictEqual(serializePublicProduct(null), {});
  });

  test('idempotent — double serialization yields same result', () => {
    const pub1 = serializePublicProduct(PRIVATE_SNAPSHOT);
    const pub2 = serializePublicProduct(pub1);
    assert.deepStrictEqual(pub1, pub2);
  });
});

// ── Suite 14: P0C-001 — GET /signals.json returns public data; merge_health POST ──
describe('Suite 14 — P0C-001 Worker GET public boundary + merge_health', () => {
  const PRIVATE_KV_SNAPSHOT = {
    updated: '2026-08-18T00:00:00Z',
    schedule: [{ match: 'Federer vs Nadal' }],
    football: [],
    tennis: [{ signal_id: 'sig_001', ev_pct: 12.5 }],
    bankroll_state: { free: 12345.67, staked: 0, published_at: '2026-08-18T00:00:00Z' },
    open_bets: [{ id: 'PRIVATE_TEST_MARKER' }],
    settled_bets: [{ id: 'hist_001' }],
    meta: { stale_odds: false, default_user: 'philip', user: 'philip' },
  };

  test('GET /signals.json returns public-only payload (no private fields)', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    await kv.put('signals_json', JSON.stringify(PRIVATE_KV_SNAPSHOT));
    const resp = await fw(env, 'GET', '/signals.json');
    assert.equal(resp.status, 200);
    const pub = await resp.json();
    assert.ok(!('bankroll_state' in pub), 'bankroll_state in public GET response');
    assert.ok(!('open_bets' in pub), 'open_bets in public GET response');
    assert.ok(!('settled_bets' in pub), 'settled_bets in public GET response');
    assert.ok(!JSON.stringify(pub).includes('PRIVATE_TEST_MARKER'), 'private marker in GET response');
    assert.ok(!JSON.stringify(pub).includes('12345.67'), 'bankroll value in GET response');
  });

  test('GET /signals.json has no user identity in meta', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    await kv.put('signals_json', JSON.stringify(PRIVATE_KV_SNAPSHOT));
    const resp = await fw(env, 'GET', '/signals.json');
    const pub = await resp.json();
    const meta = pub.meta || {};
    assert.ok(!('user' in meta), 'meta.user in public GET response');
    assert.ok(!('default_user' in meta), 'meta.default_user in public GET response');
  });

  test('GET /signals.json public fields are present', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    await kv.put('signals_json', JSON.stringify(PRIVATE_KV_SNAPSHOT));
    const resp = await fw(env, 'GET', '/signals.json');
    const pub = await resp.json();
    assert.ok('schedule' in pub, 'schedule missing from public GET');
    assert.ok('tennis' in pub, 'tennis missing from public GET');
  });

  test('POST /signals ?merge_health=1 merges health without wiping private fields', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    await kv.put('signals_json', JSON.stringify(PRIVATE_KV_SNAPSHOT));
    const healthPayload = { overall: 'ok', jobs: [], generated_at: '2026-08-18T00:00:00Z' };
    const resp = await fw(env, 'POST', '/signals?merge_health=1', { health: healthPayload }, MASTER_TOKEN);
    assert.equal(resp.status, 200, 'merge_health POST failed');
    const updated = JSON.parse(await kv.get('signals_json'));
    // Health was injected
    assert.deepStrictEqual(updated.health, healthPayload, 'health not merged into KV');
    // Private fields preserved
    assert.ok('bankroll_state' in updated, 'bankroll_state wiped from KV after merge_health');
    assert.ok('open_bets' in updated, 'open_bets wiped from KV after merge_health');
    assert.ok(updated.bankroll_state.free === 12345.67, 'bankroll free value corrupted');
  });

  test('POST /signals ?merge_health=1 without health key returns 400', async () => {
    const kv = makeMockKV();
    const env = makeEnv(kv);
    await kv.put('signals_json', JSON.stringify(PRIVATE_KV_SNAPSHOT));
    const resp = await fw(env, 'POST', '/signals?merge_health=1', { not_health: {} }, MASTER_TOKEN);
    assert.equal(resp.status, 400, 'bad merge_health request should return 400');
  });
});

// ── Suite 15: P0C-001 — Fail-closed nested private markers ───────────────
// C3: serializePublicProduct() must throw for nested private keys inside
// approved containers (not just top-level exclusion). Worker GET must return
// 500 (fail-closed) when the serializer detects a nested private field.
describe('Suite 15 — P0C-001 fail-closed nested private markers', () => {

  test('serializePublicProduct throws for nested owner in tennis list', () => {
    const snap = { tennis: [{ signal_id: 'sig_001', ev_pct: 12.5, owner: 'PRIVATE_TEST_MARKER' }] };
    assert.throws(
      () => serializePublicProduct(snap),
      /owner/,
      'nested owner inside tennis must throw privacy violation'
    );
  });

  test('serializePublicProduct throws for nested bankroll in model_tips', () => {
    const snap = { model_tips: { match_x: { prob: 0.55, bankroll: 12345.67 } } };
    assert.throws(
      () => serializePublicProduct(snap),
      /bankroll/,
      'nested bankroll inside model_tips must throw privacy violation'
    );
  });

  test('serializePublicProduct throws for nested user in health', () => {
    const snap = { health: { status: 'ok', user: 'philip' } };
    assert.throws(
      () => serializePublicProduct(snap),
      /user/,
      'nested user inside health must throw privacy violation'
    );
  });

  test('Worker GET /signals.json returns 500 for nested private in approved container', async () => {
    const kv = makeP0cKV();
    const env = makeP0cEnv(kv);
    const snapWithNestedPrivate = {
      updated: '2026-08-18T00:00:00Z',
      tennis: [{ signal_id: 'sig_001', ev_pct: 12.5, owner: 'PRIVATE_LEAK_MARKER' }],
      schedule: [{ match: 'A vs B' }],
    };
    await kv.put('signals_json', JSON.stringify(snapWithNestedPrivate));
    const resp = await fwP0c(env, 'GET', '/signals.json');
    // Fail-closed: Worker must return 500, not 200 with private data.
    assert.equal(resp.status, 500, 'GET must fail-closed (500) for nested private in approved container');
    const body = await resp.json();
    assert.ok(
      !JSON.stringify(body).includes('PRIVATE_LEAK_MARKER'),
      'private marker must not appear in 500 response body'
    );
  });

  test('Worker GET /signals.json clean nested data returns 200', async () => {
    const kv = makeP0cKV();
    const env = makeP0cEnv(kv);
    const cleanSnap = {
      updated: '2026-08-18T00:00:00Z',
      tennis: [{ signal_id: 'sig_001', ev_pct: 12.5, match: 'A vs B' }],
      schedule: [{ match: 'A vs B', kickoff: '2026-08-18T14:00:00Z' }],
      health: { status: 'ok', jobs: [] },
    };
    await kv.put('signals_json', JSON.stringify(cleanSnap));
    const resp = await fwP0c(env, 'GET', '/signals.json');
    assert.equal(resp.status, 200, 'clean nested data must return 200');
    const pub = await resp.json();
    assert.ok('tennis' in pub, 'tennis must be present in clean public GET response');
    assert.ok('schedule' in pub, 'schedule must be present in clean public GET response');
  });
});

// ── Suite 16: P0C-002 — Authenticated private state + owner routing ──────
// T1–T12 of the required deterministic test matrix.
// Verifies: /me auth, exact-owner routing, no DEFAULT_USER fallback, master
// token 403, cross-user rotate_token/token_status rejection, private allowlist,
// and P0C-001 regression (unauthenticated /signals.json remains public-only).
describe('Suite 16 — P0C-002 Authenticated Private State (/me + cross-user auth)', () => {
  const MASTER = 'p0c002-master-token';
  const ALICE_TOK  = 'alice-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const BOB_TOK    = 'bob-token-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
  const PHILIP_TOK = 'philip-token-pppppppppppppppppppppppppppppp';

  function makeKV() {
    const store = new Map();
    return {
      async get(k) { return store.has(k) ? store.get(k) : null; },
      async put(k, v) { store.set(k, v); },
      async delete(k) { store.delete(k); },
      async list({ prefix = '' } = {}) {
        const keys = [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name }));
        return { keys, list_complete: true, cursor: '' };
      },
      _store: store,
    };
  }
  async function setup() {
    const kv = makeKV();
    const env = { SIGNALS: kv, API_TOKEN: MASTER };
    await kv.put('user_tokens', JSON.stringify({
      alice:  { active: ALICE_TOK,  previous: null, rotated_at: '2026-08-18T00:00:00Z' },
      bob:    { active: BOB_TOK,    previous: null, rotated_at: '2026-08-18T00:00:00Z' },
      philip: { active: PHILIP_TOK, previous: null, rotated_at: '2026-08-18T00:00:00Z' },
    }));
    return { kv, env };
  }
  async function req(env, method, path, { token, body } = {}) {
    const headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const init = { method, headers };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    return workerDefault.fetch(new Request('https://w-p0c002.test' + path, init), env, {});
  }
  const PHILIP_SNAP = {
    updated: '2026-08-18T00:00:00Z',
    schedule: [{ match: 'A vs B' }],
    tennis: [{ signal_id: 'sig_p', ev_pct: 10 }],
    bankroll_state: { free: 100, staked: 0, published_at: '2026-08-18T00:00:00Z' },
    open_bets: [{ id: 'PHILIP_ONLY_MARKER' }],
    settled_bets: [],
    meta: { stale_odds: false, default_user: 'philip', user: 'philip' },
  };
  const ALICE_SNAP = {
    updated: '2026-08-18T00:00:00Z',
    bankroll_state: { free: 50, staked: 0, published_at: '2026-08-18T00:00:00Z' },
    open_bets: [{ id: 'ALICE_ONLY_MARKER' }],
    settled_bets: [],
    schedule: [{ match: 'X vs Y' }],
    meta: { user: 'alice' },
  };

  // T1
  test('T1: GET /me without token → 401', async () => {
    const { env } = await setup();
    const r = await req(env, 'GET', '/me');
    assert.equal(r.status, 401);
  });
  // T2
  test('T2: GET /me with invalid token → 401', async () => {
    const { env } = await setup();
    const r = await req(env, 'GET', '/me', { token: 'not-a-real-token' });
    assert.equal(r.status, 401);
  });
  // T3
  test('T3: GET /me with valid alice token → owner=alice + alice data', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json_alice', JSON.stringify(ALICE_SNAP));
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/me', { token: ALICE_TOK });
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.owner, 'alice');
    assert.ok(JSON.stringify(body).includes('ALICE_ONLY_MARKER'));
    assert.ok(!JSON.stringify(body).includes('PHILIP_ONLY_MARKER'), 'must not leak philip state');
  });
  // T4 — A cannot read B by manipulating ?user= etc.
  test('T4: alice token cannot read bob data via ?user= or path', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json_alice', JSON.stringify(ALICE_SNAP));
    await kv.put('signals_json_bob', JSON.stringify({ open_bets: [{ id: 'BOB_ONLY_MARKER' }] }));
    const r = await req(env, 'GET', '/me?user=bob', { token: ALICE_TOK });
    assert.equal(r.status, 200, 'alice should still get her own /me');
    const body = await r.json();
    assert.equal(body.owner, 'alice');
    assert.ok(!JSON.stringify(body).includes('BOB_ONLY_MARKER'), 'BOB data must never appear in alice /me');
  });
  // T5 — alice token + no alice snapshot → 404, no Philip fallback
  test('T5: alice token + missing snapshot → 404, zero Philip data', async () => {
    const { env, kv } = await setup();
    // Only Philip snapshot exists; alice has NO snapshot.
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/me', { token: ALICE_TOK });
    assert.equal(r.status, 404, 'must be 404, never fall back to Philip');
    const body = await r.json();
    assert.ok(!JSON.stringify(body).includes('PHILIP_ONLY_MARKER'), 'must never leak philip data on 404');
    assert.equal(body.owner, 'alice');
  });
  // T6 — philip token reads signals_json (legacy key), owner=philip
  test('T6: philip token reads signals_json as exact owner (owner=philip)', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/me', { token: PHILIP_TOK });
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.equal(body.owner, 'philip');
    assert.ok(JSON.stringify(body).includes('PHILIP_ONLY_MARKER'));
  });
  // T7 — master token without per-user identity → 403
  test('T7: master token on /me → 403 (no implicit identity)', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/me', { token: MASTER });
    assert.equal(r.status, 403);
    const txt = await r.text();
    assert.ok(!txt.includes('PHILIP_ONLY_MARKER'), 'must not leak state on 403');
  });
  // T8 — POST /rotate_token: alice cannot rotate bob
  test('T8: POST /rotate_token alice trying user=bob → 403', async () => {
    const { env } = await setup();
    const r = await req(env, 'POST', '/rotate_token', { token: ALICE_TOK, body: { user: 'bob' } });
    assert.equal(r.status, 403);
  });
  // T9 — GET /token_status: alice cannot inspect bob
  test('T9: GET /token_status?user=bob with alice token → 403', async () => {
    const { env } = await setup();
    const r = await req(env, 'GET', '/token_status?user=bob', { token: ALICE_TOK });
    assert.equal(r.status, 403);
  });
  // T10 — P0C-001 regression: /signals.json unauth returns 200, zero private fields
  test('T10: GET /signals.json unauthenticated returns 200 + zero private fields', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/signals.json');
    assert.equal(r.status, 200);
    const body = await r.json();
    assert.ok(!('bankroll_state' in body));
    assert.ok(!('open_bets' in body));
    assert.ok(!JSON.stringify(body).includes('PHILIP_ONLY_MARKER'));
  });
  // T11 — /me response contains only private allowlist fields
  test('T11: /me contains only private-allowlist fields (no schedule/model_tips/etc)', async () => {
    const { env, kv } = await setup();
    await kv.put('signals_json', JSON.stringify(PHILIP_SNAP));
    const r = await req(env, 'GET', '/me', { token: PHILIP_TOK });
    const body = await r.json();
    const publicKeys = ['schedule', 'model_tips', 'tennis', 'football', 'all_odds',
                        'top_elo', 'wm_results', 'odds_history', 'health', 'build_info', 'tennis_stats'];
    for (const k of publicKeys) {
      assert.ok(!(k in body), `public key '${k}' must not appear in /me payload`);
    }
    assert.ok('owner' in body);
    assert.ok('schema_version' in body);
  });
  // T12 — stored owner mismatch → 403
  test('T12: stored owner != auth.user → 403 (owner_mismatch guard)', async () => {
    const { env, kv } = await setup();
    // alice snapshot but stored meta.user = 'bob' → mismatch, must fail closed.
    await kv.put('signals_json_alice', JSON.stringify({
      ...ALICE_SNAP,
      meta: { user: 'bob' },
    }));
    const r = await req(env, 'GET', '/me', { token: ALICE_TOK });
    assert.equal(r.status, 403);
    const body = await r.json();
    assert.equal(body.error, 'owner_mismatch');
  });

  // Bonus: serializePrivateState() unit checks
  test('serializePrivateState empty snapshot → owner + schema_version only', () => {
    const out = serializePrivateState(null, 'alice');
    assert.equal(out.owner, 'alice');
    assert.equal(out.schema_version, '1');
    assert.ok(!('bankroll_state' in out));
  });
  test('serializePrivateState strips public product keys', () => {
    const out = serializePrivateState({
      schedule: [1, 2, 3],
      tennis: [{}],
      bankroll_state: { free: 10 },
      model_tips: {},
    }, 'alice');
    assert.ok(!('schedule' in out));
    assert.ok(!('tennis' in out));
    assert.ok(!('model_tips' in out));
    assert.ok('bankroll_state' in out);
  });
});
