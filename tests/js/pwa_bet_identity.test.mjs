import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const betsSource = readFileSync(resolve(__dir, '../../docs/js/bets.js'), 'utf8');
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const indexSource = readFileSync(resolve(__dir, '../../docs/index.html'), 'utf8');
const deepLinkStart = betsSource.indexOf('function _openBetModalForBetId(');
const deepLinkEnd = betsSource.indexOf('\n// ── Render open bets tab ──', deepLinkStart);
assert.ok(deepLinkStart >= 0 && deepLinkEnd > deepLinkStart);

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
    'data-shadow', 'data-is-shadow', 'data-unsupported',
    'data-edge-lost', 'data-stale', 'data-no-bet-flag',
  ]) {
    assert.match(viewsSource, new RegExp(attr.replaceAll('-', '\\-')));
  }
  assert.match(viewsSource, /:\s*\(!_isValueActionable\)/);
  assert.match(viewsSource, /isActionableValueSignal\(s, _currentBankroll\(\), \(_openBets \|\| \[\]\)\.length\)/);
  assert.match(viewsSource, /class="place-bet-btn"/);
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


test('core betting frontend assets use the same cache-bust release', () => {
  const release = '20261002-detail-bet-cta';
  for (const asset of ['app', 'views', 'bets']) {
    assert.match(indexSource, new RegExp(`src="js/\${asset}\\.js\\?v=\${release}"`));
  }
});
