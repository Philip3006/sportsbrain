import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const app = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');
const views = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');

test('PWA consumes only the validated Nations League value projection', () => {
  assert.match(app, /_validNationsLeagueActionablePayload/);
  assert.match(app, /d\.nations_league_value_signals\?\.signals/);
  assert.match(app, /d\.nations_league_value_signals = null|nations_league_value_signals: null/);
  assert.match(views, /isActionableValueSignal\(nlSignal/);
  assert.match(views, /nl-value-unavailable/);
  assert.match(views, /data-signal-id=/);
});

test('PWA keeps the immutable Nations League bundle outside the value projection', () => {
  assert.match(app, /renderNationsLeagueLive\(d\.nations_league/);
  assert.match(app, /only the independently validated fresh quote/);
});
