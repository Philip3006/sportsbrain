import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const analyticsSource = readFileSync(resolve(__dir, '../../docs/js/analytics.js'), 'utf8');
const appSource = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const indexSource = readFileSync(resolve(__dir, '../../docs/index.html'), 'utf8');

function loadAnalytics({ disabled = false, fetchImpl } = {}) {
  const listeners = {};
  const document = {
    body: { dataset: {} },
    querySelector: () => null,
    addEventListener: (name, callback) => { listeners[`document:${name}`] = callback; },
  };
  const context = {
    Date,
    Error,
    JSON,
    Math,
    Promise,
    String,
    document,
    crypto: { randomUUID: () => 'anonymous-test-id' },
    fetch: fetchImpl || (() => Promise.resolve({ ok: true })),
    matchMedia: () => ({ matches: false }),
    addEventListener: (name, callback) => { listeners[name] = callback; },
    SB_ANALYTICS_DISABLED: disabled,
  };
  vm.createContext(context);
  vm.runInContext(analyticsSource, context);
  return { api: context.sbAnalytics, context, listeners };
}

function observed(api) {
  return JSON.parse(JSON.stringify(api.__test.events()));
}

test('analytics initialization and transport failures are fail-open', () => {
  const { api } = loadAnalytics({ fetchImpl: () => { throw new Error('network blocked'); } });
  assert.doesNotThrow(() => api.capture('tab_opened', { tab: 'football', previous_tab: 'home' }));
  assert.ok(observed(api).some((item) => item.event === 'tab_opened'));
});

test('pwa_opened is emitted once per application load', () => {
  const { api } = loadAnalytics({ disabled: true });
  api.capture('pwa_opened', { display_mode: 'browser' }, { onceKey: 'pwa_opened' });
  assert.equal(observed(api).filter((item) => item.event === 'pwa_opened').length, 1);
});

test('tab and match events retain semantic properties', () => {
  const { api } = loadAnalytics({ disabled: true });
  api.capture('tab_opened', { tab: 'football', previous_tab: 'home' });
  api.capture('match_opened', {
    sport: 'football', competition: 'EPL', fixture_key: 'alpha vs beta',
    lifecycle_stage: 'INITIAL', source_view: 'football',
  });
  const events = observed(api);
  assert.deepEqual(events.find((item) => item.event === 'tab_opened').properties, {
    tab: 'football', previous_tab: 'home',
  });
  assert.equal(events.find((item) => item.event === 'match_opened').properties.fixture_key, 'alpha vs beta');
});

test('nested helper ownership can deduplicate one interaction', () => {
  const { api } = loadAnalytics({ disabled: true });
  const properties = { sport: 'football', fixture_key: 'alpha vs beta', source_view: 'signal' };
  api.capture('match_opened', properties, { onceKey: 'interaction:signal:1' });
  api.capture('match_opened', properties, { onceKey: 'interaction:signal:1' });
  assert.equal(observed(api).filter((item) => item.event === 'match_opened').length, 1);
});

test('signal events drop financial, private, and credential-like fields', () => {
  const { api } = loadAnalytics({ disabled: true });
  api.capture('signal_opened', {
    sport: 'football', competition: 'EPL', fixture_key: 'alpha vs beta',
    lifecycle_stage: 'INITIAL', signal_status: 'ACTIVE', confidence: 'HIGH', source_view: 'football',
    bankroll: 100, stake_eur: 5, open_bets: ['private'], access_token: 'secret',
  });
  const event = observed(api).find((item) => item.event === 'signal_opened');
  assert.deepEqual(event.properties, {
    sport: 'football', competition: 'EPL', fixture_key: 'alpha vs beta',
    lifecycle_stage: 'INITIAL', signal_status: 'ACTIVE', confidence: 'HIGH', source_view: 'football',
  });
});

test('frontend errors are sanitized and contain no raw URL, email, or bearer token', () => {
  const { api, listeners } = loadAnalytics({ disabled: true });
  listeners.error({
    message: 'request https://private.invalid/me for jane@example.com Bearer secret-token',
  });
  listeners.unhandledrejection({
    reason: new Error('failed at https://private.invalid/response?token=secret'),
  });
  const errors = observed(api).filter((item) => item.event === 'frontend_error');
  assert.equal(errors.length, 2);
  for (const error of errors) {
    const serialized = JSON.stringify(error.properties);
    assert.doesNotMatch(serialized, /https?:\/\//i);
    assert.doesNotMatch(serialized, /@/);
    assert.doesNotMatch(serialized, /Bearer|secret-token|token=secret/i);
  }
});

test('the PWA loads analytics centrally and keeps shadow/actionability boundaries intact', () => {
  assert.match(indexSource, /js\/analytics\.js/);
  assert.match(appSource, /sbAnalytics/);
  assert.match(viewsSource, /data-analytics-diagnostics/);
  assert.match(appSource, /payload\.no_bet !== true/);
  assert.match(viewsSource, /NO BET/);
  assert.doesNotMatch(analyticsSource, /identify\s*\(/);
  assert.doesNotMatch(analyticsSource, /session[_ -]?replay|enable_recording/i);
});
