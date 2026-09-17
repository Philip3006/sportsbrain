# SportsBrain Night Shift Dispatcher V1

Builder 5 owns the dispatcher. It is a control-plane component, not a worker,
and it cannot be dispatched recursively. V1 explicitly registers only:

| Builder | Governed responsibility |
| --- | --- |
| Builder 1 | Research / Shadow / Evidence Lifecycle |
| Builder 2 | Independent Qualification / Authority |
| Builder 3 | Memory / Context / Observability |
| Builder 4 | Provider Cascade / Controlled Shadow Infrastructure |

Builders 6, 7, and 8 are not registered. The runtime does not claim that they
exist, and no task template targets them.

## Boundaries

The dispatcher owns task admission, durable state, leases, retry scheduling,
approval gates, dependency ordering, and audit history. A worker adapter owns
the actual Builder implementation and is injected through the Python API.

The dispatcher deliberately does not:

- discover workers from packages, names, branches, prompts, or installed tools;
- execute arbitrary shell text;
- merge, deploy, publish, reset, rebase, or delete repository content;
- modify an active Builder 1–4 branch or pull request;
- infer a new Builder role from a task or template.

The governed delivery gate may commit and push only the unique task branch
from its isolated worktree, then query or create exactly one pull request for
that task. It never pushes `main`, another Builder branch, or with force.

The governed files in `config/night_shift/` are the authority boundary. A
future Builder may be added only by a reviewed registry entry, reviewed task
templates, and explicit roadmap entries. The queue schema, state machine, lease
handling, and audit chain do not depend on the number of registered workers.

`StaticBootstrapProvider` is the V1 context seam. `MemoryV4BootstrapProvider`
is present only as an explicit future seam; this branch does not depend on
Memory PR #6 and does not copy Memory retrieval logic.

## Task lifecycle

```text
BACKLOG ──approve──> READY ──claim──> CLAIMED ──start──> RUNNING
   │                    │                                  │
   └─reject──> FAILED_SAFE       └─dependency──> WAITING_DEPENDENCY
                                                         │
RUNNING ──verify──> VERIFYING ──delivery──> PR_READY ──> CEO_REVIEW
                                      └─verified merge──> COMPLETED
   │                       └─read-only delivery──> COMPLETED ──> CEO_REVIEW
   ├─retry──> READY
   ├─unsafe/auth gate──> BLOCKED
   └─unsafe/stale/failed verification──> FAILED_SAFE

READY/WAITING_DEPENDENCY ──cancel──> CANCELLED
```

The external state model is exactly `BACKLOG`, `READY`,
`WAITING_DEPENDENCY`, `CLAIMED`, `RUNNING`, `VERIFYING`, `PR_READY`,
`CEO_REVIEW`, `BLOCKED`, `FAILED_SAFE`, `COMPLETED`, and `CANCELLED`.
Retry timing is orthogonal metadata, not an extra state. Code-changing work
must pass the independent verification gate, receive a deterministic commit,
push only its task branch, and have one real PR before `PR_READY`. Queue-level
exhaustion is reported as `IDLE_SAFE` or `INTENTIONAL_IDLE` when an explicit
roadmap has no eligible work. `MERGE_BACKPRESSURE` stops new roadmap selection
while PRs await CEO review. Unsafe scope, stale worker ownership, or failed
verification produces `FAILED_SAFE`; CEO authorization and prohibited work
remain `BLOCKED` and cannot be released by `unblock`.

Every transition is committed in the same SQLite transaction as its audit
event. Leases are owned by an explicit worker instance, have an expiry, and
must be heartbeated. An expired lease is requeued while attempts remain or is
dead-lettered when the attempt budget is exhausted. A late completion from a
stale owner is rejected.

Tasks support explicit dependencies, priority, bounded attempts, parent task
identity, and idempotency keys. A task is not claimable until every dependency
is `COMPLETED`. `PR_READY` and `CEO_REVIEW` are not dependency satisfaction:
they represent unmerged work waiting for CEO action. Failed, cancelled,
blocked, or dead-lettered dependencies block the dependent task rather than
allowing it to run with incomplete context. Independent roadmap items remain
eligible while a parent PR waits for merge.

A PR-backed task reaches `COMPLETED` after the operator runs
`reconcile-merged TASK_ID --actor OPERATOR`. The command performs a read-only
GitHub query and requires exact repository, base branch/base SHA, task branch,
commit SHA, remote SHA, and recorded PR-number binding with `merged=true`.
GitHub or binding verification failure is fail-closed. Successful
reconciliation records durable `merge_verified` evidence; it never merges,
approves, pushes, or deploys.

## Safety gates

1. Builder target must resolve in `builders.json`; `builder-5` is rejected at
   registry, submit, claim, and worker-adapter boundaries.
2. Repository, branch prefix, task type, and risk class must match the
   target’s explicit allowlists.
3. Read-only tasks may be queued directly. Code-changing tasks require
   approval and use a `nightshift/builder-N/` branch namespace.
4. External side effects and destructive tasks are disabled in V1.
5. Queue capacity, payload size, dependency count, attempt count, lease time,
   and worker concurrency are bounded.
6. Merge/deploy/push/reset/rebase/delete intent is rejected before persistence.
7. Pause is a durable kill switch: claims return no task until an authorized
   operator resumes the dispatcher.
8. The audit event stream is hash chained. `verify_audit_chain()` fails if an
   event is altered or removed.

Each production claim first validates and fetches the dedicated bare control
repository at `~/Library/Application Support/SportsBrain/night-shift/repo.git`,
resolves `origin/<base_branch>` to an authoritative SHA, compares it with
`expected_base_sha` when supplied, and allocates a unique
`nightshift/<builder>/<task>` branch/worktree below
`~/Library/Application Support/SportsBrain/night-shift/worktrees/`. The
production checkout is inspected only as a runtime surface. The explicit
`runtime_dirty.json` policy allows known evidence-backed writer paths and
hard-blocks `UNEXPECTED_SOURCE_DIRTY`; Git operations never depend on the
production checkout HEAD. Worktree diagnostics are retained under the runtime
state directory; the dispatcher never pushes or merges them.

Provision isolation once with `python3 scripts/night_shift_control_repo.py`.
This fetches current `origin/main` into the bare control repository and sets a
non-secret local commit identity used only for task branches. It does not pause
or edit any runtime writer LaunchAgent.

## Operating the queue

Use an external state path for a long-running installation. The default is
`~/Library/Application Support/SportsBrain/runtime-state/nightshift.sqlite3`;
`SPORTSBRAIN_NIGHTSHIFT_STATE` may override it only with an absolute path.

```bash
python3 scripts/night_shift_dispatcher.py builders
python3 scripts/night_shift_dispatcher.py templates
python3 scripts/night_shift_dispatcher.py status
python3 scripts/night_shift_dispatcher.py doctor
python3 scripts/night_shift_dispatcher.py roadmap
python3 scripts/night_shift_dispatcher.py blocked
python3 scripts/night_shift_dispatcher.py pr-ready
python3 scripts/night_shift_dispatcher.py workers

python3 scripts/night_shift_dispatcher.py enqueue-template \
  builder-1.evidence-lifecycle-audit \
  --branch nightshift/builder-1/2026-09-16-audit \
  --payload '{"scope":"Top-5 shadow evidence"}' \
  --idempotency-key b1-top5-evidence-audit-20260916

python3 scripts/night_shift_dispatcher.py approve TASK_ID \
  --actor philip --reason "Night-shift scope reviewed"
python3 scripts/night_shift_dispatcher.py claim builder-1
python3 scripts/night_shift_dispatcher.py recover
python3 scripts/night_shift_dispatcher.py audit --limit 50
```

`claim` only leases work. Production worker processes call
`NightShiftDispatcher.run_autonomous_cycle()` with a reviewed, non-shell
adapter, send heartbeats for long work, and return an `ExecutionResult`. A
queue record, process exit, or worker prose alone is not success. For every task,
`required_tests`, structured-argv `verification_commands`, and
`max_runtime_seconds` are persisted and independently enforced. Code-changing
work also records the base SHA, commit SHA, remote SHA, and exact PR identity;
PR delivery fails closed when GitHub authentication or exact-one-PR checks are
not healthy. Use `start`/`stop`, `pause`/`resume`, `drain`, and `restart` only
as durable operator controls; process supervision remains with launchd.

The supplied `FakeExecutor` is used by automated tests and the deterministic
acceptance runner:

```bash
python3 scripts/night_shift_fake_acceptance.py
```

The real adapter uses the locally documented `codex exec` stdin-prompt form
with `--ephemeral`, `-C`, bounded timeout, captured stdout/stderr, process
identity, and post-run scope verification. It has no fake-success fallback.
The optional `launchd/com.sportsbrain.night-shift-worker.plist.template` is a
user-level template only; it is not installed or enabled automatically and
does not modify existing production LaunchAgents.

Unattended local Night Shift requires the Mac to remain powered on, awake, and
connected. An operator may run `caffeinate -dimsu` in a separate terminal for a
bounded session; no global power setting is changed.

## Verification

From the repository root:

```bash
pytest -q tests/nightshift
ruff check src/nightshift tests/nightshift scripts/night_shift_dispatcher.py
```

The tests cover each explicitly registered Builder 1–4 template and worker
path, along with Builder 5 recursion prevention, approval, leases, retry and
dead-letter behavior, dependency blocking, idempotency, pause, and audit-chain
integrity. They also cover expected versus unexpected runtime dirtiness,
control-repository isolation, explicit roadmap selection, bounded debug
retests, blocker parking/re-eligibility, merge backpressure, and intentional
idle.
