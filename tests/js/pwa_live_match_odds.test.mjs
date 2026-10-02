import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = fileURLToPath(new URL('.', import.meta.url));
const viewsSource = readFileSync(resolve(here, '../../docs/js/views.js'), 'utf8');
const indexSource = readFileSync(resolve(here, '../../docs/index.html'), 'utf8');

function matchKey(home, away) {
  return `${String(home).toLowerCase().replace(/\s+/g, ' ').trim()} vs ${String(away).toLowerCase().replace(/\s+/g, ' ').trim()}`;
}

function helpers(currentRecords = {}) {
  const context = {
    Date,
    Number,
    Object,
    Array,
    String,
    matchKey,
    _openBets: [],
    _currentBankroll: () => 100,
    _isCurrentQuote: (quote, nowMs) => {
      const odds = Number(quote?.odds ?? quote?.current_odds);
      const timestamp = Date.parse(quote?.odds_ts || '');
      return Number.isFinite(odds) && odds > 1 && Number.isFinite(timestamp) &&
        quote?.current !== false && quote?.freshness !== 'stale' &&
        nowMs - timestamp >= -60_000 && nowMs - timestamp <= 30 * 60_000;
    },
    _findCurrentMatchOdds: (home, away) => Object.values(currentRecords).find((record) =>
      matchKey(record.home, record.away) === matchKey(home, away)) || null,
    isActionableValueSignal: (signal) => ({ ok: signal?.signal_status === 'ACTIVE' }),
    fmtKickoff: () => '03.10 · 05:00',
    esc: (value) => String(value ?? '').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
  };
  vm.createContext(context);
  const start = viewsSource.indexOf('function _currentMatchOddsRecordIsFresh(');
  const end = viewsSource.indexOf('\nfunction _dedupeHomeGames', start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(`${viewsSource.slice(start, end)}\n` +
    'globalThis.matchOddsHelpers = { fresh: _currentMatchOddsRecordIsFresh, quote: _tennisScheduleQuote, row: _tennisMoreGameRow, bindRows: _bindTennisMoreGameRows, actionable: _homeValueSignalIsActionable, homeMap: _currentOddsHomeMap };', context);
  return context.matchOddsHelpers;
}

const now = Date.parse('2026-10-02T13:30:00Z');

function record(overrides = {}) {
  return {
    sport: 'tennis',
    home: 'Katerina Siniakova',
    away: 'Elina Svitolina',
    outcomes: { home: 2.48, away: 1.49 },
    odds_ts: '2026-10-02T13:27:25Z',
    source: 'tennis_explorer',
    current: true,
    freshness: 'current',
    ...overrides,
  };
}

test('fresh match-level odds replace both scan quotes and are labelled with their source', () => {
  const snapshot = record();
  const api = helpers({ fixture: snapshot });
  const quote = api.quote({
    home: snapshot.home,
    away: snapshot.away,
    odds_home: 2.25,
    odds_away: 1.57,
  }, now);

  assert.deepEqual(JSON.parse(JSON.stringify(quote)), {
    home: 2.48,
    away: 1.49,
    freshness: 'current',
    source: 'tennis_explorer',
  });
  const html = api.row({
    home: snapshot.home,
    away: snapshot.away,
    kickoff: '2026-10-03T03:00:00Z',
    odds_home: 2.25,
    odds_away: 1.57,
  }, now);
  assert.match(html, /2\.48/);
  assert.match(html, /1\.49/);
  assert.match(html, /AKTUELL · TENNIS_EXPLORER/);
  assert.doesNotMatch(html, /SCAN-QUOTE/);
});

test('stale or missing match-level odds remain explicitly scan-only', () => {
  const stale = record({
    home: 'Jelena Ostapenko',
    away: 'Paula Badosa',
    outcomes: { home: 2.35, away: 1.53 },
    odds_ts: '2026-10-02T12:55:00Z',
    current: false,
    freshness: 'stale',
  });
  const api = helpers({ fixture: stale });
  const game = {
    home: 'Jelena Ostapenko',
    away: 'Paula Badosa',
    kickoff: '2026-10-03T03:00:00Z',
    odds_home: 2.08,
    odds_away: 1.75,
  };
  const quote = api.quote(game, now);
  assert.equal(quote.freshness, 'scan');
  assert.equal(quote.home, 2.08);
  assert.equal(quote.away, 1.75);
  const html = api.row(game, now);
  assert.match(html, /SCAN-QUOTE/);
  assert.doesNotMatch(html, /AKTUELL/);

  const missingApi = helpers();
  assert.equal(missingApi.quote({ home: game.home, away: game.away }, now).freshness, 'missing');
});

test('unsignaled tennis schedule rows are keyboard-focusable detail controls', () => {
  const row = helpers().row({
    home: 'Jelena Ostapenko',
    away: 'Paula Badosa',
    kickoff: '2026-10-03T03:00:00Z',
  }, now);
  assert.match(row, /class="tennis-more-game-row" role="button" tabindex="0"/);
  assert.match(row, /class="tennis-more-game-team">Jelena Ostapenko/);
  assert.match(row, /class="tennis-more-game-team">Paula Badosa/);
  let clickHandler;
  let keyHandler;
  const fakeRow = {
    querySelectorAll: () => [{ textContent: ' Jelena Ostapenko ' }, { textContent: ' Paula Badosa ' }],
    addEventListener: (type, handler) => {
      if (type === 'click') clickHandler = handler;
      if (type === 'keydown') keyHandler = handler;
    },
  };
  let openedMatch = null;
  const bindContext = { openMatch: (match) => { openedMatch = match; } };
  vm.createContext(bindContext);
  const bindingStart = viewsSource.indexOf('function _bindTennisMoreGameRows(');
  const bindingEnd = viewsSource.indexOf('\nfunction _homeValueSignalIsActionable', bindingStart);
  vm.runInContext(`${viewsSource.slice(bindingStart, bindingEnd)}\nglobalThis.bindRows = _bindTennisMoreGameRows;`, bindContext);
  bindContext.bindRows({ querySelectorAll: () => [fakeRow] });
  clickHandler();
  assert.equal(openedMatch, 'Jelena Ostapenko vs Paula Badosa');
  let prevented = false;
  keyHandler({ key: 'Enter', preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(openedMatch, 'Jelena Ostapenko vs Paula Badosa');
});

test('Home preserves Value identity only for actionable signals and uses fresh odds for both sides', () => {
  const sin = record();
  const vekic = record({
    home: 'Donna Vekic',
    away: 'Lin Zhu',
    outcomes: { home: 1.53, away: 2.31 },
  });
  const active = {
    sport: 'tennis',
    match: `${sin.home} vs ${sin.away}`,
    market: 'away',
    signal_id: 'signal-active',
    signal_status: 'ACTIVE',
    current_odds: 1.49,
    current_ev_pct: 10.6,
  };
  const edgeLost = {
    sport: 'tennis',
    match: `${vekic.home} vs ${vekic.away}`,
    market: 'home',
    signal_id: 'signal-edge-lost',
    signal_status: 'EDGE_LOST',
    current_odds: 1.53,
    current_ev_pct: -27.3,
  };
  const api = helpers({ sin, vekic });
  const mapped = JSON.parse(JSON.stringify(api.homeMap({ sin, vekic }, [active, edgeLost], now)));
  const activeKey = matchKey(sin.home, sin.away);
  const edgeKey = matchKey(vekic.home, vekic.away);
  assert.equal(mapped[activeKey].home.odds, 2.48);
  assert.equal(mapped[activeKey].away.odds, 1.49);
  assert.equal(mapped[activeKey].away.signal.signal_id, 'signal-active');
  assert.equal(mapped[edgeKey].home.odds, 1.53);
  assert.equal(mapped[edgeKey].home.signal, null);
  assert.equal(api.actionable(edgeLost), false);
  assert.match(viewsSource, /if \(_homeValueSignalIsActionable\(s\)\) sigCount/);
});

test('frontend cache-bust release includes match-level odds fix', () => {
  for (const asset of ['app.js', 'views.js', 'bets.js']) {
    assert.ok(indexSource.includes(`${asset}?v=20261003-value-provenance-v1`));
  }
});
