"""Arena adapter tests: real MCP protocol, discovery, honest capability."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestrator.agents.arena import ArenaAdapter
from orchestrator.config import ArenaConfig
from orchestrator.mcp.client import connect_streamable_http

ROOT = Path(__file__).resolve().parents[1]


def make_config(stub, **overrides):
    config = ArenaConfig(
        enabled=True,
        transport="streamable_http",
        endpoint=stub.mcp_url,
        request_timeout=10.0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class FakeTask:
    objective = "Implement a dash"
    branch = "driftline/test"
    project = "DRIFTLINE"
    acceptance_criteria = ["tests pass"]


def test_streamable_http_discovers_real_tools(stub_mcp):
    adapter = ArenaAdapter(make_config(stub_mcp))
    capability = adapter.connect()
    assert capability.level == "TOOL_FOUND"
    assert capability.task_tool.name == "arena_task"
    assert capability.status_tool.name == "arena_task_status"
    assert capability.result_tool.name == "arena_task_result"
    assert "objective" in capability.task_tool.properties
    assert capability.task_tool.required == ["objective"]
    assert capability.discovered["task_tool_schema"]["type"] == "object"
    adapter.close()


def test_dispatch_maps_arguments_and_returns_ref(stub_mcp):
    adapter = ArenaAdapter(make_config(stub_mcp))
    adapter.connect()
    result = adapter.dispatch(FakeTask())
    assert result["accepted"] is True
    assert result["ref"] == "arena-stub-1"
    name, args = stub_mcp.state.calls[0]
    assert name == "arena_task"
    assert args["objective"] == "Implement a dash"
    assert args["branch"] == "driftline/test"
    assert adapter.capability.level == "DISPATCH_ACCEPTED"


def test_dispatch_schema_mismatch_reported(stub_mcp):
    adapter = ArenaAdapter(make_config(stub_mcp))
    adapter.connect()
    # Add a required property the orchestrator cannot supply.
    adapter.capability.task_tool.input_schema = {
        "type": "object",
        "properties": {"objective": {"type": "string"}, "repo_url": {"type": "string"}},
        "required": ["objective", "repo_url"],
    }
    result = adapter.dispatch(FakeTask())
    assert result["accepted"] is False
    assert result["error"] == "schema mismatch"
    assert any("repo_url" in m for m in result["missing"])
    assert stub_mcp.state.calls == []


def test_dispatch_rejected_by_server(stub_mcp):
    stub_mcp.state.fail_dispatch = True
    adapter = ArenaAdapter(make_config(stub_mcp))
    adapter.connect()
    result = adapter.dispatch(FakeTask())
    assert result["accepted"] is False


def test_connected_without_task_tool_is_honest(stub_mcp):
    stub_mcp.state.omit_task_tool = True
    adapter = ArenaAdapter(make_config(stub_mcp))
    capability = adapter.connect()
    assert capability.level == "CONNECTED"
    assert capability.can_dispatch is False
    assert any("UNVERIFIED" in d for d in capability.diagnostics)


def test_not_connected_gives_precise_diagnostic():
    config = ArenaConfig(
        enabled=True,
        transport="streamable_http",
        endpoint="http://127.0.0.1:1/mcp",
        request_timeout=2.0,
    )
    adapter = ArenaAdapter(config)
    capability = adapter.connect()
    assert capability.level == "NOT_CONNECTED"
    assert capability.task_tool is None
    assert any("ARENA_NOT_CONNECTED" in d for d in capability.diagnostics)


def test_disabled_adapter():
    config = ArenaConfig(enabled=False, transport="streamable_http", endpoint="http://x/mcp")
    adapter = ArenaAdapter(config)
    capability = adapter.connect()
    assert capability.level == "NOT_CONNECTED"
    assert any("disabled" in d for d in capability.diagnostics)


def test_stdio_transport(stub_mcp_state):
    command = [sys.executable, str(ROOT / "tests" / "stubs" / "stdio_mcp_server.py")]
    config = ArenaConfig(
        enabled=True,
        transport="stdio",
        stdio_command=" ".join(command),
        request_timeout=10.0,
    )
    adapter = ArenaAdapter(config)
    capability = adapter.connect()
    assert capability.level == "TOOL_FOUND"
    assert capability.task_tool.name == "arena_task"
    result = adapter.dispatch(FakeTask())
    assert result["accepted"] is True
    assert result["ref"] == "stdio-stub-1"
    adapter.close()


def test_rest_bridge_probe(stub_mcp):
    adapter = ArenaAdapter(make_config(stub_mcp))
    report = adapter.probe_rest_bridge()
    assert report["health"]["status_code"] == 200
    assert report["tools"]["status_code"] == 200


def test_capability_levels_are_ordered():
    from orchestrator.agents.arena import _CAPABILITY_ORDER

    assert _CAPABILITY_ORDER[0] == "NOT_CONNECTED"
    assert _CAPABILITY_ORDER[-1] == "COMPLETED"


def test_get_status_and_result(stub_mcp):
    adapter = ArenaAdapter(make_config(stub_mcp))
    adapter.connect()
    status = adapter.get_status("abc")
    assert status["available"] is True
    assert status["payload"]["status"] == "RUNNING"
    stub_mcp.state.task_status = "DONE"
    result = adapter.get_result("abc")
    assert result["payload"]["implemented"] is True
    assert adapter.capability.level == "COMPLETED"
