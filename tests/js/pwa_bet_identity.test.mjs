import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const betsSource = readFileSync(resolve(__dir, '../../docs/js/bets.js'), 'utf8');
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const deepLinkStart = betsSource.indexOf('function _openBetModalForBetId(');
const deepLinkEnd = betsSource.indexOf('\n// ── Render open bets tab ──', deepLinkStart);
assert.ok(deepLinkStart >= 0 && deepLinkEnd > deepLinkStart);
const homeOddsStart = viewsSource.indexOf('function _renderHomeOddsButton(');
const homeOddsEnd = viewsSource.indexOf('\nfunction renderHome()', homeOddsStart);
assert.ok(homeOddsStart >= 0 && homeOddsEnd > homeOddsStart);
const actionabilityEnd = betsSource.indexOf('\nfunction computeSafeStake');
assert.ok(actionabilityEnd > 0);

function renderHomeOdds(signal, odds = signal?.odds, game = {}, scanEv = null) {
  const context = {
    _currentBankroll: () => 1000,
    _openBets: [],
    esc: (value) => String(value ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
  };
  vm.createContext(context);
  vm.runInContext(`${betsSource.slice(0, actionabilityEnd)}\n` +
    `${viewsSource.slice(homeOddsStart, homeOddsEnd)}\n` +
    'globalThis.renderHomeOdds = _renderHomeOddsButton;', context);
  const html = context.renderHomeOdds(
    signal ? { odds, ev: signal.ev_pct ?? null, model_prob: signal.model_prob, signal } : { odds, ev: scanEv },
    'home',
    { home: 'Player A', away: 'Player B', kickoff: signal?.kickoff || '', sport: signal?.sport || 'football', ...game },
  );
  return { html };
}

function runDeepLink(signal) {
  const button = { dataset: {} };
  let opened = null;
  const context = {
    _signals: [signal],
    document: {
      querySelector: () => null,
      createElement: () => button,
    },
    navTo: () => {},
    _openBetModalFromBtn: (candidate) => { opened = candidate; },
    decodeURIComponent,
  };
  vm.createContext(context);
  vm.runInContext(`${betsSource.slice(deepLinkStart, deepLinkEnd)}\n` +
    'globalThis.openDeepLink = _openBetModalForBetId;', context);
  context.openDeepLink(`${encodeURIComponent(signal.match)}:${signal.market}`);
  return opened;
}

const canonicalTennis = {
  match: 'Player A vs Player B',
  market: 'home',
  odds: 2.1,
  stake_eur: 5,
  ev_pct: 15,
  model_prob: 0.52,
  fair_prob: 0.48,
  confidence: 'HIGH',
  kickoff: '2026-10-02T18:00:00Z',
  sport: 'tennis',
  signal_id: 'tennis:player-a-player-b:home',
  signal_status: 'ACTIVE',
  current_odds: 2.12,
  current_ev_pct: 14.8,
  odds_ts: '2026-10-01T19:55:00Z',
  event_status: 'UPCOMING',
  fixture_key: 'tennis:player-a-v-player-b:2026-10-02',
  league: 'atp',
  shadow: false,
  is_shadow: false,
  unsupported: false,
  edge_lost: false,
  stale: false,
  no_bet_flag: false,
};

test('standard Tennis signal-card path carries the canonical actionability fields', () => {
  for (const attr of [
    'data-signal-id', 'data-signal-status', 'data-current-odds',
    'data-current-ev', 'data-odds-ts', 'data-event-status',
    'data-fixture-key', 'data-league', 'data-source',
  ]) {
    assert.match(viewsSource, new RegExp(attr.replaceAll('-', '\\-')));
  }
  assert.match(viewsSource, /:\s*\(!_isValueActionable\)/);
});

test('deep-link path preserves the same canonical Tennis identity and gates', () => {
  const button = runDeepLink(canonicalTennis);
  assert.ok(button);
  assert.deepEqual(button.dataset, {
    match: canonicalTennis.match,
    market: canonicalTennis.market,
    odds: '2.1',
    stake: '5',
    ev: '15',
    modelProb: '0.52',
    fairProb: '0.48',
    confidence: 'HIGH',
    kickoff: canonicalTennis.kickoff,
    sport: 'tennis',
    signalId: canonicalTennis.signal_id,
    signalStatus: 'ACTIVE',
    currentOdds: '2.12',
    currentEv: '14.8',
    oddsTs: canonicalTennis.odds_ts,
    eventStatus: 'UPCOMING',
    fixtureKey: canonicalTennis.fixture_key,
    league: 'atp',
    shadow: 'false',
    isShadow: 'false',
    unsupported: 'false',
    edgeLost: 'false',
    stale: 'false',
    noBetFlag: 'false',
    source: 'value',
  });
});

test('deep-link path does not manufacture identity for a missing signal_id', () => {
  const button = runDeepLink({ ...canonicalTennis, signal_id: '' });
  assert.ok(button);
  assert.equal(button.dataset.signalId, '');
  assert.equal(button.dataset.source, 'value');
  assert.match(betsSource, /signal_id missing or empty/);
});

test('Home Tennis actionable odds button carries the exact canonical signal identity', () => {
  const freshTennis = { ...canonicalTennis, odds_ts: new Date(Date.now() - 60_000).toISOString() };
  const { html } = renderHomeOdds(freshTennis);
  assert.match(html, /<button /);
  assert.match(html, /data-source="value"/);
  for (const [name, value] of [
    ['signal-id', canonicalTennis.signal_id],
    ['signal-status', canonicalTennis.signal_status],
    ['current-odds', canonicalTennis.current_odds],
    ['current-ev', canonicalTennis.current_ev_pct],
    ['odds-ts', freshTennis.odds_ts],
    ['event-status', canonicalTennis.event_status],
    ['fixture-key', canonicalTennis.fixture_key],
    ['league', canonicalTennis.league],
    ['sport', canonicalTennis.sport],
    ['model-prob', canonicalTennis.model_prob],
    ['fair-prob', canonicalTennis.fair_prob],
    ['confidence', canonicalTennis.confidence],
    ['kickoff', canonicalTennis.kickoff],
    ['shadow', canonicalTennis.shadow],
    ['is-shadow', canonicalTennis.is_shadow],
    ['unsupported', canonicalTennis.unsupported],
    ['edge-lost', canonicalTennis.edge_lost],
    ['stale', canonicalTennis.stale],
    ['no-bet-flag', canonicalTennis.no_bet_flag],
  ]) {
    assert.match(html, new RegExp(`data-${name}="${String(value).replace('.', '\\.')}`));
  }
  assert.doesNotMatch(html, /data-signal-id=""/);
});

test('Home Tennis canonical but non-actionable odds remain display-only', () => {
  const stale = { ...canonicalTennis, stale: true, current_ev_pct: 14.8 };
  const { html } = renderHomeOdds(stale, 2.1);
  assert.doesNotMatch(html, /<button /);
  assert.match(html, /canonical-value-unavailable/);
  assert.doesNotMatch(html, /data-source="value"/);
  assert.match(html, /2\.12/);
});

test('Home Nations League canonical actionability keeps the existing CTA/display-only split', () => {
  const actionable = {
    ...canonicalTennis,
    is_nations_league_value: true,
    sport: 'football',
    odds_ts: new Date(Date.now() - 60_000).toISOString(),
  };
  const value = renderHomeOdds(actionable);
  assert.match(value.html, /<button /);
  assert.match(value.html, /data-source="value"/);
  const unavailable = renderHomeOdds({ ...actionable, stale: true });
  assert.doesNotMatch(unavailable.html, /<button /);
  assert.match(unavailable.html, /nl-value-unavailable/);
});

test('Home odds without a canonical signal preserve manual behavior', () => {
  const { html } = renderHomeOdds(null, 2.1, { sport: 'football' }, 15);
  assert.match(html, /<button /);
  assert.match(html, /data-source="manual"/);
  assert.match(html, /data-signal-id=""/);
});
