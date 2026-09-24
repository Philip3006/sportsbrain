# Top-5 Final Acceptance

`src/football/top5_final_acceptance.py` is the read-only composition boundary
for the final Top-5 launch acceptance package. It consumes already-produced
B4 quota proof, five-league discovery evidence, the completed controlled-shadow
run, model/runtime provenance, and Worker/static public payloads.

The command is side-effect-free:

```text
python3 scripts/top5_final_acceptance.py BUNDLE.json \
  --expected-source-main-sha CURRENT_MAIN_SHA \
  --now 2026-09-20T12:00:00Z
```

Success is `TOP5_FINAL_ACCEPTANCE_VERIFIED` and includes a deterministic
manifest digest. Any missing, stale, synthetic, mismatched, candidate-authority,
or unsafe input returns `TOP5_FINAL_ACCEPTANCE_BLOCKED` with a precise reason.

The gate does not select `therundown_experimental` as authority. Production
authority must remain `the_odds_api`; closing odds remain benchmark-only and
cannot enter prediction input. It never performs network requests, writes
Worker/KV state, publishes, activates, places bets, or mutates the ledger.
