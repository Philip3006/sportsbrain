// Read-only Champions League public-product contract.
// This deliberately does not select providers, call providers, activate data,
// or write KV state. It mirrors the Python CL publication boundary.

export const CL_CANONICAL_LEAGUE = 'ucl';
export const CL_COMPETITION_NAME = 'UEFA Champions League';
export const CL_LEAGUE_CODES = new Set([
  'ucl', 'champions_league', 'uefa_champs_league', 'soccer_uefa_champs_league',
]);
export const CL_PUBLICATION_SCHEMA = 'champions-league-publication-v1';
const CL_MARKETS = new Set(['home', 'draw', 'away']);
const CL_STATES = new Set(['DISABLED', 'SHADOW', 'CONTROLLED', 'LIVE']);
const CL_PUBLICATION_STATUSES = new Set(['UNPUBLISHED', 'PUBLISHED', 'FAILED', 'BLOCKED']);
const CL_RESULT_STATUSES = new Set(['PENDING', 'FINAL', 'WON', 'LOST', 'VOID', 'UNKNOWN']);
const SHA256_RE = /^[0-9a-f]{64}$/i;
const CL_RELEASE_FIELDS = new Set([
  'schema_version', 'competition', 'league_code', 'generation_id',
  'activation_state', 'publication_status', 'publication_enabled',
  'publication_authorization_id', 'provider_authority', 'result_authority',
  'source_sha', 'research_sha', 'model_artifact_hash', 'prediction_count',
  'fixture_count', 'generated_at', 'published_at', 'stale_after_seconds', 'no_bet',
]);
const CL_PROVENANCE_FIELDS = [
  'source', 'provider', 'source_sha', 'research_sha', 'model_artifact_hash',
  'snapshot_id', 'snapshot_kind', 'captured_at', 'source_age_seconds', 'evidence_digest',
];

function text(value, field) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`CL ${field} is required`);
  return value.trim();
}

function sha(value, field) {
  const valueText = text(value, field);
  if (!SHA256_RE.test(valueText)) throw new Error(`CL ${field} must be a SHA-256 hex digest`);
  return valueText;
}

function timestamp(value, field) {
  const valueText = text(value, field);
  const parsed = Date.parse(valueText);
  if (!Number.isFinite(parsed)) throw new Error(`CL ${field} must be ISO-8601`);
  return parsed;
}

function number(value, field) {
  if (typeof value === 'boolean' || !Number.isFinite(Number(value)) || Number(value) < 0) {
    throw new Error(`CL ${field} must be finite and non-negative`);
  }
  return Number(value);
}

function count(value, field) {
  if (!Number.isInteger(value) || value < 1) throw new Error(`CL ${field} must be a positive integer`);
  return value;
}

function canonicalLeague(value) {
  if (typeof value !== 'string' || !CL_LEAGUE_CODES.has(value.trim().toLowerCase())) {
    throw new Error(`unsupported CL league: ${value}`);
  }
  return CL_CANONICAL_LEAGUE;
}

function state(value) {
  const normalized = text(value, 'activation_state').toUpperCase();
  if (!CL_STATES.has(normalized)) throw new Error(`unsupported CL activation_state: ${normalized}`);
  return normalized;
}

function publicationStatus(value) {
  const normalized = text(value, 'publication_status').toUpperCase();
  if (!CL_PUBLICATION_STATUSES.has(normalized)) throw new Error(`unsupported CL publication_status: ${normalized}`);
  return normalized;
}

function ageCheck(value, field, nowMs, maxAgeSeconds) {
  const parsed = timestamp(value, field);
  const age = (nowMs - parsed) / 1000;
  if (age < 0) throw new Error(`CL ${field} is in the future`);
  if (maxAgeSeconds != null && age > maxAgeSeconds) throw new Error(`CL ${field} is stale`);
  return parsed;
}

function validateRelease(value, { nowMs, maxAgeSeconds, requirePublished }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('champions_league_release must be an object');
  }
  const required = [
    'schema_version', 'competition', 'league_code', 'generation_id',
    'activation_state', 'publication_status', 'publication_enabled',
    'provider_authority', 'result_authority', 'source_sha', 'research_sha',
    'model_artifact_hash', 'prediction_count', 'fixture_count', 'generated_at',
    'stale_after_seconds', 'no_bet',
  ];
  for (const field of required) if (!(field in value)) throw new Error(`CL release is incomplete: ${field}`);
  if (value.schema_version !== CL_PUBLICATION_SCHEMA) throw new Error('unsupported CL publication schema');
  if (value.competition !== CL_COMPETITION_NAME) throw new Error('CL competition name mismatch');
  canonicalLeague(value.league_code);
  const activationState = state(value.activation_state);
  const status = publicationStatus(value.publication_status);
  if (typeof value.publication_enabled !== 'boolean') throw new Error('CL publication_enabled must be boolean');
  if (value.no_bet !== true) throw new Error('CL publication must remain no-bet');
  text(value.generation_id, 'generation_id');
  text(value.provider_authority, 'provider_authority');
  text(value.result_authority, 'result_authority');
  sha(value.source_sha, 'source_sha');
  sha(value.research_sha, 'research_sha');
  sha(value.model_artifact_hash, 'model_artifact_hash');
  const predictionCount = count(value.prediction_count, 'prediction_count');
  const fixtureCount = count(value.fixture_count, 'fixture_count');
  const staleAfterSeconds = number(value.stale_after_seconds, 'stale_after_seconds');
  if (staleAfterSeconds <= 0) {
    throw new Error('CL stale_after_seconds must be positive');
  }
  ageCheck(value.generated_at, 'generated_at', nowMs, maxAgeSeconds ?? staleAfterSeconds);
  if (value.published_at) timestamp(value.published_at, 'published_at');
  if (value.publication_authorization_id) text(value.publication_authorization_id, 'publication_authorization_id');
  if (value.publication_enabled && !value.publication_authorization_id) {
    throw new Error('published CL release requires publication authorization');
  }
  if (value.publication_enabled && (status !== 'PUBLISHED' || !['CONTROLLED', 'LIVE'].includes(activationState))) {
    throw new Error('published CL release must be CONTROLLED/LIVE and PUBLISHED');
  }
  if (!value.publication_enabled && status === 'PUBLISHED') throw new Error('unenabled CL release cannot claim PUBLISHED status');
  if (requirePublished && value.publication_enabled !== true) throw new Error('CL publication is not enabled');
  return { ...value, league_code: CL_CANONICAL_LEAGUE, activation_state: activationState,
    publication_status: status, prediction_count: predictionCount, fixture_count: fixtureCount,
    stale_after_seconds: staleAfterSeconds };
}

export function projectChampionsLeagueRelease(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('champions_league_release must be an object');
  const projected = {};
  for (const field of CL_RELEASE_FIELDS) if (field in value) projected[field] = value[field];
  return validateRelease(projected, { nowMs: Date.now(), maxAgeSeconds: null, requirePublished: false });
}

function validateRecord(record, { nowMs, maxAgeSeconds, release }) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) throw new Error('CL record must be an object');
  if (record.sport !== 'football') throw new Error('CL record must have sport=football');
  canonicalLeague(record.league);
  const predictionId = text(record.prediction_id, 'prediction_id');
  const fixtureKey = text(record.fixture_key, 'fixture_key');
  text(record.match, 'match'); text(record.home, 'home'); text(record.away, 'away');
  timestamp(record.kickoff, 'kickoff'); timestamp(record.prediction_timestamp, 'prediction_timestamp');
  text(record.model_identity, 'model_identity'); text(record.source, 'source'); text(record.provider, 'provider');
  const market = text(record.market, 'market').toLowerCase();
  if (!CL_MARKETS.has(market)) throw new Error(`unsupported CL market: ${market}`);
  if (typeof record.publication_enabled !== 'boolean') throw new Error('CL record publication_enabled must be boolean');
  if (record.no_bet !== true) throw new Error('CL record must remain no-bet');
  const activationState = state(record.activation_state);
  const status = publicationStatus(record.publication_status);
  if (text(record.signal_status, 'signal_status').toUpperCase() === 'ACTIVE') throw new Error('CL record cannot be actionable');
  const resultStatus = text(record.result_status, 'result_status').toUpperCase();
  if (!CL_RESULT_STATUSES.has(resultStatus) || text(record.settlement_status, 'settlement_status').toUpperCase() !== resultStatus) {
    throw new Error('CL result/settlement representation is invalid');
  }
  const staleState = text(record.stale_state, 'stale_state').toUpperCase();
  if (!['FRESH', 'STALE', 'UNKNOWN'].includes(staleState)) throw new Error('CL stale_state is invalid');
  const provenance = record.provenance;
  if (!provenance || typeof provenance !== 'object' || Array.isArray(provenance)) throw new Error('CL provenance must be an object');
  for (const field of CL_PROVENANCE_FIELDS) {
    if (!(field in provenance)) throw new Error(`CL provenance missing ${field}`);
    if (field === 'source_age_seconds') number(provenance[field], `provenance.${field}`);
    else if (['source_sha', 'research_sha', 'model_artifact_hash', 'evidence_digest'].includes(field)) sha(provenance[field], `provenance.${field}`);
    else if (field === 'captured_at') ageCheck(provenance[field], 'provenance.captured_at', nowMs, maxAgeSeconds);
    else text(provenance[field], `provenance.${field}`);
  }
  if (provenance.source !== record.source || provenance.provider !== record.provider) throw new Error('CL record/provenance source binding mismatch');
  if (release) {
    if (status !== release.publication_status || activationState !== release.activation_state) throw new Error('CL record/release state mismatch');
    if (record.publication_enabled !== release.publication_enabled) throw new Error('CL record/release enablement mismatch');
    if (record.provider !== release.provider_authority) throw new Error('CL record/release provider mismatch');
    for (const field of ['source_sha', 'research_sha', 'model_artifact_hash']) if (provenance[field] !== release[field]) throw new Error(`CL ${field} binding mismatch`);
  }
  if (status === 'PUBLISHED' && staleState !== 'FRESH') throw new Error('stale CL records cannot be published');
  if (record.publication_enabled && status !== 'PUBLISHED') throw new Error('enabled CL records must be PUBLISHED');
  return { predictionId, fixtureKey, market };
}

function validateHealth(health, release) {
  if (!health || typeof health !== 'object' || Array.isArray(health)) throw new Error('CL health is required');
  const candidates = [];
  if (health.football_release && typeof health.football_release === 'object') candidates.push(health.football_release);
  if (Array.isArray(health.football_releases)) candidates.push(...health.football_releases.filter((item) => item && typeof item === 'object'));
  const matches = candidates.filter((item) => CL_LEAGUE_CODES.has(String(item.league || '').toLowerCase()));
  if (matches.length !== 1) throw new Error('CL health release is missing or duplicated');
  const item = matches[0];
  for (const field of ['schema_version', 'league', 'publication_status', 'stale_artifact', 'missing_result_count', 'settlement_status', 'source_age_seconds', 'source', 'provider', 'source_sha', 'activation_state', 'no_bet', 'publication_enabled', 'observed_at']) {
    if (!(field in item)) throw new Error(`CL health missing ${field}`);
  }
  if (item.schema_version !== 'football-release-health-v1' || item.source_sha !== release.source_sha || item.provider !== release.provider_authority || item.publication_status !== release.publication_status || String(item.activation_state).toUpperCase() !== release.activation_state || item.publication_enabled !== release.publication_enabled || item.no_bet !== true) throw new Error('CL health/release binding mismatch');
  timestamp(item.observed_at, 'CL health observed_at');
}

export function validateChampionsLeaguePublicProduct(payload, {
  nowMs = Date.now(), maxAgeSeconds = null, requireRelease = true,
  requireHealth = false, requirePublished = false,
} = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('public product must be an object');
  const records = Array.isArray(payload.football) ? payload.football.filter((record) => record && typeof record === 'object' && CL_LEAGUE_CODES.has(String(record.league || '').toLowerCase())) : [];
  const rawRelease = payload.champions_league_release;
  if (requireRelease && !rawRelease) throw new Error('CL release envelope is required');
  const release = rawRelease ? validateRelease(rawRelease, { nowMs, maxAgeSeconds, requirePublished }) : null;
  const effectiveMaxAgeSeconds = maxAgeSeconds ?? (release ? release.stale_after_seconds : null);
  if (!records.length) throw new Error('CL public records are missing');
  const groups = new Map();
  for (const record of records) {
    const result = validateRecord(record, { nowMs, maxAgeSeconds: effectiveMaxAgeSeconds, release });
    const key = `${result.predictionId}\u0000${result.fixtureKey}`;
    if (!groups.has(key)) groups.set(key, new Set());
    groups.get(key).add(result.market);
  }
  for (const markets of groups.values()) if (markets.size !== CL_MARKETS.size || [...CL_MARKETS].some((market) => !markets.has(market))) throw new Error('each CL prediction must contain home/draw/away');
  if (release) {
    if (release.prediction_count !== groups.size) throw new Error('CL prediction_count does not match records');
    if (release.fixture_count !== new Set([...groups.keys()].map((key) => key.split('\u0000')[1])).size) throw new Error('CL fixture_count does not match records');
    if (requireHealth) validateHealth(payload.health, release);
  }
  if (effectiveMaxAgeSeconds != null && payload.updated) ageCheck(payload.updated, 'updated', nowMs, effectiveMaxAgeSeconds);
  return {
    schema_version: CL_PUBLICATION_SCHEMA,
    league_code: CL_CANONICAL_LEAGUE,
    prediction_count: groups.size,
    fixture_count: new Set([...groups.keys()].map((key) => key.split('\u0000')[1])).size,
    publication_enabled: Boolean(release && release.publication_enabled),
    publication_status: release ? release.publication_status : 'UNPUBLISHED',
  };
}
