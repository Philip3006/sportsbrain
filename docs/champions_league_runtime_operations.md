# Champions League runtime operations

The Champions League operations boundary is intentionally disabled by default.
It reuses the generic Football identity, market, prediction, and shadow-signal
contracts while keeping the CL lifecycle separate from the Top-5 provider
cascade.

## Current contract

- league code: `UCL`
- The Odds API sport key: `soccer_uefa_champs_league`
- production provider authority: `the_odds_api`
- candidate provider: not registered here
- activation mode: `disabled`
- network, scheduler, publication, betting, and ledger flags: `false`
- automatic retries: `0` (`retry_attempts=1` means one attempt)
- timeout: `5` seconds, supplied as metadata for a future injected caller
- signal-time odds freshness: at most `900` seconds

The CL sport key remains a catalogue entry and is deliberately absent from the
live league registry. No scheduler or provider client is imported by the CL
runtime module.

## Offline lifecycle

`ChampionsLeagueRuntime.run_offline()` accepts an injected
`ChampionsLeagueRunBundle` and validates the complete sequence:

1. fixture discovery
2. pre-match scan
3. odds refresh using signal-time odds only
4. prediction dispatch with the unbound model contract
5. shadow signal lifecycle (`no_bet=true`)
6. result ingestion
7. non-financial settlement

The lifecycle store uses idempotency keys of `run_id:phase`. A replay with the
same bundle is a no-op and returns the same report; a changed bundle with the
same run identity fails closed. The in-memory store is a test/replay seam, not
operator runtime state.

Rollback records a non-financial `rolled_back` marker. It does not write a
ledger, publish an artifact, alter a scheduler, or call Cloudflare.

## Future activation prerequisites

Before a separate release decision can connect real dependencies, the operator
must provide current health for fixture discovery, pre-match scan, odds refresh,
prediction dispatch, signal lifecycle, result ingestion, settlement,
idempotency, scheduler contract, and rollback. These checks are informational
only in the current contract: `activation_allowed` remains `false` even when
all offline health fields are green.

The future integration must continue to use The Odds API as the production
authority, bind Signal-Time snapshots to the fixture identity, and keep closing
odds outside prediction input. No live CL execution is enabled by this change.
