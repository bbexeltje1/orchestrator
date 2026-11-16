"""THX-02 engine tests against the verified /v1/systemone contract."""

from __future__ import annotations

from orchestrator.config import ThxConfig
from orchestrator.engines.thx02 import CRITERIA, Thx02Engine
from orchestrator.models import Decision, ServiceHealth


def make_engine(ollama_stub, **overrides):
    config = ThxConfig(
        enabled=True,
        base_url=ollama_stub.base_url,
        model="HAL-X/thx-02:q4_k_m",
        min_confidence=0.55,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return Thx02Engine(config)


def test_health_pass(ollama_stub):
    engine = make_engine(ollama_stub)
    result = engine.health()
    assert result.health is ServiceHealth.PASS
    assert "HAL-X" in result.detail


def test_health_blocked_when_model_missing(ollama_stub):
    engine = make_engine(ollama_stub, model="not/installed:latest")
    result = engine.health()
    assert result.health is ServiceHealth.BLOCKED
    assert "not installed" in result.detail


def test_health_blocked_when_ollama_down(base_dir):
    config = ThxConfig(enabled=True, base_url="http://127.0.0.1:1", model="x")
    engine = Thx02Engine(config)
    result = engine.health()
    assert result.health is ServiceHealth.BLOCKED


def test_classify_dispatch_with_probabilities(ollama_stub):
    engine = make_engine(ollama_stub)
    result = engine.classify({"current_status": "READY", "objective": "add dash"})
    assert result.health is ServiceHealth.PASS
    assert result.decision is not None
    assert result.decision.decision is Decision.DISPATCH
    assert set(result.decision.probabilities) == set(CRITERIA)
    assert abs(sum(result.decision.probabilities.values()) - 1.0) < 1e-6
    assert result.decision.confidence == 0.91


def test_unknown_decision_escalates(ollama_stub):
    ollama_stub.decision = "DEPLOY_NOW"
    engine = make_engine(ollama_stub)
    result = engine.classify({"current_status": "READY"})
    assert result.decision is None
    assert result.health is ServiceHealth.UNVERIFIED
    assert "unknown decision" in result.detail


def test_low_confidence_escalates(ollama_stub):
    ollama_stub.confidence = 0.2
    engine = make_engine(ollama_stub, min_confidence=0.55)
    result = engine.classify({"current_status": "READY"})
    assert result.decision is None
    assert "below conservative threshold" in result.detail


def test_missing_probabilities_escalate(ollama_stub):
    ollama_stub.probabilities = {}
    engine = make_engine(ollama_stub)
    result = engine.classify({"current_status": "READY"})
    assert result.decision is None
    assert "no probability" in result.detail


def test_chat_fallback_degraded(ollama_stub):
    ollama_stub.systemone_status = 404
    engine = make_engine(ollama_stub, allow_fallback_chat=True)
    result = engine.classify({"current_status": "READY"})
    assert result.decision is not None
    assert result.decision.decision is Decision.DISPATCH
    assert result.degraded is True
    assert result.health is ServiceHealth.UNVERIFIED


def test_chat_fallback_disabled(ollama_stub):
    ollama_stub.systemone_status = 404
    engine = make_engine(ollama_stub, allow_fallback_chat=False)
    result = engine.classify({"current_status": "READY"})
    assert result.decision is None
    assert result.health is ServiceHealth.BLOCKED


def test_decision_endpoint_detection(ollama_stub):
    engine = make_engine(ollama_stub)
    assert engine.decision_endpoint_supported() is True
    ollama_stub.systemone_status = 404
    assert engine.decision_endpoint_supported() is False


def test_all_criteria_are_in_the_closed_set():
    assert set(CRITERIA) == {d.value for d in Decision}
