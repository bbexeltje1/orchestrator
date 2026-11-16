"""Qwen engine tests: discovery, parsing, routing validation."""

from __future__ import annotations

from orchestrator.config import OmniRouteConfig, QwenConfig
from orchestrator.engines.qwen import QwenEngine
from orchestrator.models import ServiceHealth


def make_engine(qwen_stub, *, model="auto", omniroute=None):
    config = QwenConfig(enabled=True, base_url=qwen_stub.base_url, model=model)
    return QwenEngine(config, omniroute)


def test_discover_model(qwen_stub):
    engine = make_engine(qwen_stub)
    assert engine.discover_model() == "Qwen/Qwen3-8B"


def test_health_pass(qwen_stub):
    engine = make_engine(qwen_stub)
    health, detail, discovered = engine.health()
    assert health is ServiceHealth.PASS
    assert discovered["model"] == "Qwen/Qwen3-8B"


def test_health_blocked_when_down():
    engine = QwenEngine(QwenConfig(enabled=True, base_url="http://127.0.0.1:1"))
    health, detail, _ = engine.health()
    assert health is ServiceHealth.BLOCKED


def test_plan_goal_parses_tasks(qwen_stub):
    engine = make_engine(qwen_stub)
    tasks = engine.plan_goal("Add a combo system")
    assert len(tasks) == 1
    assert tasks[0]["objective"] == "Implement feature X"
    assert tasks[0]["acceptance_criteria"] == ["tests pass"]


def test_plan_goal_malformed_returns_empty(qwen_stub):
    qwen_stub.reply = "I cannot help with that."
    engine = make_engine(qwen_stub)
    assert engine.plan_goal("x") == []


def test_complete_uses_discovered_model(qwen_stub):
    engine = make_engine(qwen_stub)
    result = engine.complete([{"role": "user", "content": "hi"}])
    assert result.model == "Qwen/Qwen3-8B"
    assert result.routed_via == "direct"


def test_omniroute_not_used_when_disabled(qwen_stub):
    omni = OmniRouteConfig(enabled=False, base_url="http://127.0.0.1:20128")
    engine = make_engine(qwen_stub, omniroute=omni)
    usable, detail, _ = engine.validate_omniroute()
    assert usable is False
    assert "disabled" in detail


def test_omniroute_unreachable(qwen_stub):
    omni = OmniRouteConfig(enabled=True, base_url="http://127.0.0.1:1")
    engine = make_engine(qwen_stub, omniroute=omni)
    usable, detail, _ = engine.validate_omniroute()
    assert usable is False


def test_parse_tasks_ignores_invalid_entries(qwen_stub):
    qwen_stub.reply = '[{"nope": 1}, {"objective": "real"}, "junk"]'
    engine = make_engine(qwen_stub)
    tasks = engine.plan_goal("x")
    assert len(tasks) == 1
    assert tasks[0]["objective"] == "real"
