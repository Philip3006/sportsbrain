import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const appSource = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');

const NOW = Date.parse('2026-10-01T12:00:00Z');

function matchKey(home, away) {
  return `${String(home).toLowerCase().replace(/\s+/g, ' ').trim()} vs ${String(away).toLowerCase().replace(/\s+/g, ' ').trim()}`;
}

function homeHelpers(signals, schedule) {
  const context = {
    Date,
    Number,
    Math,
    String,
    Map,
    Array,
    _signals: signals,
    _schedule: schedule,
    matchKey,
  };
  vm.createContext(context);
  const phaseStart = viewsSource.indexOf('function _nationsLeaguePhaseLabel(');
  const start = viewsSource.indexOf('function _nationsLeagueHomeGames(');
  const end = viewsSource.indexOf('\nfunction renderHome', start);
  assert.notEqual(phaseStart, -1);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.runInContext(`${viewsSource.slice(phaseStart, start)}${viewsSource.slice(start, end)}\n` +
    'globalThis.homeHelpers = { phase: _nationsLeaguePhaseLabel, nl: _nationsLeagueHomeGames, tennis: _tennisHomeGames, dedupe: _dedupeHomeGames };', context);
  return context.homeHelpers;
}

function livePayload() {
  const fixtures = [
    ['Denmark', 'Portugal'], ['Greece', 'Netherlands'], ['Wales', 'Norway'],
    ['Malta', 'Gibraltar'], ['Germany', 'Serbia'], ['Israel', 'Kosovo'],
    ['Ireland', 'Austria'],
  ].map(([home, away], index) => ({
    fixture_id: `fixture-${index}`,
    competition: 'UEFA Nations League',
    canonical_identity: { home_team: home, away_team: away },
    kickoff_utc: new Date(NOW + 6 * 60 * 60 * 1000).toISOString(),
    prediction_cutoff: new Date(NOW + 5 * 60 * 60 * 1000).toISOString(),
    phase: 'initial',
    probabilities: { home: 0.35, draw: 0.25, away: 0.40 },
    model_release: { model_version: 'nations_league_v1_1', release_id: 'r'.repeat(64) },
  }));
  return {
    schema: 'nations-league-live-public-v1',
    competition: 'UEFA Nations League',
    status: 'LIVE',
    publication_enabled: true,
    no_bet: true,
    betting_enabled: false,
    ledger_mutation: false,
    fixture_count: fixtures.length,
    fixtures,
  };
}

function liveQuoteProjection(fixtureId, actionable) {
  const signal = {
    signal_id: 'nl:value:canonical-test',
    signal_status: 'ACTIVE',
    is_nations_league_value: true,
    fixture_key: fixtureId,
    market: 'home',
    match: 'Denmark vs Portugal',
    current_odds: 2.1,
    current_ev_pct: 12.5,
    model_prob: 52,
    fair_prob: 40,
    odds_ts: '2026-10-01T11:59:00Z',
    quote_captured_at: '2026-10-01T11:59:00Z',
    event_status: 'PREMATCH',
    league: 'unl',
    confidence: 'MEDIUM',
  };
  const outcomes = {
    home: { model_probability: 0.52, decimal_odds: 2.1, market_probability: 0.40, probability_edge: 0.12, ev: 0.092 },
    draw: { model_probability: 0.25, decimal_odds: 4.0, market_probability: 0.25, probability_edge: 0, ev: 0 },
    away: { model_probability: 0.23, decimal_odds: 3.0, market_probability: 0.35, probability_edge: -0.12, ev: -0.31 },
  };
  return {
    signals: actionable ? [signal] : [],
    quote_evidence: [{
      fixture_id: fixtureId,
      actionable_signal_count: actionable ? 1 : 0,
      no_bet: true,
      no_bet_reason: actionable ? 'ACTIONABLE_SIGNAL_AVAILABLE' : 'NO_CANONICAL_ACTIONABLE_OUTCOME',
      bookmaker: 'iSports European median',
      edge_analysis: { outcomes },
    }],
  };
}

function openLiveDetailWithProjection(projection) {
  const live = livePayload();
  const header = { innerHTML: '' };
  const cards = { innerHTML: '' };
  const context = {
    Date,
    Number,
    Math,
    String,
    Array,
    _nationsLeague: live,
    _nationsLeagueValueProjection: projection,
    matchKey,
    _analyticsFixtureKey: () => 'fixture-key',
    _captureAnalytics: () => {},
    _tickCountdowns: () => {},
    fmtKickoffCompact: () => '01.10 · 18:00',
    esc: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    showView: (view) => { context.lastView = view; },
    document: {
      getElementById: (id) => id === 'detail-header' ? header : id === 'detail-cards' ? cards : null,
    },
  };
  vm.createContext(context);
  const start = appSource.indexOf('function openNationsLeagueMatch(');
  const end = appSource.indexOf('\nfunction openMatch(', start);
  const phaseStart = viewsSource.indexOf('function _nationsLeaguePhaseLabel(');
  const phaseEnd = viewsSource.indexOf('\nfunction _nationsLeagueHomeGames', phaseStart);
  vm.runInContext(`${viewsSource.slice(phaseStart, phaseEnd)}${appSource.slice(start, end)}\n` +
    'globalThis.openNl = openNationsLeagueMatch;', context);
  context.openNl('Denmark vs Portugal');
  return { context, header, cards };
}

test('Home tennis curation maps 178 schedules to exactly 7 unique signal matches', () => {
  const schedule = Array.from({ length: 178 }, (_, index) => ({
    sport: 'tennis',
    home: `Schedule Home ${index}`,
    away: `Schedule Away ${index}`,
    kickoff: new Date(NOW + (index + 1) * 3600000).toISOString(),
    tour: 'Synthetic Tour',
  }));
  const signals = Array.from({ length: 6 }, (_, index) => ({
    sport: 'tennis',
    match: `Schedule Home ${index} vs Schedule Away ${index}`,
    kickoff: schedule[index].kickoff,
    tour: 'Synthetic Tour',
  }));
  signals.push({
    sport: 'tennis',
    match: 'Signal Only Home vs Signal Only Away',
    kickoff: new Date(NOW + 10 * 3600000).toISOString(),
  });
  signals.push({ ...signals[0], market: 'away' });

  const games = homeHelpers(signals, schedule).tennis();
  assert.equal(games.length, 7);
  assert.equal(games.filter((game) => game.home.startsWith('Schedule Home')).length, 6);
  assert.equal(games.some((game) => game.home === 'Signal Only Home'), true);
  assert.equal(games.some((game) => game.home === 'Schedule Home 177'), false);
});

test('LIVE Nations League maps all seven canonical fixtures without odds', () => {
  const helpers = homeHelpers([], []);
  const games = helpers.nl(livePayload(), NOW);
  assert.equal(games.length, 7);
  assert.deepEqual(games.map((game) => game.home), [
    'Denmark', 'Greece', 'Wales', 'Malta', 'Germany', 'Israel', 'Ireland',
  ]);
  assert.ok(games.every((game) => game.is_nations_league_live === true));
  assert.ok(games.every((game) => game.no_bet === true));
  assert.ok(games.every((game) => !('odds_home' in game) && !('odds_draw' in game) && !('odds_away' in game)));
});

test('phase presentation translates without mutating internal lifecycle values', () => {
  const helpers = homeHelpers([], []);
  const live = livePayload();
  assert.equal(live.fixtures[0].phase, 'initial');
  assert.equal(helpers.phase('initial').compact, 'VORAB');
  assert.equal(helpers.phase('initial').detail, 'Vorab-Prognose · Modell');
  assert.equal(live.fixtures[0].phase, 'initial');
  const refinement = helpers.phase(
    'refinement', '2026-10-01T16:45:00Z', '2026-10-01T18:00:00Z',
  );
  assert.equal(refinement.compact, 'AKTUALISIERT');
  assert.equal(refinement.detail, 'Aktualisierte Prognose · 75 Min. vor Anpfiff');
  const fallback = helpers.phase('refinement', 'not-a-time', '');
  assert.equal(fallback.compact, 'AKTUALISIERT');
  assert.equal(fallback.detail, 'Aktualisierte Prognose · Modell');
});

test('invalid LIVE payload is fail-closed and schedule/NL duplicates prefer LIVE identity', () => {
  const helpers = homeHelpers([], []);
  const payload = livePayload();
  const games = helpers.nl(payload, NOW);
  assert.equal(helpers.nl({ ...payload, status: 'SHADOW' }, NOW).length, 0);
  const duplicateSchedule = {
    sport: 'football', home: 'Denmark', away: 'Portugal', league: 'ucl',
    kickoff: games[0].kickoff,
  };
  const merged = helpers.dedupe([duplicateSchedule, ...games]);
  assert.equal(merged.length, 7);
  assert.equal(merged[0].is_nations_league_live, true);
});

test('LIVE detail renders model probabilities and NO BET without market prices', () => {
  const live = livePayload();
  const header = { innerHTML: '' };
  const cards = { innerHTML: '' };
  const context = {
    Date,
    Number,
    Math,
    String,
    Array,
    _nationsLeague: live,
    matchKey,
    _analyticsFixtureKey: () => 'fixture-key',
    _captureAnalytics: () => {},
    _tickCountdowns: () => {},
    fmtKickoffCompact: () => '01.10 · 18:00',
    esc: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    showView: (view) => { context.lastView = view; },
    document: {
      getElementById: (id) => id === 'detail-header' ? header : id === 'detail-cards' ? cards : null,
    },
  };
  vm.createContext(context);
  const start = appSource.indexOf('function openNationsLeagueMatch(');
  const end = appSource.indexOf('\nfunction openMatch(', start);
  const phaseStart = viewsSource.indexOf('function _nationsLeaguePhaseLabel(');
  const phaseEnd = viewsSource.indexOf('\nfunction _nationsLeagueHomeGames', phaseStart);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  assert.notEqual(phaseStart, -1);
  assert.notEqual(phaseEnd, -1);
  vm.runInContext(`${viewsSource.slice(phaseStart, phaseEnd)}${appSource.slice(start, end)}\n` +
    'globalThis.openNl = openNationsLeagueMatch;', context);
  context.openNl('Denmark vs Portugal');

  assert.equal(context.lastView, 'detail');
  assert.match(header.innerHTML, /Denmark vs Portugal/);
  assert.match(cards.innerHTML, /UEFA Nations League · Modell/);
  assert.match(cards.innerHTML, /Vorab-Prognose · Modell/);
  assert.doesNotMatch(cards.innerHTML, /\bINITIAL\b|\bREFINEMENT\b|\bLIVE ·/);
  assert.match(cards.innerHTML, /35\.0% Modell/);
  assert.match(cards.innerHTML, /25\.0% Modell/);
  assert.match(cards.innerHTML, /40\.0% Modell/);
  assert.match(cards.innerHTML, /NO BET/);
  assert.match(cards.innerHTML, /Keine Marktquote/);
  assert.doesNotMatch(cards.innerHTML, /Wette platzieren|place-bet-btn/);
});

test('fresh zero-actionable quote renders value evidence, justified NO BET, and no CTA', () => {
  const { cards } = openLiveDetailWithProjection(liveQuoteProjection('fixture-0', false));
  assert.match(cards.innerHTML, /Quote 2\.10/);
  assert.match(cards.innerHTML, /Markt 40\.0%/);
  assert.match(cards.innerHTML, /Δ 12\.0pp/);
  assert.match(cards.innerHTML, /EV 9\.2%/);
  assert.match(cards.innerHTML, /NO BET · Keine kanonische Aktionierbarkeit/);
  assert.doesNotMatch(cards.innerHTML, /Wette platzieren|data-signal-id=/);
});

test('fresh actionable quote renders canonical signal CTA without NO BET downgrade', () => {
  const { cards } = openLiveDetailWithProjection(liveQuoteProjection('fixture-0', true));
  assert.match(cards.innerHTML, /Quote 2\.10/);
  assert.match(cards.innerHTML, /Markt 40\.0%/);
  assert.match(cards.innerHTML, /WERTSIGNAL · Wettoption verfügbar/);
  assert.match(cards.innerHTML, /Wette platzieren · 2\.10/);
  assert.match(cards.innerHTML, /data-signal-id="nl:value:canonical-test"/);
  assert.doesNotMatch(cards.innerHTML, /NO BET/);
});
