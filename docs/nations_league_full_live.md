# Nations League v1.1 LIVE lifecycle

The canonical active release is read from
`results/audits/continuous_model_lifecycle_registry.json`.  The committed
registry has exactly one ACTIVE `nations_league_v1_1` pointer and is validated
before a prediction can be materialized.  Release changes are immutable and
must be committed atomically; rollback uses the previous release pointer.

Each 15-minute cycle is ordered as:

1. rebuild a fresh input-state at the actual UTC capture cutoff from the
   official UEFA result continuation; only a READY completeness proof with
   `result_safe_available_at < prediction_cutoff` is accepted;
2. return `NO_OP` when the sealed training data/state digest is unchanged,
   otherwise build, validate, and atomically activate a new causal release;
3. load the newest ACTIVE release;
4. classify verified fixtures using INITIAL (22–26 hours) or REFINEMENT
   (60–120 minutes) windows;
5. materialize idempotent LIVE records bound to the release and sealed
   input-state; and
6. pass the resulting public bundle through the existing serializer/staging
   boundary.

The runner's default is plan-only.  The scheduled workflow uses the explicit
offline execution mode, the durable append-only store
`results/research/nations_league_v1_1_live_prediction_store.jsonl`, and the
committed lifecycle registry.  It does not call an odds provider, activate
betting, mutate a ledger, or create a wager.  A record is always
`no_bet=true`, `betting_enabled=false`, and `ledger_mutation=false`.

The current static projection is materialized with:

```text
python3 scripts/build_nations_league_live_public.py \
  --campaign results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json \
  --registry results/audits/continuous_model_lifecycle_registry.json \
  --binding results/audits/nations_league_v1_1_live_evidence_binding.json \
  --store results/research/nations_league_v1_1_live_prediction_store.jsonl \
  --input docs/data/signals.json \
  --output docs/data/signals.json
```

The seven existing INITIAL records are copied exactly.  A refinement replaces
the current public fixture view while remaining in `audit_history`; old
records are never rewritten.  A kicked-off or expired fixture is not current,
but remains in the audit store.
