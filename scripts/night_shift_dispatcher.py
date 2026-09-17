"""Operator CLI for the governed Builder 5 Night Shift dispatcher.

The CLI manages queue state only. Worker code is injected through the Python
API so arbitrary shell commands cannot be smuggled into a queue operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift import (
    ExecutionResult,
    NightShiftDispatcher,
    TaskState,
)
from src.nightshift.doctor import run_doctor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SportsBrain Builder 5 Night Shift queue"
    )
    parser.add_argument("--state", type=Path, help="external SQLite state path")
    parser.add_argument(
        "--config-dir", type=Path, default=REPO_ROOT / "config" / "night_shift"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("builders", help="show explicitly registered worker Builders")
    sub.add_parser("templates", help="show explicitly registered task templates")
    sub.add_parser("status", help="show queue health and state counts")
    sub.add_parser("roadmap", help="show the explicit autonomous roadmap")
    sub.add_parser("blocked", help="show parked blocked tasks")
    sub.add_parser("pr-ready", help="show tasks awaiting CEO review")
    sub.add_parser("workers", help="show the governed worker registry")
    sub.add_parser(
        "doctor", help="check local dependencies and safety boundaries without mutation"
    )
    enqueue = sub.add_parser("enqueue-template", help="enqueue a reviewed template")
    enqueue.add_argument("template_id")
    enqueue.add_argument("--branch", required=True)
    enqueue.add_argument("--payload", required=True, help="JSON object")
    enqueue.add_argument("--requested-by", default="operator")
    enqueue.add_argument("--idempotency-key")
    enqueue.add_argument("--priority", type=int)
    enqueue.add_argument("--max-attempts", type=int)
    enqueue.add_argument("--dependency", action="append", default=[])
    enqueue.add_argument("--allowed-path", action="append", default=[])
    enqueue.add_argument("--prohibited-path", action="append", default=[])
    enqueue.add_argument("--resource-lock", action="append", default=[])
    for name in ("approve", "reject", "cancel"):
        action = sub.add_parser(name)
        action.add_argument("task_id")
        action.add_argument("--actor", required=True)
        action.add_argument("--reason", default="")
    review = sub.add_parser(
        "ceo-review", help="move independently verified work to CEO_REVIEW"
    )
    review.add_argument("task_id")
    review.add_argument("--actor", required=True)
    unblock = sub.add_parser(
        "unblock", help="release dependency-blocked work after prerequisites succeed"
    )
    unblock.add_argument("task_id")
    unblock.add_argument("--actor", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("builder_id")
    claim.add_argument("--worker-instance")
    complete = sub.add_parser("complete")
    complete.add_argument("task_id")
    complete.add_argument("--worker-instance", required=True)
    complete.add_argument("--lease-generation", type=int, required=True)
    complete.add_argument("--success", action="store_true")
    complete.add_argument("--summary", default="")
    complete.add_argument("--data", default="{}", help="JSON object")
    complete.add_argument("--non-retryable", action="store_true")
    sub.add_parser(
        "recover", help="requeue expired leases or dead-letter exhausted work"
    )
    for name in ("pause", "resume"):
        action = sub.add_parser(name)
        action.add_argument("--actor", required=True)
    for name in ("start", "stop", "drain", "restart"):
        action = sub.add_parser(name)
        action.add_argument("--actor", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--task-id")
    audit.add_argument("--limit", type=int, default=100)
    return parser


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("expected valid JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _dispatcher(args: argparse.Namespace) -> NightShiftDispatcher:
    return NightShiftDispatcher.from_config(
        config_dir=args.config_dir, state_path=args.state
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        runtime = (
            args.state.expanduser().parent
            if args.state
            else Path.home()
            / "Library"
            / "Application Support"
            / "SportsBrain"
            / "runtime-state"
        )
        report = run_doctor(
            repo_root=REPO_ROOT,
            runtime_dir=runtime,
            state_path=args.state,
            config_dir=args.config_dir,
        )
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "ok" else 1
    dispatcher = _dispatcher(args)
    if args.command == "builders":
        print(
            json.dumps(
                [builder.as_dict() for builder in dispatcher.registry.builders],
                indent=2,
            )
        )
    elif args.command == "templates":
        print(
            json.dumps(
                [template.as_dict() for template in dispatcher.templates.templates],
                indent=2,
            )
        )
    elif args.command == "status":
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command == "roadmap":
        print(json.dumps(dispatcher.store.roadmap_records(), indent=2))
    elif args.command == "blocked":
        print(
            json.dumps(
                [
                    record.as_dict()
                    for record in dispatcher.store.list_tasks(state=TaskState.BLOCKED)
                ],
                indent=2,
            )
        )
    elif args.command == "pr-ready":
        print(
            json.dumps(
                [
                    record.as_dict()
                    for record in dispatcher.store.list_tasks(state=TaskState.PR_READY)
                ],
                indent=2,
            )
        )
    elif args.command == "workers":
        print(
            json.dumps(
                [item.as_dict() for item in dispatcher.registry.builders], indent=2
            )
        )
    elif args.command == "enqueue-template":
        record = dispatcher.submit_template(
            args.template_id,
            branch=args.branch,
            payload=_json_object(args.payload),
            requested_by=args.requested_by,
            idempotency_key=args.idempotency_key,
            priority=args.priority,
            max_attempts=args.max_attempts,
            dependency_ids=tuple(args.dependency),
            allowed_paths=tuple(args.allowed_path),
            prohibited_paths=tuple(args.prohibited_path),
            resource_locks=tuple(args.resource_lock),
        )
        print(json.dumps(record.as_dict(), indent=2))
    elif args.command == "approve":
        print(
            json.dumps(
                dispatcher.approve(
                    args.task_id, approver=args.actor, reason=args.reason
                ).as_dict(),
                indent=2,
            )
        )
    elif args.command == "reject":
        print(
            json.dumps(
                dispatcher.reject(
                    args.task_id, approver=args.actor, reason=args.reason
                ).as_dict(),
                indent=2,
            )
        )
    elif args.command == "cancel":
        print(
            json.dumps(
                dispatcher.cancel(
                    args.task_id, actor=args.actor, reason=args.reason
                ).as_dict(),
                indent=2,
            )
        )
    elif args.command == "ceo-review":
        print(
            json.dumps(
                dispatcher.mark_ceo_review(args.task_id, actor=args.actor).as_dict(),
                indent=2,
            )
        )
    elif args.command == "unblock":
        print(
            json.dumps(
                dispatcher.unblock(args.task_id, actor=args.actor).as_dict(), indent=2
            )
        )
    elif args.command == "claim":
        record = dispatcher.claim_next(
            args.builder_id, worker_instance_id=args.worker_instance
        )
        print(json.dumps(record.as_dict() if record else {"claimed": False}, indent=2))
    elif args.command == "complete":
        print(
            json.dumps(
                dispatcher.complete(
                    args.task_id,
                    worker_instance_id=args.worker_instance,
                    lease_generation=args.lease_generation,
                    execution=ExecutionResult(
                        success=args.success,
                        summary=args.summary,
                        data=_json_object(args.data),
                        retryable=not args.non_retryable,
                    ),
                ).as_dict(),
                indent=2,
            )
        )
    elif args.command == "recover":
        print(
            json.dumps(
                [record.as_dict() for record in dispatcher.recover_expired()], indent=2
            )
        )
    elif args.command == "pause":
        dispatcher.pause(actor=args.actor)
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command in {"resume", "start"}:
        dispatcher.resume(actor=args.actor)
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command == "stop":
        dispatcher.pause(actor=args.actor)
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command == "drain":
        dispatcher.drain(actor=args.actor)
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command == "restart":
        dispatcher.restart(actor=args.actor)
        print(json.dumps(dispatcher.status(), indent=2))
    elif args.command == "audit":
        print(
            json.dumps(
                [
                    event.as_dict()
                    for event in dispatcher.store.audit_events(
                        task_id=args.task_id, limit=args.limit
                    )
                ],
                indent=2,
            )
        )
    else:  # pragma: no cover - argparse enforces command choices
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
