"""Task manager tests using a real local MCP stub."""

from __future__ import annotations

import time

from orchestrator.agents.arena import ArenaAdapter
from orchestrator.models import TaskStatus
from orchestrator.policy import Policy
from orchestrator.task_manager import TaskManager, TaskManagerError


def make_manager(db, config, stub, *, dry_run=False, timeout=30.0):
    config.arena.endpoint = stub.mcp_url
    config.arena.transport = "streamable_http"
    config.arena.request_timeout = timeout
    config.workflow.dry_run = dry_run
    adapter = ArenaAdapter(config.arena)
    capability = adapter.connect()
    assert capability.can_dispatch, capability.diagnostics
    manager = TaskManager(
        db, config, arena=adapter, policy=Policy(dry_run=dry_run, autonomy="hybrid")
    )
    return manager, adapter


def test_create_and_dedupe(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp)
    task, created = manager.create_task(
        project="DRIFTLINE",
        branch="driftline/test",
        objective="Add a health bar",
        acceptance_criteria=["tests pass"],
    )
    assert created
    again, created2 = manager.create_task(
        project="DRIFTLINE",
        branch="driftline/test",
        objective="Add a health bar",
        acceptance_criteria=["tests pass"],
    )
    assert not created2
    assert again.id == task.id
    assert db.get_task_by_idempotency(task.idempotency_key).id == task.id


def test_create_rejects_unsafe_branch(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp)
    try:
        manager.create_task(project="DRIFTLINE", branch="a;rm -rf", objective="x")
        raised = False
    except TaskManagerError:
        raised = True
    assert raised


def test_dispatch_success_and_poll(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="Implement dash"
    )
    outcome = manager.dispatch(task.id)
    assert outcome.dispatched
    assert outcome.status == TaskStatus.RUNNING.value
    assert outcome.arena_ref == "arena-stub-1"
    assert stub_mcp.state.calls[0][0] == "arena_task"

    poll = manager.poll(task.id)
    assert poll.status == TaskStatus.RUNNING.value  # stub still RUNNING

    stub_mcp.state.task_status = "DONE"
    poll = manager.poll(task.id)
    assert poll.status == TaskStatus.RESULT_RECEIVED.value

    result = manager.collect_result(task.id)
    assert result["available"]
    assert result["payload"]["implemented"] is True


def test_dispatch_without_arena_refuses(db, config):
    config.workflow.dry_run = False
    manager = TaskManager(db, config, arena=None, policy=Policy(dry_run=False))
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="x"
    )
    outcome = manager.dispatch(task.id)
    assert not outcome.dispatched
    assert "ARENA_NOT_CONNECTED" in outcome.detail
    assert task.status == TaskStatus.READY.value


def test_dry_run_never_calls_arena(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp, dry_run=True)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="dry run task"
    )
    outcome = manager.dispatch(task.id)
    assert not outcome.dispatched
    assert "dry_run" in outcome.detail
    assert stub_mcp.state.calls == []


def test_dispatch_timeout_marks_unknown_and_never_resends(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp, timeout=0.2)

    def slow_dispatch(task):
        time.sleep(1.0)
        return {"accepted": True}

    adapter.dispatch = slow_dispatch  # type: ignore[assignment]
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="slow task"
    )
    outcome = manager.dispatch(task.id)
    assert outcome.status == TaskStatus.DISPATCH_UNKNOWN.value
    assert "reconcile" in outcome.detail

    # A second dispatch attempt must be refused, not resent.
    second = manager.dispatch(task.id)
    assert not second.dispatched
    assert "not permitted" in second.detail
    assert stub_mcp.state.calls == []


def test_schema_mismatch_blocks(db, config, stub_mcp):
    manager, adapter = make_manager(db, config, stub_mcp)
    original = adapter.build_dispatch_arguments

    def broken(task):
        return {}, ["required property 'repository_url' is not supplied"]

    adapter.build_dispatch_arguments = broken  # type: ignore[assignment]
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="schema mismatch"
    )
    outcome = manager.dispatch(task.id)
    assert outcome.status == TaskStatus.BLOCKED.value
    assert "schema mismatch" in outcome.detail
    adapter.build_dispatch_arguments = original  # type: ignore[assignment]


def test_retry_limit_enforced(db, config, stub_mcp):
    config.workflow.max_retries = 1
    manager, adapter = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="retry me"
    )
    # Simulate a failure with a retry count already at the limit.
    db.update_task(task.id, status=TaskStatus.FAILED.value, retry_count=1)
    outcome = manager.retry(task.id)
    assert outcome.status == TaskStatus.BLOCKED.value
    assert "retry limit" in outcome.detail


def test_events_are_audited(db, config, stub_mcp):
    manager, _ = make_manager(db, config, stub_mcp)
    task, _ = manager.create_task(
        project="DRIFTLINE", branch="driftline/test", objective="audit me"
    )
    manager.dispatch(task.id)
    events = db.events(task.id)
    statuses = [e["to_status"] for e in events]
    assert TaskStatus.DISPATCH_PENDING.value in statuses
    assert TaskStatus.RUNNING.value in statuses
