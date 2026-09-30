(function (global) {
  'use strict';

  // This is the public PostHog browser project token for Sportsbrain. It is
  // intentionally safe to ship in the static PWA; no private/API/Worker key
  // is ever used here.
  const PUBLIC_PROJECT_TOKEN = 'phc_nh3VcS7sLg8vQHE6nmfja5sfUcaSu9SXKCSVzjSa6d6M';
  const INGESTION_HOST = 'https://eu.i.posthog.com';
  const EVENTS = {
    pwa_opened: ['build_sha', 'app_version', 'display_mode'],
    tab_opened: ['tab', 'previous_tab'],
    match_opened: ['sport', 'competition', 'fixture_id', 'fixture_key', 'lifecycle_stage', 'source_view'],
    prediction_viewed: ['sport', 'competition', 'fixture_id', 'fixture_key', 'lifecycle_stage', 'model_version', 'confidence', 'source_view'],
    signal_opened: ['sport', 'competition', 'fixture_id', 'fixture_key', 'lifecycle_stage', 'signal_status', 'confidence', 'source_view'],
    nl_match_opened: ['fixture_id', 'fixture_key', 'lifecycle_stage', 'publication_state', 'shadow_state', 'source_view'],
    model_diagnostics_opened: ['sport', 'competition', 'fixture_id', 'fixture_key', 'lifecycle_stage', 'model_version'],
    frontend_error: ['error_type', 'message', 'source', 'component', 'active_view', 'build_sha'],
  };
  const MAX_TEXT = 120;
  const state = {
    initialized: false,
    endpoint: null,
    transport: null,
    distinctId: null,
    once: new Set(),
    observed: [],
  };

  function text(value) {
    if (typeof value !== 'string') return null;
    const cleaned = value
      .replace(/[\u0000-\u001f\u007f]/g, ' ')
      .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, '[redacted]')
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[redacted-email]')
      .replace(/https?:\/\/[^\s]+/gi, '[redacted-url]')
      .replace(/\s+/g, ' ')
      .trim();
    return cleaned ? cleaned.slice(0, MAX_TEXT) : null;
  }

  function safeBuildValue(name) {
    try {
      const meta = global.document?.querySelector?.(`meta[name="${name}"]`);
      return text(meta?.content || global[name] || '');
    } catch (_) {
      return null;
    }
  }

  function activeView() {
    try {
      return text(global.document?.body?.dataset?.activeView ||
        global.document?.querySelector?.('.nav-tab.active')?.dataset?.view || '');
    } catch (_) {
      return null;
    }
  }

  function anonymousId() {
    try {
      if (global.crypto?.randomUUID) return global.crypto.randomUUID();
    } catch (_) {}
    return 'sb-anon-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function defaultTransport(endpoint, payload) {
    if (typeof global.fetch !== 'function') return Promise.resolve();
    try {
      return global.fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'omit',
        keepalive: true,
        body: JSON.stringify(payload),
      }).catch(() => undefined);
    } catch (_) {
      return Promise.resolve();
    }
  }

  function init(config = {}) {
    if (state.initialized) return true;
    if (global.SB_ANALYTICS_DISABLED === true) {
      state.initialized = true;
      state.transport = () => Promise.resolve();
      return true;
    }
    const token = config.token || PUBLIC_PROJECT_TOKEN;
    const host = config.host || INGESTION_HOST;
    if (!/^phc_[A-Za-z0-9._-]+$/.test(token) || host !== INGESTION_HOST) return false;
    try {
      state.endpoint = host.replace(/\/+$/, '') + '/capture/';
      state.distinctId = anonymousId();
      state.transport = config.transport || defaultTransport;
      state.initialized = true;
      return true;
    } catch (_) {
      state.initialized = false;
      state.transport = null;
      return false;
    }
  }

  function sanitizeProperties(eventName, properties) {
    const allowed = EVENTS[eventName];
    if (!allowed) return null;
    const out = {};
    for (const key of allowed) {
      const value = properties && properties[key];
      if (typeof value === 'boolean') out[key] = value;
      else if (typeof value === 'number' && Number.isFinite(value)) out[key] = value;
      else {
        const clean = text(value);
        if (clean) out[key] = clean;
      }
    }
    return out;
  }

  function capture(eventName, properties = {}, options = {}) {
    if (!EVENTS[eventName]) return false;
    const onceKey = options && typeof options.onceKey === 'string' ? options.onceKey : null;
    if (onceKey && state.once.has(onceKey)) return false;
    if (onceKey) state.once.add(onceKey);
    const safe = sanitizeProperties(eventName, properties);
    if (!safe) return false;
    const event = {
      event: eventName,
      properties: { ...safe, distinct_id: state.distinctId || anonymousId() },
    };
    // Keep a bounded in-memory observation hook for browser tests and local
    // diagnostics. It is never persisted and contains only the allow-list.
    state.observed.push({ event: eventName, properties: { ...safe } });
    if (state.observed.length > 50) state.observed.shift();
    if (!state.initialized && !init()) return true;
    try {
      Promise.resolve(state.transport?.(state.endpoint, {
        api_key: PUBLIC_PROJECT_TOKEN,
        event: eventName,
        properties: event.properties,
      })).catch(() => undefined);
    } catch (_) {}
    return true;
  }

  const api = { init, capture };
  api.__test = {
    events: () => state.observed.map((item) => ({ event: item.event, properties: { ...item.properties } })),
    reset: () => { state.once.clear(); state.observed.length = 0; },
    setTransport: (transport) => { state.transport = transport; state.initialized = true; state.endpoint = 'test://posthog'; state.distinctId = 'test-anonymous'; },
  };
  global.sbAnalytics = api;

  function captureFrontendError(errorType, message, source, component) {
    capture('frontend_error', {
      error_type: errorType,
      message: text(message) || 'unexpected frontend error',
      source: text(source) || 'window',
      component: text(component) || 'runtime',
      active_view: activeView(),
      build_sha: safeBuildValue('sb-build-sha'),
    });
  }

  try {
    global.addEventListener?.('error', (event) => {
      // Ignore resource errors: they do not identify an application exception
      // and their target can contain arbitrary DOM-owned data.
      if (event?.target && event.target !== global) return;
      captureFrontendError('runtime_error', event?.error?.message || event?.message, 'window', 'runtime');
    }, true);
    global.addEventListener?.('unhandledrejection', (event) => {
      const reason = event?.reason;
      captureFrontendError('unhandled_rejection', reason?.message || String(reason || ''), 'window', 'promise');
    });
    global.document?.addEventListener?.('toggle', (event) => {
      const details = event?.target;
      if (!details?.open || details.dataset?.analyticsDiagnostics !== 'true') return;
      capture('model_diagnostics_opened', {
        sport: details.dataset.sport,
        competition: details.dataset.competition,
        fixture_key: details.dataset.fixtureKey,
        lifecycle_stage: details.dataset.lifecycleStage,
        model_version: details.dataset.modelVersion,
      });
    }, true);
  } catch (_) {}

  init();
  capture('pwa_opened', {
    build_sha: safeBuildValue('sb-build-sha'),
    app_version: safeBuildValue('sb-app-version'),
    display_mode: (() => {
      try { return global.matchMedia?.('(display-mode: standalone)').matches ? 'standalone' : 'browser'; }
      catch (_) { return 'browser'; }
    })(),
  }, { onceKey: 'pwa_opened' });
})(typeof window !== 'undefined' ? window : globalThis);
