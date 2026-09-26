import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');

function sourceBetween(start, end) {
  const from = viewsSource.indexOf(start);
  const to = viewsSource.indexOf(end, from);
  assert.notEqual(from, -1, `missing source start: ${start}`);
  assert.notEqual(to, -1, `missing source end: ${end}`);
  return viewsSource.slice(from, to);
}

function renderCard(signal) {
  const context = {
    esc: (value) => String(value ?? ''),
    marketLabel: () => 'Heimsieg',
    _footballCompatMetaHtml: () => '',
    _signalAgeHtml: () => '',
    _modelDots: () => '',
    _signalOddsHistoryHtml: () => '',
    _oddsSparkline: () => '',
    infoTip: () => '',
  };
  vm.createContext(context);
  vm.runInContext(
    `${sourceBetween('function _top5LifecycleAge', 'function _formBadgesHtml')}` +
      `${sourceBetween('function sigCard(', '\n// ── Top Recommendations')}` +
      '\nglobalThis.renderSigCard = sigCard;',
    context,
    { filename: 'docs/js/views.js' },
  );
  return context.renderSigCard(signal, true);
}

function signal(stage, lifecycleOverrides = {}) {
  const now = Date.now();
  const initialAt = new Date(now - 30 * 60000).toISOString();
  const currentAt = new Date(now).toISOString();
  const lifecycle = {
    schema_version: 'top5-lifecycle-public-v1',
    lifecycle_id: 'life:fixture:home',
    initial_record_id: 'initial:fixture:home',
    lifecycle_version: stage === 'INITIAL' ? 1 : 2,
    lifecycle_stage: stage,
    initial_generated_at: stage === 'INITIAL' ? currentAt : initialAt,
    current_generated_at: currentAt,
    fixture_identity: 'fixture:one',
    model_identity: 'model:one',
    provenance_binding: { source_sha: 'a'.repeat(64) },
    ...lifecycleOverrides,
  };
  if (stage === 'REFINED') {
    Object.assign(lifecycle, {
      initial_probability: 0.52,
      current_probability: 0.55,
      probability_delta: 0.03,
      initial_market_probability: 0.49,
      current_market_probability: 0.5,
      initial_edge_pp: 3,
      current_edge_pp: 5,
      edge_delta_pp: 2,
      refinement_classification: 'STRENGTHENED',
    });
  }
  if (stage === 'WITHDRAWN') lifecycle.refinement_classification = 'WITHDRAWN';
  return {
    sport: 'football',
    league: 'EPL',
    match: 'Home FC vs Away FC',
    home: 'Home FC',
    away: 'Away FC',
    kickoff: new Date(now + 2 * 3600000).toISOString(),
    market: 'home',
    odds: 2.1,
    ev_pct: 7,
    confidence: 'MEDIUM',
    stake_eur: 5,
    stake_pct: 1,
    model_prob: stage === 'REFINED' ? 55 : 52,
    fair_prob: 50,
    current_odds: 2.1,
    current_ev_pct: 7,
    signal_status: 'CONTROLLED',
    signal_id: 'signal:one',
    odds_ts: currentAt,
    lifecycle,
  };
}

test('INITIAL renders a quiet badge and distinct prediction, odds, and kickoff ages', () => {
  const html = renderCard(signal('INITIAL'));
  assert.match(html, />Initial</);
  assert.match(html, /Vorhersage aktualisiert:/);
  assert.match(html, /Odds-Snapshot:/);
  assert.match(html, /Anpfiff:/);
});

test('REFINED keeps current probability primary and shows compact history/classification', () => {
  const html = renderCard(signal('REFINED'));
  assert.match(html, />Refined</);
  assert.match(html, /52\.0% → 55\.0%/);
  assert.match(html, /Edge \+3\.0 → \+5\.0 pp/);
  assert.match(html, /Gestärkt/);
  assert.match(html, /55\.0%/);
});

test('WITHDRAWN is clearly historical and contains no active odds, EV, or bet action', () => {
  const html = renderCard(signal('WITHDRAWN', {
    initial_probability: 0.52,
    current_probability: 0.55,
    probability_delta: 0.03,
  }));
  assert.match(html, />Zurückgezogen</);
  assert.match(html, /Keine aktive Empfehlung/);
  assert.match(html, /52\.0% → 55\.0%/);
  assert.doesNotMatch(html, /class="card-footer"/);
  assert.doesNotMatch(html, /EV \+/);
  assert.doesNotMatch(html, /Wette platzieren/);
});
