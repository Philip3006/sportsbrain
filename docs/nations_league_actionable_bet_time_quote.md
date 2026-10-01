# Nations League bet-time value quote

The immutable Nations League `LIVE` prediction artifact remains `no_bet=true`,
`betting_enabled=false`, and `ledger_mutation=false`.  A separate
`nations_league-actionable-value-signals-v1` projection is created only from a
fresh iSports schedule-plus-European-odds capture for current `refinement`
fixtures.  Its production authority remains `the_odds_api`; iSports is
evidence/quote provenance only.

## Operator sequence

1. Create a fresh authorization file with the exact current refinement fixture
   IDs in canonical order.  It must use
   `nations-league-bet-time-quote-authorization-v1`, `provider=isports_api`,
   `maximum_request_count=2`, `retry_count=0`, `no_bet=true`, and false
   publication/activation/betting flags.  Compute `authorization_digest` with
   `canonical_evidence_digest` over the payload without that field.
2. Run the zero-network preflight:

   ```text
   python3 scripts/capture_nations_league_bet_quote.py \
     --input /secure/current-signals.json \
     --authorization-file /secure/nl-quote-authorization.json \
     --output /secure/nl-actionable-value-signals.json
   ```

   It must print `PREFLIGHT_READY`, `request_count=0`, and
   `credential_access_count=0`.
3. Only after reviewing that output, run the same command with
   `--execute-network`.  This performs exactly one schedule request and one
   bulk European-odds request through the reviewed iSports transport.  It does
   not use Main Odds, a per-match loop, fallback, retry, lifecycle mutation, or
   provider-authority change.
4. Review the immutable output projection.  It must validate with
   `validate_nations_league_actionable_projection` and be merged into trusted
   Worker state only through the master-only
   `POST /signals?merge_nations_league_value_signals=1` endpoint.  The request
   body contains exactly one key: `nations_league_value_signals`.

The browser treats the projection as value-signal input only after validating
its digest and safety fields.  Stale, malformed, non-`refinement`, non-current
odds, or non-actionable quotes do not produce a value CTA.  `/pending_bets`
continues to re-resolve the signal from Worker state and remains the sole
authoritative handoff to the existing bet consumer/ledger path.

## Fixed safety bounds

- iSports requests: exactly 2 on success (schedule, bulk European odds)
- retries: 0
- quote freshness: at most 30 minutes at projection time
- complete regulation 1X2 only; pre-match only
- existing football minimum-edge, `MAX_EV`, bankroll, active-bet, and 5% cap
  gates remain authoritative
- production authority: `the_odds_api`
- publication, activation, and ledger mutation: false
- no external bookmaker submission is implemented

The projection is a public-safe actionability view, not a rewrite of the
underlying prediction or market evidence.  It contains only redacted request
provenance and snapshot digests; the API credential is never serialized.
