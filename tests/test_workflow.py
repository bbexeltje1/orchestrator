"""Workflow-level tests: planning, THX consultation, safe dry-run execution."""

from __future__ import annotations

from orchestrator.config import ThxConfig
from orchestrator.engines.qwen import QwenEngine
from orchestrator.engines.thx02 import Thx02Engine
from orchestrator.models import Decision, TaskStatus
from orchestrator.policy import Policy
from orchestrator.storage.database import Database
from orchestrator.workflow import Workflow


def build(config, qwen_stub, ollama_stub):
    config.qwen.base_url = qwen_stub.base_url
    config.thx.base_url = ollama_stub.base_url
    db = Database(config.storage.database)
    qwen = QwenEngine(config.qwen, config.omniroute)
    thx = Thx02Engine(config.thx)
    workflow = Workflow(db, config, qwen=qwen, thx=thx, policy=Policy(dry_run=True))
    return workflow, db


def test_submit_goal_creates_tasks(config, qwen_stub, ollama_stub):
    workflow, db = build(config, qwen_stub, ollama_stub)
    result = workflow.submit_goal("Add a combo counter")
    assert not result.blocked_reason
    assert len(result.created_tasks) == 1
    task = db.get_task(result.created_tasks[0])
    assert task.status == TaskStatus.READY.value
    assert task.objective == "Implement feature X"
    db.close()


def test_submit_goal_dedupes(config, qwen_stub, ollama_stub):
    workflow, db = build(config, qwen_stub, ollama_stub)
    first = workflow.submit_goal("Same goal")
    second = workflow.submit_goal("Same goal")
    assert first.created_tasks == second.created_tasks
    assert len(db.list_tasks()) == 1
    db.close()


def test_submit_goal_blocked_without_qwen(config):
    db = Database(config.storage.database)
    workflow = Workflow(db, config, qwen=None, policy=Policy(dry_run=True))
    result = workflow.submit_goal("anything")
    assert result.blocked_reason
    db.close()


def test_consult_thx_returns_actionable_decision(config, qwen_stub, ollama_stub):
    workflow, db = build(config, qwen_stub, ollama_stub)
    decision = workflow.consult_thx({"current_status": "READY"})
    assert decision is Decision.DISPATCH
    db.close()


def test_consult_thx_escalates_unknown(config, qwen_stub, ollama_stub):
    ollama_stub.decision = "BREAK_GLASS"
    workflow, db = build(config, qwen_stub, ollama_stub)
    assert workflow.consult_thx({"current_status": "READY"}) is None
    db.close()


def test_milestone_dry_run_dispatches_nothing(config, qwen_stub, ollama_stub, stub_mcp):
    config.arena.endpoint = stub_mcp.mcp_url
    config.arena.transport = "streamable_http"
    workflow, db = build(config, qwen_stub, ollama_stub)
    from orchestrator.agents.arena import ArenaAdapter

    adapter = ArenaAdapter(config.arena)
    adapter.connect()
    workflow.arena = adapter
    workflow.tm.arena = adapter
    result = workflow.run_milestone("Add a combo counter")
    assert result.created_tasks
    assert stub_mcp.state.calls == []
    db.close()


def test_feature_branch_is_isolated(config):
    wf = object.__new__(Workflow)
    wf.config = config
    branch = wf._feature_branch()
    assert branch.startswith("driftline/orchestrator-")
    assert branch != "main"
