# SportsBrain PWA analytics

The PWA uses the centralized `docs/js/analytics.js` module. It sends only the
explicitly allow-listed product events to the EU PostHog capture endpoint with
the public browser project token for project `287510`.

The transport is anonymous and fail-open:

- the anonymous `distinct_id` exists only in memory for the page load;
- no `identify`, Session Replay, feature flags, experiments, surveys, cookies,
  or local-storage persistence are enabled;
- network, loader, and initialization failures are ignored by the application;
- unknown properties are dropped before transport;
- private state, betting values, credentials, request/response bodies, and
  arbitrary DOM text are never sent.

Event ownership is semantic: navigation owns `tab_opened`, match-detail entry
owns `match_opened`, the signal-detail bridge owns `signal_opened`, prediction
rendering owns `prediction_viewed`, and explicit diagnostic `<details>`
expansion owns `model_diagnostics_opened`. `pwa_opened` is once per page load;
other interactions may be recorded again when the user genuinely repeats them.

The in-memory `sbAnalytics.__test` hook is bounded and contains only sanitized
allow-listed events. It is used by frontend tests and does not persist data.
