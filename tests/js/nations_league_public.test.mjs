import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { createHash, webcrypto } from 'node:crypto';

const __dir = fileURLToPath(new URL('.', import.meta.url));
const viewsSource = readFileSync(resolve(__dir, '../../docs/js/views.js'), 'utf8');
const appSource = readFileSync(resolve(__dir, '../../docs/js/app.js'), 'utf8');
const workerModule = await import('../../cloudflare/worker.js');
const { serializePublicProduct, validatePublicNationsLeagueDigest } = workerModule;

const NOW = Date.now();
const P = { home: 0.52, draw: 0.25, away: 0.23 };
const M = { home: 0.48, draw: 0.27, away: 0.25 };

function publicBundle(overrides = {}) {
  return {
    schema: 'nations-league-public-v1',
    competition: 'UEFA Nations League',
    provider: 'isports_api',
    provider_league_id: 146819,
    evidence_status: 'WEAK_EVIDENCE_SHADOW_ONLY',
    lifecycle: 'SHADOW_ONLY',
    no_bet: true,
    publication_enabled: false,
    captured_at: new Date(NOW - 1000).toISOString(),
    source_sha: 'a'.repeat(40),
    artifact_digest: 'b'.repeat(64),
    model_snapshot_digest: 'c'.repeat(64),
    public_digest: 'd'.repeat(64),
    fixture_count: 1,
    fixtures: [{
      provider_event_id: 'event-1',
      competition: 'UEFA Nations League',
      kickoff: new Date(NOW + 3600000).toISOString(),
      home: 'Germany',
      away: 'France',
      captured_at: new Date(NOW - 1000).toISOString(),
      source_sha: 'a'.repeat(40),
      artifact_digest: 'b'.repeat(64),
      model: {
        probabilities: P,
        components: { raw_dixon_coles: P, raw_gbt: P, canonical_stacker: P },
      },
      market: { bookmaker: 'pinnacle', probabilities: M, odds_decimal: { home: 2.1, draw: 3.2, away: 3.6 } },
    }],
    ...overrides,
  };
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

function bindPublicDigest(payload) {
  const body = { ...payload };
  delete body.public_digest;
  payload.public_digest = createHash('sha256').update(canonical(body), 'utf8').digest('hex');
  return payload;
}

function render(payload) {
  const element = {
    hidden: false,
    innerHTML: '',
    replaceChildren() { this.innerHTML = ''; },
  };
  const context = {
    Date,
    Number,
    Math,
    String,
    document: { getElementById: (id) => id === 'nations-league-shadow' ? element : null },
    esc: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  };
  vm.createContext(context);
  const start = viewsSource.indexOf('function renderNationsLeagueShadow(');
  const end = viewsSource.indexOf('\nfunction _footballCompatMetaHtml', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.runInContext(viewsSource.slice(start, end) + '\nglobalThis.renderNl = renderNationsLeagueShadow;', context);
  context.renderNl(payload);
  return element;
}

test('Worker public allowlist preserves the validated Nations League shadow envelope', () => {
  const source = { nations_league: publicBundle(), private_marker: 'must disappear' };
  const output = serializePublicProduct(source);
  assert.deepEqual(output.nations_league, source.nations_league);
  assert.equal('private_marker' in output, false);
  assert.throws(() => serializePublicProduct({ nations_league: publicBundle({ no_bet: false }) }), /Nations League/);
  assert.throws(() => serializePublicProduct({ nations_league: publicBundle({ fixture_count: 2 }) }), /Nations League/);
  assert.throws(() => serializePublicProduct({ nations_league: publicBundle({ provider: 'the_odds_api' }) }), /Nations League/);
  assert.throws(() => serializePublicProduct({ nations_league: publicBundle({ provider_league_id: 999 }) }), /Nations League/);
});

test('Worker and PWA verify the public projection digest and reject tampering', async () => {
  const bundle = bindPublicDigest(publicBundle());
  assert.equal(await validatePublicNationsLeagueDigest(bundle), true);
  const tampered = structuredClone(bundle);
  tampered.fixtures[0].home = 'Changed after validation';
  await assert.rejects(validatePublicNationsLeagueDigest(tampered), /digest/i);

  const context = { crypto: webcrypto, TextEncoder, JSON, Object, Array, Uint8Array, String };
  vm.createContext(context);
  const start = appSource.indexOf('function _canonicalNationsLeagueJson(');
  const end = appSource.indexOf('\nfunction _top5LifecycleError', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.runInContext(`${appSource.slice(start, end)}\nglobalThis.verifyNlDigest = _validNationsLeaguePublicDigest;`, context);
  assert.equal(await context.verifyNlDigest(bundle), true);
  assert.equal(await context.verifyNlDigest(tampered), false);
});

test('Worker GET /signals.json serves a bound shadow object and fails closed on digest mismatch', async () => {
  const bundle = bindPublicDigest(publicBundle());
  const values = new Map([['signals_json', JSON.stringify({ updated: bundle.captured_at, nations_league: bundle })]]);
  const env = { SIGNALS: { get: async (key) => values.get(key) || null } };
  const request = () => new Request('https://worker.test/signals.json');
  const response = await workerModule.default.fetch(request(), env, {});
  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).nations_league, bundle);
  const tampered = structuredClone(bundle);
  tampered.fixtures[0].home = 'Tampered';
  values.set('signals_json', JSON.stringify({ nations_league: tampered }));
  const rejected = await workerModule.default.fetch(request(), env, {});
  assert.equal(rejected.status, 500);
});

test('PWA renders UEFA Nations League as read-only Shadow with model and market probabilities', () => {
  const payload = publicBundle();
  const secondFixture = structuredClone(payload.fixtures[0]);
  secondFixture.provider_event_id = 'event-2';
  secondFixture.home = 'Spain';
  secondFixture.away = 'Italy';
  secondFixture.kickoff = new Date(NOW + 2 * 3600000).toISOString();
  secondFixture.market.bookmaker = 'pinnacle';
  payload.fixtures.push(secondFixture);
  payload.fixture_count = 2;
  const element = render(payload);
  assert.equal(element.hidden, false);
  assert.match(element.innerHTML, /UEFA Nations League/);
  assert.match(element.innerHTML, />Shadow</);
  assert.match(element.innerHTML, /Modell 52\.0%/);
  assert.match(element.innerHTML, /Markt 48\.0%/);
  assert.match(element.innerHTML, /Spain vs Italy/);
  assert.equal((element.innerHTML.match(/class="nl-shadow-fixture"/g) || []).length, 2);
  assert.match(element.innerHTML, /Kein Wett- oder Produktionssignal/);
  assert.doesNotMatch(element.innerHTML, /place-bet|data-stake|wette abgeben/i);
});

test('PWA hides stale and incomplete Nations League bundles and keeps the shadow out of signals', () => {
  const stale = render(publicBundle({ captured_at: new Date(NOW - 16 * 60 * 1000).toISOString() }));
  assert.equal(stale.hidden, true);
  const partial = render(publicBundle({ fixture_count: 2 }));
  assert.equal(partial.hidden, true);
  assert.match(appSource, /_signals\s*=\s*\[\.\.\.\(d\.football\|\|\[\]\),\s*\.\.\.\(d\.tennis\|\|\[\]\)\]/);
  assert.match(appSource, /renderNationsLeagueShadow\(d\.nations_league\s*\|\|\s*null\)/);
});
