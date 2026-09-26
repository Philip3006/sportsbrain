# Top-5 controlled activation runbook

Status: prepared for future CEO-authorized use only. The current repository
state remains disabled, no-bet, unpublished, and unregistered. The runbook is
an evidence checklist; it does not authorize a run.

The only approved name for a manually authorized execution is **CONTROLLED
ACTIVATION RUNS**. Do not call these runs a natural canary or a natural
scheduled run.

## Scope and prerequisites

The evidence package remains exactly five leagues (`BL1`, `EPL`, `LL`, `SA`,
`L1`); a controlled activation plan selects exactly one of those leagues.
Evidence for fewer than all five leagues is rejected. The production authority
remains `the_odds_api`; `therundown_experimental` is never a production route.

- Confirm the exact production source SHA and the frozen Research SHA.
- The currently referenced frozen Research SHA is
  `6eaabbec7d0182103d815c72fae4976e261b40aa`; it is not modified here.
- Confirm the exact model artifact hash and candidate/model identity.
- Confirm the selected league-scoped provider authority for fixtures, odds, and
  results; odds authority must be exactly `the_odds_api`.
- Confirm the approved signal-time contract, markets, regions, cadence, and
  retry budget.
- Confirm the cumulative rollout evidence through CEO approval.
- Confirm the immutable configuration snapshot and rollback pointer.
- Revalidate the full B2 five-league receipt package and the original B1 final
  acceptance bundle; do not rely on operator-supplied readiness booleans.
- Supply a separate Signal-Time approval identity and verify it against the
  exact approved contract.
- Confirm that the model adapter, provider client, scheduler, publisher, and
  ledger boundaries are the intended explicitly injected dependencies.

## CEO gate

The CEO authorization must identify the league, candidate, model artifact,
source SHA, Research SHA, provider authority, signal-time contract, rollback
pointer, and exact scope. A blank, stale, or mismatched authorization fails
closed. Authorization is not inferred from a passing shadow metric.

## Preflight

1. Validate the cumulative state machine:
   `research_approved` → `adapter_ready` → `offline_compatible` →
   `shadow_inference` → `signal_time_validated` → `provider_validated` →
   `shadow_performance_validated` → `ceo_approved`.
2. Validate source, Research, and model hashes.
3. Validate league/provider/signal-time identity.
4. Confirm closing odds are excluded from inference.
5. Confirm publication is disabled until its separate policy is approved.
6. Confirm no-bet behavior, rollback readiness, and external runtime-state
   ownership.

The durable control commands are:

```text
chmod 600 <exact-evidence-and-authorization.json>
python3 scripts/top5_controlled_activation.py prepare --input <absolute-path-to-exact-evidence-and-authorization.json>
python3 scripts/top5_controlled_activation.py status [--activation-id <id>]
python3 scripts/top5_controlled_activation.py execute --input <same-absolute-path> --activation-id <id> --execute
python3 scripts/top5_controlled_activation.py rollback --activation-id <id> --plan-digest <exact-plan-digest>
```

The default path is non-executing; `execute` requires the explicit flag and
revalidates the exact B2 package, B1 bundle, activation authorization, Signal-
Time identity, and pre-activation snapshot. The state file is external,
owner-only, atomically replaced, digest-checked, and idempotent. The token in
the authorization input is never persisted. Current `main` has no genuine
one-shot production model/provider runtime, so `execute --execute` fails
closed with `NO_PRODUCTION_ONE_SHOT_MODEL_PROVIDER_RUNTIME` before state or
provider access. The current authorization contract validates the supplied
authorization fields but has no separate cryptographic Philip-signature
verifier; no execution path may rely on this control record as authenticated
authorization until that verifier and the production runtime are reviewed.
Do not treat a PREPARED record as an activation.

No recurring scheduler is registered or required for a future manual
one-shot canary. This PR does not add scheduler wiring.

## Activation

If a separately reviewed production runtime is later wired and a fresh explicit
authorization is supplied, execute one league only from the exact prepared
snapshot. Record the source SHA, Research SHA, model hash, `the_odds_api`
authority, Signal-Time approval identity, and rollback pointer before the
first production cycle. This runbook does not authorize or perform that step.

Do not expand league scope, change cadence, bind a different model, add a
provider, or enable publication in the same run.

## Verification

- Verify the selected league and fixture identity.
- Verify signal-time snapshots are fresh and signal-relative.
- Verify inference inputs contain no closing snapshot.
- Verify every prediction has complete provenance.
- Verify output remains no-bet until the separately approved policy says
  otherwise.
- Verify publisher and PWA contracts expose only the approved Top-5
  namespace.

## Health checks

Record league health, provider health, fixture and signal-time coverage, odds
freshness, inference health and latency, publisher health, result-source
health, fallback/retry/stale/duplicate rates, quota/cost usage, last
successful cycle, and activation state.

## Rollback

Any provider failure, stale market, inference error, malformed prediction,
publication error, scheduler error, health degradation, duplicate signal,
wrong league mapping, or model provenance mismatch invokes the rollback
controller. The required result is the last known safe disabled state:

- activation mode `disabled`;
- no-bet `true`;
- publication `false`;
- scheduler `false`;
- ledger mutation `false`.

The durable rollback command is scoped to the exact activation ID and plan
digest. It retains the evidence record and restores the saved disabled state
with publication, scheduler, betting, and ledger flags false. It does not
delete files, reset Git, or mutate the financial ledger. Because `main` has no
live Top-5 route writer/consumer, this is a verified durable control-state
rollback only; live routing rollback cannot yet be claimed ready. The missing
route writer must be reviewed together with the one-shot production runtime.

## Production verification and evidence capture

Capture the preflight payload, authorization reference, source/Research/model
hashes, provider and timing configuration, health payload, prediction and
signal provenance, publisher/PWA validation, rollback result, and the final
CEO review record. Keep closing-odds and CLV-style artifacts benchmark-only.
