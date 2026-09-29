# SportsBrain product analytics

The PWA sends a small anonymous event vocabulary through `docs/js/analytics.js`.
The wrapper is fail-open: if the PostHog SDK, network, or CSP is unavailable,
the product UI continues normally.

## Event contract

| Event | Allowed properties |
| --- | --- |
| `pwa_opened` | `app_version`, `display_mode`, `source` |
| `tab_opened` | `tab`, `previous_tab` |
| `match_opened` | `sport`, `competition`, `fixture_id`, `lifecycle_stage`, `source_view` |
| `prediction_viewed` | `sport`, `competition`, `fixture_id`, `lifecycle_stage`, `model_version`, `confidence`, `source_view` |
| `signal_opened` | `sport`, `competition`, `fixture_id`, `lifecycle_stage`, `signal_status`, `confidence`, `source_view` |
| `nl_match_opened` | `fixture_id`, `lifecycle_stage`, `publication_state`, `source_view` |
| `model_diagnostics_opened` | `sport`, `competition`, `fixture_id`, `model_version`, `lifecycle_stage` |
| `frontend_error` | `error_type`, sanitized `message`, `source_file`, `app_version`, `active_view` |

Only explicit allowlisted properties are retained. The wrapper never calls
`identify()`, never sends private SportsBrain state, access tokens, bankroll,
stakes, betting history, raw model vectors, usernames, or email addresses.
Autocapture, pageview capture, session replay, surveys, experiments, and
feature flags are disabled. Repeated lifecycle events are deduplicated in the
current page session where one interaction can pass through multiple helpers.

## Product Health dashboard

The intended dashboard is `SportsBrain — Product Health` with trends for
`pwa_opened`, `tab_opened`, `match_opened`, `prediction_viewed`,
`signal_opened`, `nl_match_opened`, `model_diagnostics_opened`, and
`frontend_error`. Intended funnels are:

`pwa_opened → tab_opened → match_opened → prediction_viewed`

`prediction_viewed → model_diagnostics_opened`

The connected PostHog project is the EU project and the browser uses only its
public project token. The available PostHog MCP in this environment exposed
project read access but not a dashboard/insight creation operation; the
dashboard must therefore be created in the PostHog UI or through a later
supported project-management connector. No experiments, feature flags,
surveys, or session replay configuration was created by this change.
