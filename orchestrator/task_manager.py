"""Task lifecycle management.

This is where idempotency, duplicate-dispatch protection and the
``DISPATCH_UNKNOWN`` reconciliation path live. The rules are strict:

* A task is dispatched at most once per ``idempotency_key`` unless a human
  explicitly retries.
* If a dispatch times out after the request *might* have reached Arena, the
  task becomes ``DISPATCH_UNKNOWN`` and is reconciled — never blindly resent.
* AI output never changes policy. ``Policy`` is consulted on every dispatch.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .agents.arena import ArenaAdapter
from .config import Config
from .models import (
    Decision,
    ServiceHealth,
    Task,
    TaskStatus,
    make_idempotency_key,
    new_id,
    redact,
    utcnow_iso,
)
from .policy import Effect, Operation, Policy
from .state_machine import StateMachineError, is_dispatchable, require_transition
from .storage.database import Database, DatabaseError


class TaskManagerError(Exception):
    pass


@dataclass
class DispatchOutcome:
    task_id: str
    status: str
    dispatched: bool
    arena_ref: Optional[str] = None
    detail: str = ""
    accepted: Optional[bool] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "dispatched": self.dispatched,
            "arena_ref": self.arena_ref,
            "detail": self.detail,
            "accepted": self.accepted,
        }


class TaskManager:
    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        arena: Optional[ArenaAdapter] = None,
        policy: Optional[Policy] = None,
        logger: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.arena = arena
        self.policy = policy or Policy(
            require_approval_for_merge=config.workflow.require_approval_for_merge,
            require_approval_for_publish=config.workflow.require_approval_for_publish,
            autonomy=config.workflow.autonomy,
            dry_run=config.workflow.dry_run,
        )
        self.log = logger or _NullLogger()

    # -- creation --------------------------------------------------------

    def create_task(
        self,
        *,
        project: str,
        branch: str,
        objective: str,
        acceptance_criteria: Optional[List[str]] = None,
        parent_goal: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[Task, bool]:
        """Create a task. Returns (task, created).

        If a task with the same idempotency key already exists, the existing
        task is returned with ``created=False`` — duplicates are impossible.
        """
        objective = objective.strip()
        if not objective:
            raise TaskManagerError("objective must not be empty")
        ok, reason = self.policy.validate_branch(branch)
        if not ok:
            raise TaskManagerError(f"unsafe branch: {reason}")
        key = make_idempotency_key(project, objective, "|".join(acceptance_criteria or []))
        existing = self.db.get_task_by_idempotency(key)
        if existing is not None:
            self.log.info("duplicate task suppressed (idempotency_key=%s)", key)
            return existing, False
        task = Task(
            id=new_id("task"),
            idempotency_key=key,
            project=project,
            branch=branch,
            objective=objective,
            acceptance_criteria=list(acceptance_criteria or []),
            parent_goal=parent_goal,
            metadata=dict(metadata or {}),
        )
        try:
            self.db.create_task(task)
        except DatabaseError as exc:
            raise TaskManagerError(str(exc)) from exc
        self.log.info("task created id=%s objective=%.60s", task.id, task.objective)
        return task, True

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.db.get_task(task_id)

    def list_tasks(self, status: Optional[str] = None) -> List[Task]:
        return self.db.list_tasks(status=status)

    # -- policy gate -----------------------------------------------------

    def _policy_gate(self, task: Task, operation: Operation) -> tuple[bool, Optional[str]]:
        decision = self.policy.check(operation)
        self.log.info(
            "policy %s -> %s (%s)", operation.value, decision.effect.value, decision.reason
        )
        if decision.effect is Effect.ALLOW:
            return True, None
        if decision.effect is Effect.REQUIRE_APPROVAL:
            approval_id = self.db.request_approval(
                operation.value,
                decision.reason,
                task_id=task.id,
            )
            self.db.update_task(
                task.id,
                status=TaskStatus.AWAITING_APPROVAL.value,
                message=f"approval required: {operation.value}",
            )
            return False, approval_id
        self.db.update_task(
            task.id, status=TaskStatus.BLOCKED.value, message=f"denied: {operation.value}"
        )
        return False, decision.reason

    # -- local coding-agent execution (Claude Code) ------------------------

    def execute_with_claude(
        self,
        task_id: str,
        *,
        executor: Any,
        workdir: str | Path,
        verify_fn: Optional[Callable[[Any], tuple[bool, str]]] = None,
        timeout: Optional[float] = None,
    ) -> DispatchOutcome:
        """Execute a task with the Claude Code CLI in ``workdir``.

        This is an *explicit, human-initiated* local execution channel (the
        ``claude-run`` / ``claude-acceptance`` commands). It is intentionally
        separate from ``dispatch()``: ``workflow.dry_run`` keeps blocking all
        Arena contact exactly as before and is never consulted here - the
        explicit command is the consent. All transitions still go through the
        state machine and the policy gate. A local timeout is deterministic
        (the process is killed), so it becomes FAILED, never DISPATCH_UNKNOWN.
        """
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if not is_dispatchable(task.status):
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail=f"task is {task.status}; execution is not permitted from this state",
            )
        allowed, gate = self._policy_gate(task, Operation.DISPATCH_TASK)
        if not allowed:
            return DispatchOutcome(
                task_id=task.id,
                status=self.db.get_task(task_id).status,
                dispatched=False,
                detail=f"policy gate: {gate}",
            )

        try:
            require_transition(task.status, TaskStatus.DISPATCH_PENDING)
        except StateMachineError as exc:
            return DispatchOutcome(
                task_id=task.id, status=task.status, dispatched=False, detail=str(exc)
            )
        self.db.update_task(
            task.id,
            status=TaskStatus.DISPATCH_PENDING.value,
            dispatch_at=utcnow_iso(),
            message="executing with Claude Code CLI",
        )
        self.db.update_task(
            task.id, status=TaskStatus.RUNNING.value, message="claude_code running"
        )
        try:
            result = executor.run_task(task.objective, workdir, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - executor bugs must not corrupt state
            self.db.update_task(
                task.id,
                status=TaskStatus.FAILED.value,
                error=redact(f"{type(exc).__name__}: {exc}"),
                message="claude_code executor raised",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.FAILED.value,
                dispatched=False,
                detail=f"executor error: {type(exc).__name__}: {redact(exc)}",
            )

        changed = list(result.created_files) + list(result.modified_files)
        detail = (
            f"claude_code exit={result.returncode} created={result.created_files} "
            f"modified={result.modified_files}"
        )
        if not result.ok:
            self.db.update_task(
                task.id,
                status=TaskStatus.FAILED.value,
                error=redact(result.blocked_reason or result.result_text[:300] or "run failed"),
                message=detail,
                changed_files=changed,
                final_result=redact(result.result_text[:4000]),
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.FAILED.value,
                dispatched=False,
                detail=detail,
                raw=result.to_dict(),
            )

        self.db.update_task(
            task.id,
            status=TaskStatus.RESULT_RECEIVED.value,
            message=detail,
            changed_files=changed,
            final_result=redact(result.result_text[:4000]),
        )
        if verify_fn is None:
            self.db.update_task(
                task.id,
                status=TaskStatus.NEEDS_REVIEW.value,
                message="agent run finished; human review required",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.NEEDS_REVIEW.value,
                dispatched=True,
                detail=detail + " (NEEDS_REVIEW)",
                raw=result.to_dict(),
            )

        self.db.update_task(
            task.id, status=TaskStatus.VERIFYING.value, message="running verification"
        )
        try:
            verified, verify_detail = verify_fn(result)
        except Exception as exc:  # noqa: BLE001
            verified, verify_detail = False, f"verification raised {type(exc).__name__}: {redact(exc)}"
        if verified:
            self.db.update_task(
                task.id,
                status=TaskStatus.VERIFIED.value,
                message=f"verified: {verify_detail}",
                verification={"passed": True, "detail": verify_detail},
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.VERIFIED.value,
                dispatched=True,
                detail=f"{detail}; VERIFIED: {verify_detail}",
                raw=result.to_dict(),
            )
        self.db.update_task(
            task.id,
            status=TaskStatus.NEEDS_REVIEW.value,
            message=f"verification failed: {verify_detail}",
            verification={"passed": False, "detail": verify_detail},
        )
        return DispatchOutcome(
            task_id=task.id,
            status=TaskStatus.NEEDS_REVIEW.value,
            dispatched=True,
            detail=f"{detail}; verification failed: {verify_detail}",
            raw=result.to_dict(),
        )

    # -- dispatch --------------------------------------------------------

    def dispatch(self, task_id: str) -> DispatchOutcome:
        """Dispatch exactly once, with timeout reconciliation."""
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if not is_dispatchable(task.status):
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail=f"task is {task.status}; dispatch is not permitted from this state",
            )
        if self.arena is None or not self.arena.capability.connected:
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail="Arena is not connected (ARENA_NOT_CONNECTED); refusing to fake a dispatch",
            )

        allowed, gate = self._policy_gate(task, Operation.DISPATCH_TASK)
        if not allowed:
            return DispatchOutcome(
                task_id=task.id,
                status=self.db.get_task(task_id).status,
                dispatched=False,
                detail=f"policy gate: {gate}",
            )

        if self.config.workflow.dry_run:
            self.log.warning(
                "dry_run is enabled: NOT contacting Arena for task %s", task.id
            )
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail="dry_run: dispatch suppressed; no Arena call was made",
            )

        try:
            require_transition(task.status, TaskStatus.DISPATCH_PENDING)
        except StateMachineError as exc:
            return DispatchOutcome(
                task_id=task.id, status=task.status, dispatched=False, detail=str(exc)
            )
        self.db.update_task(
            task.id,
            status=TaskStatus.DISPATCH_PENDING.value,
            dispatch_at=utcnow_iso(),
            message="dispatching to Arena",
        )

        timeout = float(self.config.arena.request_timeout)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.arena.dispatch, task)
                result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # The request may have reached Arena. Never resend blindly.
            self.db.update_task(
                task.id,
                status=TaskStatus.DISPATCH_UNKNOWN.value,
                error=f"dispatch timed out after {timeout}s; reconciliation required",
                message="dispatch timed out; marked DISPATCH_UNKNOWN",
            )
            self.log.error("task %s dispatch timed out -> DISPATCH_UNKNOWN", task.id)
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.DISPATCH_UNKNOWN.value,
                dispatched=False,
                detail="dispatch timed out after possible submission; reconcile before retrying",
            )
        except Exception as exc:  # noqa: BLE001 - transport/schema errors
            self.db.update_task(
                task.id,
                status=TaskStatus.DISPATCH_UNKNOWN.value,
                error=redact(f"{type(exc).__name__}: {exc}"),
                message="dispatch failed ambiguously; marked DISPATCH_UNKNOWN",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.DISPATCH_UNKNOWN.value,
                dispatched=False,
                detail=f"dispatch error: {type(exc).__name__}: {redact(exc)}",
            )

        if not isinstance(result, dict):
            self.db.update_task(
                task.id, status=TaskStatus.FAILED.value, error="non-dict dispatch result"
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.FAILED.value,
                dispatched=False,
                detail="arena returned an unexpected result shape",
            )

        if result.get("error") == "schema mismatch":
            missing = ", ".join(result.get("missing", []))
            self.db.update_task(
                task.id,
                status=TaskStatus.BLOCKED.value,
                error=f"Arena schema mismatch: {missing}",
                message="blocked: real Arena schema is incompatible",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.BLOCKED.value,
                dispatched=False,
                detail=f"schema mismatch: {missing}",
                raw=result,
            )

        accepted = bool(result.get("accepted"))
        arena_ref = result.get("ref")
        self.db.update_task(
            task.id,
            arena_ref=arena_ref,
            message=f"Arena dispatch accepted={accepted}",
        )
        if accepted:
            self.db.update_task(
                task.id, status=TaskStatus.RUNNING.value, message="Arena accepted the task"
            )
            self.db.record_idempotency(
                task.idempotency_key, "arena_dispatch", str(result.get("ref") or "")
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.RUNNING.value,
                dispatched=True,
                arena_ref=arena_ref,
                detail="Arena accepted the task",
                accepted=True,
                raw=result,
            )
        self.db.update_task(
            task.id, status=TaskStatus.FAILED.value, error="Arena did not accept the task"
        )
        return DispatchOutcome(
            task_id=task.id,
            status=TaskStatus.FAILED.value,
            dispatched=False,
            accepted=False,
            detail=result.get("text", "Arena did not accept the task")[:500],
            raw=result,
        )

    # -- reconciliation --------------------------------------------------

    def reconcile(self, task_id: str) -> DispatchOutcome:
        """Resolve a DISPATCH_UNKNOWN task without re-dispatching."""
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if task.status != TaskStatus.DISPATCH_UNKNOWN.value:
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail=f"task is {task.status}, not DISPATCH_UNKNOWN",
            )
        if not task.arena_ref:
            self.db.update_task(
                task.id,
                status=TaskStatus.BLOCKED.value,
                error="dispatch timed out and no Arena reference was returned; "
                "manual reconciliation required",
                message="reconciled: no reference available",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.BLOCKED.value,
                dispatched=False,
                detail="no Arena reference; human reconciliation required",
            )
        try:
            status = self.arena.get_status(task.arena_ref)
        except Exception as exc:  # noqa: BLE001
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail=f"reconciliation query failed: {type(exc).__name__}",
            )
        payload = status.get("payload") or {}
        state = str(payload.get("status") or payload.get("state") or "").upper()
        if state in {"DONE", "COMPLETED", "COMPLETE", "FINISHED", "SUCCESS"}:
            self.db.update_task(
                task.id, status=TaskStatus.RESULT_RECEIVED.value, message="reconciled: complete"
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.RESULT_RECEIVED.value,
                dispatched=True,
                arena_ref=task.arena_ref,
                detail="reconciled: Arena reports completion",
            )
        if state in {"RUNNING", "PENDING", "QUEUED", "IN_PROGRESS"}:
            self.db.update_task(
                task.id, status=TaskStatus.RUNNING.value, message="reconciled: still running"
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.RUNNING.value,
                dispatched=True,
                arena_ref=task.arena_ref,
                detail="reconciled: task is running in Arena",
            )
        self.db.update_task(
            task.id,
            status=TaskStatus.FAILED.value,
            error=f"reconciled: Arena state {state or 'unknown'}",
            message="reconciled: failed/unknown",
        )
        return DispatchOutcome(
            task_id=task.id,
            status=TaskStatus.FAILED.value,
            dispatched=False,
            arena_ref=task.arena_ref,
            detail=f"reconciled: Arena state {state or 'unknown'}",
        )

    # -- polling ---------------------------------------------------------

    def poll(self, task_id: str) -> DispatchOutcome:
        """One non-AI status poll. Routine polling never calls an AI model."""
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if task.status != TaskStatus.RUNNING.value:
            return DispatchOutcome(
                task_id=task.id, status=task.status, dispatched=False, detail="not running"
            )
        if not task.arena_ref:
            self.db.update_task(
                task.id,
                status=TaskStatus.FAILED.value,
                error="running task has no Arena reference",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.FAILED.value,
                dispatched=False,
                detail="no Arena reference",
            )
        try:
            status = self.arena.get_status(task.arena_ref)
        except Exception as exc:  # noqa: BLE001
            # A status timeout is NOT a reason to create a new task.
            return DispatchOutcome(
                task_id=task.id,
                status=task.status,
                dispatched=False,
                detail=f"status poll failed (will retry): {type(exc).__name__}",
            )
        payload = status.get("payload") or {}
        state = str(payload.get("status") or payload.get("state") or "").upper()
        if state in {"DONE", "COMPLETED", "COMPLETE", "FINISHED", "SUCCESS"}:
            self.db.update_task(
                task.id, status=TaskStatus.RESULT_RECEIVED.value, message="Arena completed"
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.RESULT_RECEIVED.value,
                dispatched=True,
                arena_ref=task.arena_ref,
                detail="Arena reports completion",
            )
        if state in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            self.db.update_task(
                task.id,
                status=TaskStatus.FAILED.value,
                error=f"Arena reported {state}",
                message="Arena reported failure",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.FAILED.value,
                dispatched=False,
                arena_ref=task.arena_ref,
                detail=f"Arena reported {state}",
            )
        return DispatchOutcome(
            task_id=task.id,
            status=task.status,
            dispatched=False,
            arena_ref=task.arena_ref,
            detail=f"Arena state: {state or 'unknown'}",
        )

    def collect_result(self, task_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if not task.arena_ref:
            return {"available": False, "reason": "no Arena reference"}
        try:
            return self.arena.get_result(task.arena_ref)
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    # -- retry -----------------------------------------------------------

    def retry(self, task_id: str) -> DispatchOutcome:
        """Explicit human-approved retry. Resets retry_count-gated state."""
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskManagerError(f"unknown task {task_id}")
        if task.retry_count >= self.config.workflow.max_retries:
            self.db.update_task(
                task.id,
                status=TaskStatus.BLOCKED.value,
                error=f"retry limit reached ({task.retry_count})",
            )
            return DispatchOutcome(
                task_id=task.id,
                status=TaskStatus.BLOCKED.value,
                dispatched=False,
                detail="retry limit reached",
            )
        self.db.update_task(
            task.id,
            status=TaskStatus.READY.value,
            error=None,
            retry_count=task.retry_count + 1,
            message="explicit retry",
            clear_error=True,
        )
        return self.dispatch(task_id)


class _NullLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
