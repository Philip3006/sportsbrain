/* SportsBrain anonymous product analytics. */
(function initSportsBrainAnalytics(root) {
  'use strict';
  const POSTHOG_TOKEN = 'phc_nh3VcS7sLg8vQHE6nmfja5sfUcaSu9SXKCSVzjSa6d6M';
  const POSTHOG_HOST = 'https://eu.i.posthog.com';
  const TEST_MODE = root.__SB_ANALYTICS_TEST__ === true;
  const seen = new Set();
  const events = TEST_MODE ? (root.__sbAnalyticsEvents = []) : null;
  const schemas = {
    pwa_opened: ['app_version', 'display_mode', 'source'],
    tab_opened: ['tab', 'previous_tab'],
    match_opened: ['sport', 'competition', 'fixture_id', 'lifecycle_stage', 'source_view'],
    prediction_viewed: ['sport', 'competition', 'fixture_id', 'lifecycle_stage', 'model_version', 'confidence', 'source_view'],
    signal_opened: ['sport', 'competition', 'fixture_id', 'lifecycle_stage', 'signal_status', 'confidence', 'source_view'],
    nl_match_opened: ['fixture_id', 'lifecycle_stage', 'publication_state', 'source_view'],
    model_diagnostics_opened: ['sport', 'competition', 'fixture_id', 'model_version', 'lifecycle_stage'],
    frontend_error: ['error_type', 'message', 'source_file', 'app_version', 'active_view'],
  };
  function safeString(value, max = 120) {
    if (typeof value !== 'string') return undefined;
    const clean = value.replace(/[\r\n\t]+/g, ' ')
      .replace(/Bearer\s+[^\s]+(?:\s+[^\s]+)?/gi, 'Bearer [redacted]')
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[redacted-email]')
      .replace(/https?:\/\/[^\s]+/gi, '[redacted-url]');
    return clean.slice(0, max);
  }
  function sanitize(event, properties) {
    const allowed = schemas[event];
    if (!allowed) return {};
    const output = {};
    for (const key of allowed) {
      const value = properties && properties[key];
      if (typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) output[key] = value;
      else { const clean = safeString(value); if (clean !== undefined) output[key] = clean; }
    }
    return output;
  }
  function loadPostHog() {
    if (TEST_MODE || root.posthog) return;
    try {
      const posthog = [];
      posthog._i = [];
      posthog.__SV = 1;
      ['capture', 'captureException', 'register', 'register_once', 'unregister', 'reset',
        'opt_out_capturing', 'opt_in_capturing', 'has_opted_out_capturing', 'get_distinct_id',
        'get_property', 'get_session_id', 'on', 'off', 'startSessionRecording'].forEach((method) => {
        posthog[method] = function queuedMethod() { posthog.push([method].concat(Array.from(arguments))); };
      });
      posthog.init = function init(token, options, name) { posthog._i.push([token, options, name]); };
      root.posthog = posthog;
      const script = document.createElement('script');
      script.async = true;
      script.crossOrigin = 'anonymous';
      script.src = `${POSTHOG_HOST}/static/array.js`;
      script.onerror = () => {};
      document.head.appendChild(script);
      posthog.init(POSTHOG_TOKEN, {
        api_host: POSTHOG_HOST,
        autocapture: false,
        capture_pageview: false,
        capture_pageleave: false,
        disable_session_recording: true,
        disable_surveys: true,
        advanced_disable_flags: true,
        persistence: 'memory',
      });
    } catch (_) {}
  }
  function capture(event, properties = {}) {
    if (!schemas[event]) return false;
    const payload = sanitize(event, properties);
    if (TEST_MODE) { events.push({ event, properties: payload }); return true; }
    try { if (root.posthog && typeof root.posthog.capture === 'function') root.posthog.capture(event, payload); } catch (_) {}
    return true;
  }
  function captureOnce(key, event, properties = {}) {
    const dedupeKey = `${event}:${key}`;
    if (seen.has(dedupeKey)) return false;
    seen.add(dedupeKey);
    return capture(event, properties);
  }
  function activeView() { return document.querySelector('.nav-tab.active')?.dataset.view || undefined; }
  function appVersion() { return safeString(document.querySelector('meta[name="sportsbrain-build"]')?.content); }
  root.sbAnalytics = Object.freeze({ capture, captureOnce, activeView, appVersion, _schemas: schemas });
  if (!TEST_MODE) loadPostHog();
  captureOnce('page-load', 'pwa_opened', {
    app_version: appVersion(),
    display_mode: root.matchMedia?.('(display-mode: standalone)').matches ? 'standalone' : 'browser',
    source: 'pwa',
  });
  root.addEventListener('error', (event) => capture('frontend_error', {
    error_type: 'window_error', message: safeString(event.message),
    source_file: safeString(event.filename?.split('/').pop()), app_version: appVersion(), active_view: activeView(),
  }));
  root.addEventListener('unhandledrejection', (event) => capture('frontend_error', {
    error_type: 'unhandled_rejection', message: safeString(event.reason instanceof Error ? event.reason.message : String(event.reason || 'unhandled_rejection')),
    app_version: appVersion(), active_view: activeView(),
  }));
  const captureDiagnostics = (target) => {
    if (!target?.open) return;
    captureOnce(target.dataset.fixtureId || 'unknown', 'model_diagnostics_opened', {
      sport: target.dataset.sport, competition: target.dataset.competition, fixture_id: target.dataset.fixtureId,
      model_version: target.dataset.modelVersion, lifecycle_stage: target.dataset.lifecycleStage,
    });
  };
  document.addEventListener('toggle', (event) => {
    if (event.target?.matches?.('details[data-analytics="model-diagnostics"]')) captureDiagnostics(event.target);
  }, true);
  document.addEventListener('click', (event) => {
    const target = event.target?.closest?.('details[data-analytics="model-diagnostics"]');
    if (target) root.setTimeout(() => captureDiagnostics(target), 0);
  }, true);
})(window);
