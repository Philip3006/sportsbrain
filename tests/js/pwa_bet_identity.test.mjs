import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const betsSource = readFileSync(resolve(__dir, '../../docs/js/bets.js'), 'utf8');
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const appSource = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');
const cssSource = readFileSync(resolve(__dir, '../../docs/css/app.css'), 'utf8');
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
  const release = '20261002-match-level-odds';
  for (const asset of ['app', 'views', 'bets']) {
    assert.ok(
      indexSource.includes(`src="js/${asset}.js?v=${release}"`),
      `missing cache-bust release for ${asset}.js`,
    );
  }
});


test('non-actionable canonical detail signals render an explicit no-bet state', () => {
  assert.match(viewsSource, /NO BET · Edge verloren/);
  assert.match(viewsSource, /class="no-bet-status"/);
  assert.match(viewsSource, /Warum kein Bet\?/);
  assert.match(viewsSource, /Das frühere Signal hat seinen Value verloren/);
  assert.match(viewsSource, /Kein Einsatz/);
  assert.match(appSource, /SportsBrain Signale/);
  assert.match(appSource, /Kein aktuelles SportsBrain-Signal für dieses Spiel/);
  assert.match(appSource, /data-source="manual"/);
  assert.match(cssSource, /\.sig-card\.no-bet/);
  assert.match(cssSource, /\.no-bet-status/);
});

test('match detail prefers a fresh match-level primary quote over signal or scan odds', () => {
  const start = appSource.indexOf('const _MAX_MATCH_QUOTE_AGE_MS');
  const end = appSource.indexOf('\nfunction _matchPrimaryMarketsCard(', start);
  assert.ok(start >= 0 && end > start);
  const context = { Number };
  vm.createContext(context);
  vm.runInContext(`${appSource.slice(start, end)}\n` +
    'globalThis.quote = _matchQuoteForMarket;', context);

  const quote = context.quote(
    'home',
    [{ market: 'home', current_odds: 1.91, odds_ts: '2026-10-02T11:00:00Z', odds_source: 'signal' }],
    { home: 1.72 },
    {
      outcomes: { home: 2.18, away: 1.74 },
      current: true,
      freshness: 'current',
      odds_ts: '2026-10-02T11:59:00Z',
      source: 'tennis_explorer',
      bookmaker: 'consensus',
    },
    Date.parse('2026-10-02T12:00:00Z'),
  );

  assert.equal(quote.odds, 2.18);
  assert.equal(quote.freshness, 'current');
  assert.equal(quote.source, 'tennis_explorer');
  assert.equal(quote.bookmaker, 'consensus');

  const noSignalQuote = context.quote(
    'away',
    [],
    {},
    {
      outcomes: { home: 2.18, away: 1.74 },
      current: true,
      freshness: 'current',
      odds_ts: '2026-10-02T11:59:00Z',
      source: 'tennis_explorer',
    },
    Date.parse('2026-10-02T12:00:00Z'),
  );
  assert.equal(noSignalQuote.odds, 1.74);
  assert.equal(noSignalQuote.freshness, 'current');
});

test('match-level and signal quotes are time-true at render time', () => {
  const start = appSource.indexOf('const _MAX_MATCH_QUOTE_AGE_MS');
  const end = appSource.indexOf('\nfunction _matchPrimaryMarketsCard(', start);
  assert.ok(start >= 0 && end > start);
  const context = { Number, Date };
  vm.createContext(context);
  vm.runInContext(`${appSource.slice(start, end)}\n` +
    'globalThis.quote = _matchQuoteForMarket;', context);
  const now = Date.parse('2026-10-02T12:00:00Z');
  const baseSnapshot = {
    outcomes: { home: 2.18, away: 1.74 },
    current: true,
    freshness: 'current',
    source: 'the_odds_api',
  };

  assert.equal(context.quote('home', [], {}, {
    ...baseSnapshot, odds_ts: '2026-10-02T11:31:00Z',
  }, now).freshness, 'current');
  assert.equal(context.quote('home', [], {}, {
    ...baseSnapshot, odds_ts: '2026-10-02T11:29:59Z',
  }, now).freshness, 'missing');
  assert.equal(context.quote('home', [], {}, {
    ...baseSnapshot, odds_ts: '2026-10-02T12:02:00Z',
  }, now).freshness, 'missing');

  assert.equal(context.quote('home', [{
    market: 'home', current_odds: 1.91, odds_ts: '2026-10-02T11:29:59Z',
  }], {}, null, now).freshness, 'missing');
  assert.equal(context.quote('home', [{
    market: 'home', current_odds: 1.91, odds_ts: '2026-10-02T11:45:00Z',
  }], {}, null, now).freshness, 'current');
});


test('match detail is match-centric and supports manual betting independent of signals', () => {
  assert.match(appSource, /Match & Quoten/);
  assert.match(appSource, /Jede Quote kann als manuelle Wette eingetragen werden/);
  assert.match(appSource, /data-source="manual"/);
  assert.match(appSource, /class="match-bet-quote"/);
  assert.match(appSource, /Quote eingeben/);
  assert.match(appSource, /SportsBrain Signale/);
  assert.match(appSource, /match-signals-panel/);
  assert.match(appSource, /Kein aktuelles SportsBrain-Signal für dieses Spiel/);
  assert.match(appSource, /_matchPrimaryMarketsCard/);
  assert.match(appSource, /_matchSignalsSection/);
  assert.match(cssSource, /\.match-betting-overview/);
  assert.match(cssSource, /\.match-bet-quote/);
});
