"""Arena implementation-engine adapter.

This module never assumes an endpoint, a tool name, an argument or a task id.
It connects to whatever MCP server the configuration points at, discovers the
real ``tools/list`` output, and then selects a dispatch/status/result tool from
that *actual* schema.

Capability levels (reported separately, never conflated):

``NOT_CONNECTED``
    No MCP transport could be established.
``CONNECTED``
    MCP ``initialize`` succeeded.
``TOOL_FOUND``
    A dispatch tool was identified from the real schema.
``DISPATCH_ACCEPTED``
    A dispatch call returned a result without an MCP error.
``COMPLETED``
    A terminal result was retrieved from a status/result tool.

If no task tool can be found, the adapter reports ``ARENA_NOT_CONNECTED`` with
a precise diagnostic. It never fabricates a successful dispatch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from ..config import ArenaConfig
from ..mcp.client import (
    McpClient,
    McpError,
    McpTool,
    connect_stdio,
    connect_streamable_http,
)

# Candidate tool names, in priority order. The first one present in the real
# ``tools/list`` output wins. None of these is assumed to exist.
TASK_TOOL_CANDIDATES: Tuple[str, ...] = (
    "arena_task",
    "arena_dispatch",
    "arena_submit_task",
    "arena_run",
    "arena_create_task",
    "dispatch_task",
    "submit_task",
    "create_task",
    "run_task",
    "start_task",
    "task",
)
STATUS_TOOL_CANDIDATES: Tuple[str, ...] = (
    "arena_task_status",
    "arena_status",
    "task_status",
    "get_task_status",
    "arena_state",
    "status",
)
RESULT_TOOL_CANDIDATES: Tuple[str, ...] = (
    "arena_task_result",
    "arena_result",
    "task_result",
    "get_task_result",
    "arena_output",
    "result",
)

# Schema properties that suggest a tool dispatches work.
TASK_SCHEMA_HINTS = (
    "objective",
    "prompt",
    "instruction",
    "description",
    "task",
    "message",
    "goal",
    "repo",
    "repository",
    "branch",
)

_CAPABILITY_ORDER = [
    "NOT_CONNECTED",
    "CONNECTED",
    "TOOL_FOUND",
    "DISPATCH_ACCEPTED",
    "COMPLETED",
]


@dataclass
class ArenaCapability:
    level: str = "NOT_CONNECTED"
    transport: str = ""
    endpoint: str = ""
    tools: List[McpTool] = field(default_factory=list)
    task_tool: Optional[McpTool] = None
    status_tool: Optional[McpTool] = None
    result_tool: Optional[McpTool] = None
    diagnostics: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    discovered: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "tool_names": [t.name for t in self.tools],
            "task_tool": self.task_tool.name if self.task_tool else None,
            "status_tool": self.status_tool.name if self.status_tool else None,
            "result_tool": self.result_tool.name if self.result_tool else None,
            "diagnostics": self.diagnostics,
            "errors": self.errors,
            "discovered": self.discovered,
        }

    @property
    def connected(self) -> bool:
        return self.level != "NOT_CONNECTED"

    @property
    def can_dispatch(self) -> bool:
        return self.task_tool is not None


def extract_text(content: Any) -> str:
    """Flatten MCP tool-call content into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "output", "result"):
            if key in content:
                return extract_text(content[key])
        return json.dumps(content)
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def extract_json(content: Any) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of MCP content."""
    text = extract_text(content)
    if not text:
        return None
    # Prefer structuredContent if present.
    if isinstance(content, dict) and isinstance(content.get("structuredContent"), dict):
        return content["structuredContent"]
    candidates = [text]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidates.insert(0, match.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _find_tool(tools: Sequence[McpTool], candidates: Sequence[str]) -> Optional[McpTool]:
    by_name = {t.name: t for t in tools}
    for name in candidates:
        if name in by_name:
            return by_name[name]
    return None


def _find_task_tool_by_schema(tools: Sequence[McpTool]) -> Optional[McpTool]:
    """Last resort: a tool whose schema looks like a work dispatcher.

    Only used when no candidate name matched, and only if exactly one tool has
    a strongly dispatch-like schema. Ambiguity means no selection.
    """
    matches: List[McpTool] = []
    for tool in tools:
        props = set(tool.properties.keys())
        if len(props & set(TASK_SCHEMA_HINTS)) >= 2 and "status" not in tool.name:
            matches.append(tool)
    if len(matches) == 1:
        return matches[0]
    return None


class ArenaAdapter:
    """Discovers and drives the real Arena integration."""

    def __init__(self, config: ArenaConfig, *, base_dir: Optional[str] = None) -> None:
        self.config = config
        self.base_dir = base_dir
        self._client: Optional[McpClient] = None
        self.capability = ArenaCapability()

    # -- connection -------------------------------------------------------

    def _candidate_endpoints(self) -> List[Tuple[str, str, str]]:
        """Return (transport, endpoint, label) candidates in probe order."""
        out: List[Tuple[str, str, str]] = []
        transport = (self.config.transport or "auto").lower()
        endpoint = (self.config.endpoint or "auto").strip()

        if transport in {"http", "streamable_http", "streamable-http", "sse"}:
            if endpoint and endpoint != "auto":
                out.append(("streamable_http", endpoint, "configured endpoint"))
        elif transport == "stdio":
            if self.config.stdio_command:
                out.append(("stdio", self.config.stdio_command, "configured stdio command"))
        else:  # auto
            if endpoint and endpoint != "auto":
                if endpoint.startswith("http://") or endpoint.startswith("https://"):
                    out.append(("streamable_http", endpoint, "configured endpoint"))
                elif endpoint.startswith("stdio://"):
                    cmd = endpoint[len("stdio://") :]
                    out.append(("stdio", cmd, "configured stdio:// command"))
                else:
                    # Bare host: assume MCP at /mcp (documented arena-bridge path).
                    out.append(
                        ("streamable_http", endpoint.rstrip("/") + "/mcp", "configured host")
                    )
            if self.config.stdio_command:
                out.append(("stdio", self.config.stdio_command, "configured stdio command"))
            # Known local bridge port from the environment description.
            if not out:
                out.append(
                    (
                        "streamable_http",
                        "http://127.0.0.1:17864/mcp",
                        "default local arena bridge (/mcp)",
                    )
                )
        return out

    def connect(self) -> ArenaCapability:
        """Attempt each transport candidate and record the real outcome."""
        cap = ArenaCapability()
        if not self.config.enabled:
            cap.diagnostics.append("arena.enabled is false; adapter is disabled by configuration")
            self.capability = cap
            return cap

        for transport, endpoint, label in self._candidate_endpoints():
            cap.transport = transport
            cap.endpoint = endpoint
            try:
                if transport == "stdio":
                    command = [c for c in str(endpoint).split() if c]
                    client = connect_stdio(command, cwd=self.base_dir)
                else:
                    headers = self._auth_headers()
                    client = connect_streamable_http(endpoint, headers=headers)
            except McpError as exc:
                cap.errors.append(f"{label}: {type(exc).__name__}: {exc}")
                cap.diagnostics.append(
                    f"transport {transport} at {label} could not be established: {exc}"
                )
                continue
            except Exception as exc:  # noqa: BLE001 - report, never crash
                cap.errors.append(f"{label}: {type(exc).__name__}: {exc}")
                continue

            self._client = client
            cap.level = "CONNECTED"
            cap.discovered["server_info"] = (
                client._init_result.get("serverInfo", {}) if client._init_result else {}
            )
            cap.discovered["protocol_version"] = (
                client._init_result.get("protocolVersion") if client._init_result else None
            )
            try:
                cap.tools = client.list_tools()
            except McpError as exc:
                cap.errors.append(f"tools/list failed: {exc}")
                cap.diagnostics.append(
                    "MCP initialize succeeded but tools/list failed; "
                    "task capability is UNVERIFIED"
                )
                self.capability = cap
                return cap

            cap.task_tool = _find_tool(cap.tools, TASK_TOOL_CANDIDATES)
            if cap.task_tool is None:
                cap.task_tool = _find_task_tool_by_schema(cap.tools)
                if cap.task_tool is not None:
                    cap.diagnostics.append(
                        f"no candidate task-tool name matched; selected "
                        f"{cap.task_tool.name!r} by schema heuristic"
                    )
            cap.status_tool = _find_tool(cap.tools, STATUS_TOOL_CANDIDATES)
            cap.result_tool = _find_tool(cap.tools, RESULT_TOOL_CANDIDATES)

            if cap.task_tool is None:
                cap.level = "CONNECTED"
                cap.diagnostics.append(
                    "MCP connected but no task-dispatch tool was found in the real "
                    f"tools/list output ({[t.name for t in cap.tools]}). "
                    "Arena task capability is UNVERIFIED."
                )
                cap.discovered["task_tool_candidates"] = list(TASK_TOOL_CANDIDATES)
            else:
                cap.level = "TOOL_FOUND"
                cap.discovered["task_tool_schema"] = cap.task_tool.input_schema
                cap.diagnostics.append(
                    f"task tool discovered: {cap.task_tool.name} "
                    f"(required={cap.task_tool.required})"
                )
            self.capability = cap
            return cap

        # Nothing connected.
        cap.level = "NOT_CONNECTED"
        if not cap.errors:
            cap.errors.append("no transport candidate was configured")
        cap.diagnostics.append(
            "ARENA_NOT_CONNECTED: no MCP transport could be established. "
            "The orchestrator will run in fallback mode and will not dispatch "
            "implementation tasks."
        )
        self.capability = cap
        return cap

    def _auth_headers(self) -> Dict[str, str]:
        """Only read the token from the environment; never from config."""
        import os

        token = os.environ.get(self.config.token_env, "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    # -- rest bridge (documented arena-bridge HTTP API) -------------------

    def probe_rest_bridge(self) -> Dict[str, Any]:
        """Probe the documented arena-bridge REST API.

        This is a *separate* capability from the MCP implementation engine. It
        is reported on its own and never used to claim task dispatch works.
        """
        endpoint = (self.config.endpoint or "auto").strip()
        if endpoint in {"", "auto"}:
            endpoint = "http://127.0.0.1:17864"
        base = endpoint.rstrip("/")
        if base.endswith("/mcp"):
            base = base[: -len("/mcp")]
        result: Dict[str, Any] = {"base_url": base, "health": None, "tools": None, "errors": []}
        headers = self._auth_headers()
        try:
            with httpx.Client(timeout=self.config.request_timeout) as client:
                try:
                    resp = client.get(base + "/health", headers=headers)
                    result["health"] = {
                        "status_code": resp.status_code,
                        "body": self._safe_json(resp),
                    }
                except httpx.HTTPError as exc:
                    result["errors"].append(f"/health: {exc}")
                try:
                    resp = client.get(base + "/tools", headers=headers)
                    result["tools"] = {
                        "status_code": resp.status_code,
                        "body": self._safe_json(resp),
                    }
                except httpx.HTTPError as exc:
                    result["errors"].append(f"/tools: {exc}")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"probe failed: {exc}")
        return result

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return resp.text[:500]

    # -- dispatch ---------------------------------------------------------

    def build_dispatch_arguments(self, task: Any) -> Tuple[Dict[str, Any], List[str]]:
        """Map a task onto the *discovered* schema. Refuse unknown requirements."""
        if self.capability.task_tool is None:
            return {}, ["no task tool discovered"]
        schema = self.capability.task_tool.input_schema or {}
        props = schema.get("properties", {}) or {}
        required = list(schema.get("required", []) or [])
        args: Dict[str, Any] = {}
        missing: List[str] = []

        field_map = {
            "objective": getattr(task, "objective", ""),
            "description": getattr(task, "objective", ""),
            "prompt": getattr(task, "objective", ""),
            "instruction": getattr(task, "objective", ""),
            "task": getattr(task, "objective", ""),
            "message": getattr(task, "objective", ""),
            "goal": getattr(task, "objective", ""),
            "repo": getattr(task, "project", ""),
            "repository": getattr(task, "project", ""),
            "branch": getattr(task, "branch", ""),
            "acceptance_criteria": getattr(task, "acceptance_criteria", []),
        }
        for name in props:
            if name in field_map and field_map[name] not in (None, ""):
                args[name] = field_map[name]
        for req in required:
            if req not in args:
                if req in field_map:
                    missing.append(f"required property {req!r} has no value")
                else:
                    missing.append(
                        f"required property {req!r} is not supplied by the orchestrator; "
                        "the real schema must be adapted before dispatch"
                    )
        return args, missing

    def dispatch(self, task: Any) -> Dict[str, Any]:
        """Call the discovered task tool. Returns a structured result.

        Raises ``McpError`` on transport/protocol failure so the caller can mark
        the task ``DISPATCH_UNKNOWN`` and reconcile rather than blindly retry.
        """
        if self._client is None or self.capability.task_tool is None:
            raise McpError("Arena is not connected or no task tool is discovered")
        args, missing = self.build_dispatch_arguments(task)
        if missing:
            return {
                "accepted": False,
                "error": "schema mismatch",
                "missing": missing,
                "arguments": args,
            }
        result = self._client.call_tool(self.capability.task_tool.name, args)
        payload = extract_json(result)
        ref = None
        if payload:
            for key in ("task_id", "id", "conversation_id", "session_id", "ref", "reference"):
                if key in payload and isinstance(payload[key], (str, int)):
                    ref = str(payload[key])
                    break
        content = extract_text(result)
        accepted = result.get("isError") is not True
        if accepted:
            self.capability.level = "DISPATCH_ACCEPTED"
        return {
            "accepted": accepted,
            "tool": self.capability.task_tool.name,
            "arguments": args,
            "ref": ref,
            "payload": payload,
            "text": content[:4000],
            "raw": result,
        }

    def get_status(self, ref: str) -> Dict[str, Any]:
        if self._client is None:
            raise McpError("Arena is not connected")
        if self.capability.status_tool is None:
            return {"available": False, "reason": "no status tool discovered"}
        args = self._ref_args(self.capability.status_tool, ref)
        result = self._client.call_tool(self.capability.status_tool.name, args)
        return {"available": True, "payload": extract_json(result), "text": extract_text(result)}

    def get_result(self, ref: str) -> Dict[str, Any]:
        if self._client is None:
            raise McpError("Arena is not connected")
        if self.capability.result_tool is None:
            return {"available": False, "reason": "no result tool discovered"}
        args = self._ref_args(self.capability.result_tool, ref)
        result = self._client.call_tool(self.capability.result_tool.name, args)
        payload = extract_json(result)
        if payload is not None:
            self.capability.level = "COMPLETED"
        return {"available": True, "payload": payload, "text": extract_text(result)}

    @staticmethod
    def _ref_args(tool: McpTool, ref: str) -> Dict[str, Any]:
        props = set(tool.properties.keys())
        for name in ("task_id", "id", "conversation_id", "session_id", "ref", "reference"):
            if name in props:
                return {name: ref}
        if props:
            # Only if the schema declares exactly one property.
            if len(props) == 1:
                return {next(iter(props)): ref}
        return {}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
