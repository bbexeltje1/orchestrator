"""Dispatch safety: duplicates, timeouts, approval gates, idempotency."""

from __future__ import annotations

import time

from orchestrator.agents.arena import ArenaAdapter
from orchestrator.models import ApprovalStatus, TaskStatus
from orchestrator.policy import Effect, Operation, Policy, PolicyDecision
from orchestrator.task_manager import TaskManager


def make_manager(db, config, stub, *, dry_run=False, timeout=30.0, policy=None):
    config.arena.endpoint = stub.mcp_url
    config.arena.transport = "streamable_http"
    config.arena.request_timeout = timeout
    config.workflow.dry_run = dry_run
    adapter = ArenaAdapter(config.arena)
    adapter.connect()
    manager = TaskManager(db, config, arena=adapter, policy=policy or Policy(dry_run=dry_run))
    return manager, adapter


def test_duplicate_dispatch_protected_by_idempotency(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="b", objective="only once", acceptance_criteria=["x"]
    )
    first = manager.dispatch(task.id)
    assert first.dispatched
    # Even resetting to READY, the idempotency log records the dispatch.
    db.update_task(task.id, status=TaskStatus.READY.value)
    stored = db.idempotency_response(task.idempotency_key)
    assert stored == "arena-stub-1"


def test_status_timeout_does_not_create_task(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="b", objective="poll timeout"
    )
    manager.dispatch(task.id)

    def boom(ref):
        raise RuntimeError("status endpoint timed out")

    adapter.get_status = boom  # type: ignore[assignment]
    before = len(db.list_tasks())
    outcome = manager.poll(task.id)
    assert not outcome.dispatched
    assert "will retry" in outcome.detail
    assert db.get_task(task.id).status == TaskStatus.RUNNING.value
    assert len(db.list_tasks()) == before


def test_approval_gate_blocks_dispatch(db, config, stub_mcp):
    class GatingPolicy(Policy):
        def check(self, operation: Operation) -> PolicyDecision:
            if operation is Operation.DISPATCH_TASK:
                return PolicyDecision(
                    Effect.REQUIRE_APPROVAL, operation, "test: approval required"
                )
            return super().check(operation)

    manager, _ = make_manager(db, config, stub_mcp, policy=GatingPolicy(dry_run=False))
    task, _ = manager.create_task(project="DRIFTLINE", branch="b", objective="gated")
    outcome = manager.dispatch(task.id)
    assert not outcome.dispatched
    refreshed = db.get_task(task.id)
    assert refreshed.status == TaskStatus.AWAITING_APPROVAL.value
    pending = db.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["operation"] == Operation.DISPATCH_TASK.value

    # Resolving the approval does not by itself dispatch; a human must retry.
    status = db.resolve_approval(pending[0]["id"], granted=True)
    assert status == ApprovalStatus.GRANTED.value


def test_dispatch_unknown_reconciles_without_resend(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp, timeout=0.2)

    def slow(task):
        time.sleep(1.0)
        return {"accepted": True, "ref": "arena-late"}

    adapter.dispatch = slow  # type: ignore[assignment]
    task, _ = manager.create_task(project="DRIFTLINE", branch="b", objective="late")
    manager.dispatch(task.id)
    assert db.get_task(task.id).status == TaskStatus.DISPATCH_UNKNOWN.value

    # Reconcile with no stored reference -> BLOCKED, and Arena is not called.
    outcome = manager.reconcile(task.id)
    assert outcome.status == TaskStatus.BLOCKED.value
    assert "reconciliation required" in outcome.detail
    assert stub_mcp.state.calls == []


def test_reconcile_with_reference_marks_running(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(project="DRIFTLINE", branch="b", objective="rec")
    db.update_task(task.id, status=TaskStatus.DISPATCH_UNKNOWN.value, arena_ref="abc")
    stub_mcp.state.task_status = "RUNNING"
    outcome = manager.reconcile(task.id)
    assert outcome.status == TaskStatus.RUNNING.value


def test_reconcile_completed(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(project="DRIFTLINE", branch="b", objective="rec2")
    db.update_task(task.id, status=TaskStatus.DISPATCH_UNKNOWN.value, arena_ref="abc")
    stub_mcp.state.task_status = "DONE"
    outcome = manager.reconcile(task.id)
    assert outcome.status == TaskStatus.RESULT_RECEIVED.value


def test_concurrent_duplicate_creates_are_safe(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(project="DRIFTLINE", branch="b", objective="dedupe")
    # Directly attempt to insert a colliding task row.
    from orchestrator.models import Task

    duplicate = Task(
        id="task-forced",
        idempotency_key=task.idempotency_key,
        project="DRIFTLINE",
        branch="b",
        objective="dedupe",
    )
    raised = False
    try:
        db.create_task(duplicate)
    except Exception:
        raised = True
    assert raised
