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

function sixDecimalBundle() {
  const bundle = publicBundle();
  const fixture = bundle.fixtures[0];
  const modelProbabilities = { home: 0.260599, draw: 0.448933, away: 0.290468 };
  fixture.model.probabilities = modelProbabilities;
  fixture.model.components = {
    raw_dixon_coles: { ...modelProbabilities },
    raw_gbt: { home: 0.692012, draw: 0.260599, away: 0.047389 },
    canonical_stacker: { ...modelProbabilities },
  };
  fixture.market.probabilities = { home: 0.692012, draw: 0.260599, away: 0.047389 };
  fixture.market.odds_decimal = { home: 1.513093, draw: 2.410520, away: 3.380093 };
  return bindPublicDigest(bundle);
}

async function withFrozenDateNow(nowMs, callback) {
  const originalNow = Date.now;
  Date.now = () => nowMs;
  try {
    return await callback();
  } finally {
    Date.now = originalNow;
  }
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

function renderLive(payload) {
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
    document: { getElementById: (id) => id === 'nations-league-live' ? element : null },
    esc: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  };
  vm.createContext(context);
  const start = viewsSource.indexOf('function renderNationsLeagueLive(');
  const end = viewsSource.indexOf('\nfunction renderNationsLeagueShadow', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.runInContext(viewsSource.slice(start, end) + '\nglobalThis.renderNl = renderNationsLeagueLive;', context);
  context.renderNl(payload);
  return element;
}

function appNlHelpers() {
  const context = { crypto: webcrypto, TextEncoder, JSON, Object, Array, Uint8Array, String, Date, Number, Math };
  vm.createContext(context);
  const start = appSource.indexOf('function _canonicalNationsLeagueJson(');
  const end = appSource.indexOf('\nfunction _top5LifecycleError', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.runInContext(`const DATA_URL = 'data/signals.json';\n${appSource.slice(start, end)}\n` +
    'globalThis.nlHelpers = { digest: _validNationsLeaguePublicDigest, valid: _validNationsLeaguePublicPayload, merge: _mergeStaticNationsLeagueIfMissing };', context);
  return context.nlHelpers;
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

test('LIVE Nations League projection passes the serializer boundary without betting semantics', async () => {
  const live = JSON.parse(readFileSync(resolve(__dir, '../../docs/data/signals.json'), 'utf8')).nations_league;
  assert.equal(live.status, 'LIVE');
  assert.equal(live.fixture_count, 7);
  assert.equal(live.no_bet, true);
  assert.equal(live.betting_enabled, false);
  assert.equal(live.ledger_mutation, false);
  assert.equal(await validatePublicNationsLeagueDigest(live), true);
  assert.deepEqual(serializePublicProduct({ nations_league: live }).nations_league, live);
  assert.equal(await appNlHelpers().valid(live), true);
});

test('PWA renders future LIVE fixtures only and never exposes a bet action', () => {
  const live = JSON.parse(readFileSync(resolve(__dir, '../../docs/data/signals.json'), 'utf8')).nations_league;
  const payload = structuredClone(live);
  payload.fixtures = payload.fixtures.slice(0, 2).map((fixture, index) => ({
    ...fixture,
    kickoff_utc: new Date(NOW + (index + 1) * 3600000).toISOString(),
  }));
  payload.fixture_count = 2;
  const rendered = renderLive(payload);
  assert.equal(rendered.hidden, false);
  assert.match(rendered.innerHTML, /UEFA Nations League/);
  assert.match(rendered.innerHTML, />LIVE</);
  assert.match(rendered.innerHTML, /NO BET/);
  assert.match(rendered.innerHTML, /Germany|Greece|Denmark|Wales/);
  assert.doesNotMatch(rendered.innerHTML, /place-bet|data-stake|wette abgeben/i);
});

test('Worker and PWA verify the public projection digest and reject tampering', async () => {
  const bundle = bindPublicDigest(publicBundle());
  assert.equal(await validatePublicNationsLeagueDigest(bundle), true);
  const tampered = structuredClone(bundle);
  tampered.fixtures[0].home = 'Changed after validation';
  await assert.rejects(validatePublicNationsLeagueDigest(tampered), /digest/i);

  const helpers = appNlHelpers();
  assert.equal(await helpers.digest(bundle), true);
  assert.equal(await helpers.digest(tampered), false);
  assert.equal(await helpers.valid(bundle), true);
});

test('Worker retention accepts valid public shadow snapshots through 24 hours only', async () => {
  const helpers = appNlHelpers();
  const sixHoursOld = publicBundle({ captured_at: new Date(NOW - 6 * 3600000).toISOString() });
  sixHoursOld.fixtures[0].captured_at = sixHoursOld.captured_at;
  bindPublicDigest(sixHoursOld);
  assert.equal(await validatePublicNationsLeagueDigest(sixHoursOld), true);
  assert.equal(await helpers.valid(sixHoursOld), true);
  const expired = publicBundle({ captured_at: new Date(NOW - 24 * 3600000 - 1).toISOString() });
  expired.fixtures[0].captured_at = expired.captured_at;
  bindPublicDigest(expired);
  await assert.rejects(validatePublicNationsLeagueDigest(expired), /stale/i);
  assert.equal(await helpers.valid(expired), false);
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

test('PWA renders a 16-minute Shadow snapshot with visible freshness and keeps it read-only', () => {
  const capturedAt = new Date(NOW - 16 * 60 * 1000).toISOString();
  const payload = publicBundle({ captured_at: capturedAt });
  payload.fixtures[0].captured_at = capturedAt;
  const visible = render(payload);
  assert.equal(visible.hidden, false);
  assert.match(visible.innerHTML, /Shadow Snapshot · erfasst vor 16 Min\./);
  assert.match(visible.innerHTML, /WEAK EVIDENCE · NO BET/);
  assert.doesNotMatch(visible.innerHTML, /place-bet|data-stake|wette abgeben/i);
});

test('PWA labels several-hours-old snapshots stale and hides snapshots over 24 hours', () => {
  const capturedAt = new Date(NOW - 6 * 3600000).toISOString();
  const old = publicBundle({ captured_at: capturedAt });
  old.fixtures[0].captured_at = capturedAt;
  const stale = render(old);
  assert.equal(stale.hidden, false);
  assert.match(stale.innerHTML, /Snapshot veraltet/);
  const expiredAt = new Date(NOW - 24 * 3600000 - 1).toISOString();
  const expired = publicBundle({ captured_at: expiredAt });
  expired.fixtures[0].captured_at = expiredAt;
  assert.equal(render(expired).hidden, true);
  const partial = render(publicBundle({ fixture_count: 2 }));
  assert.equal(partial.hidden, true);
  assert.match(appSource, /_signals\s*=\s*\[\.\.\.\(d\.football\|\|\[\]\),\s*\.\.\.\(d\.tennis\|\|\[\]\)\]/);
  assert.match(appSource, /renderNationsLeagueShadow\(d\.nations_league\s*\|\|\s*null\)/);
});

test('Worker-missing NL bridge merges only a valid static Nations League field', async () => {
  const helpers = appNlHelpers();
  const worker = { football: [{ id: 'worker-football' }], tennis: [{ id: 'worker-tennis' }], marker: 'worker' };
  const nationsLeague = bindPublicDigest(publicBundle());
  const staticPayload = {
    football: [{ id: 'static-football' }],
    tennis: [{ id: 'static-tennis' }],
    nations_league: nationsLeague,
    private_data: { must_not_copy: true },
  };
  let requests = 0;
  const merged = await helpers.merge(worker, 'worker', async (url, options) => {
    requests += 1;
    assert.match(url, /^data\/signals\.json\?t=/);
    assert.equal(options.cache, 'no-store');
    return { ok: true, json: async () => staticPayload };
  }, NOW);
  assert.equal(requests, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(merged)), {
    ...worker,
    nations_league: nationsLeague,
  });
  assert.deepEqual(merged.football, worker.football);
  assert.deepEqual(merged.tennis, worker.tennis);
  assert.equal('_signals' in merged, false);
  assert.equal(merged.nations_league.no_bet, true);
});

test('Worker-missing NL bridge ignores tampered static NL and never replaces Worker NL', async () => {
  const helpers = appNlHelpers();
  const worker = { football: [{ id: 'worker' }], tennis: [], marker: 'worker' };
  const tampered = bindPublicDigest(publicBundle());
  tampered.fixtures[0].home = 'tampered after digest';
  const unchanged = await helpers.merge(worker, 'worker', async () => ({
    ok: true,
    json: async () => ({ football: [{ id: 'static' }], tennis: [], nations_league: tampered }),
  }), NOW);
  assert.deepEqual(JSON.parse(JSON.stringify(unchanged)), worker);

  const rawPrivateArtifact = {
    schema: 'nations-league-isports-shadow-v2',
    provider_operation_manifest: [{ api_key: 'must-not-be-copied' }],
  };
  const privateUnchanged = await helpers.merge(worker, 'worker', async () => ({
    ok: true,
    json: async () => ({ nations_league: rawPrivateArtifact }),
  }), NOW);
  assert.deepEqual(JSON.parse(JSON.stringify(privateUnchanged)), worker);

  const workerNl = bindPublicDigest(publicBundle());
  const workerWithNl = { ...worker, nations_league: workerNl };
  let requests = 0;
  const preferred = await helpers.merge(workerWithNl, 'worker', async () => {
    requests += 1;
    return { ok: true, json: async () => ({ nations_league: publicBundle({ provider_event_id: 'static' }) }) };
  }, NOW);
  assert.equal(requests, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(preferred)), JSON.parse(JSON.stringify(workerWithNl)));
});

test('Worker accepts six-decimal public values that are not exact IEEE-754 scaled integers', async () => {
  const bundle = sixDecimalBundle();
  assert.equal(await validatePublicNationsLeagueDigest(bundle), true);
  assert.equal(await appNlHelpers().valid(bundle), true);

  const tooPrecise = structuredClone(bundle);
  tooPrecise.fixtures[0].model.probabilities = {
    home: 0.1234567,
    draw: 0.3,
    away: 0.5765433,
  };
  tooPrecise.fixtures[0].model.components.canonical_stacker = {
    ...tooPrecise.fixtures[0].model.probabilities,
  };
  bindPublicDigest(tooPrecise);
  assert.throws(() => serializePublicProduct({ nations_league: tooPrecise }), /Nations League/);
});

test('Worker keeps public probability normalization and decimal-odds bounds', () => {
  const nonNormalized = sixDecimalBundle();
  nonNormalized.fixtures[0].model.probabilities = {
    home: 0.5,
    draw: 0.25,
    away: 0.249998,
  };
  nonNormalized.fixtures[0].model.components.canonical_stacker = {
    ...nonNormalized.fixtures[0].model.probabilities,
  };
  bindPublicDigest(nonNormalized);
  assert.throws(() => serializePublicProduct({ nations_league: nonNormalized }), /Nations League/);

  const invalidOdds = sixDecimalBundle();
  invalidOdds.fixtures[0].market.odds_decimal.home = 1;
  bindPublicDigest(invalidOdds);
  assert.throws(() => serializePublicProduct({ nations_league: invalidOdds }), /Nations League/);
});

test('Worker still rejects unsafe public Nations League schema, provenance, and lifecycle mutations', () => {
  const mutations = [
    (bundle) => { bundle.schema = 'nations-league-public-v2'; },
    (bundle) => { bundle.fixtures[0].source_sha = 'f'.repeat(40); },
    (bundle) => { bundle.lifecycle = 'ACTIVE'; },
    (bundle) => { bundle.no_bet = false; },
    (bundle) => { bundle.publication_enabled = true; },
  ];
  for (const mutate of mutations) {
    const bundle = structuredClone(publicBundle());
    mutate(bundle);
    assert.throws(() => serializePublicProduct({ nations_league: bundle }), /Nations League/);
  }
});

test('Worker accepts the canonical incident public bundle and verifies its Python digest', async () => {
  const bundle = JSON.parse(
    readFileSync(
      resolve(__dir, '../fixtures/nations_league_public_incident_20260928.json'),
      'utf8',
    ),
  );
  assert.equal(bundle.schema, 'nations-league-public-v1');
  assert.equal(bundle.fixture_count, 46);
  assert.equal(bundle.fixtures.length, 46);
  assert.equal(bundle.public_digest, '59f5aa67e18c89177e24824473b36040d5508094871cadaee43ba9a1478b125a');
  const serialized = JSON.stringify(bundle);
  assert.match(serialized, /0\.260599/);
  assert.match(serialized, /0\.448933/);
  assert.match(serialized, /0\.692012/);

  const now = Date.parse(bundle.captured_at) + 1;
  await withFrozenDateNow(now, async () => {
    assert.equal(await validatePublicNationsLeagueDigest(bundle), true);

    const numericTamper = structuredClone(bundle);
    numericTamper.fixtures[0].market.odds_decimal.home += 0.000001;
    await assert.rejects(validatePublicNationsLeagueDigest(numericTamper), /digest/i);
  });
});
