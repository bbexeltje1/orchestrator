"""A real, local MCP server used only for tests.

It speaks the actual Streamable HTTP transport and the actual JSON-RPC methods
(``initialize``, ``notifications/initialized``, ``tools/list``, ``tools/call``)
so the adapter is exercised against the real protocol rather than a mock
object. It never contacts Arena; it is a test double.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2025-03-26"

TASK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {"type": "string", "description": "what to implement"},
        "branch": {"type": "string", "description": "git branch"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["objective"],
}

STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
}

RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
}


class McpStubState:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.task_status: str = "RUNNING"
        self.task_result: Dict[str, Any] = {"implemented": True, "files": ["src/a.lua"]}
        self.dispatch_response: Dict[str, Any] = {
            "task_id": "arena-stub-1",
            "conversation_id": "conv-stub-1",
            "status": "ACCEPTED",
        }
        self.fail_dispatch = False
        self.omit_task_tool = False

    def tools(self) -> List[Dict[str, Any]]:
        tools = [
            {
                "name": "arena_task",
                "description": "Dispatch an implementation task",
                "inputSchema": TASK_SCHEMA,
            },
            {
                "name": "arena_task_status",
                "description": "Poll a task",
                "inputSchema": STATUS_SCHEMA,
            },
            {
                "name": "arena_task_result",
                "description": "Fetch a task result",
                "inputSchema": RESULT_SCHEMA,
            },
            {
                "name": "unrelated_tool",
                "description": "noise",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
        if self.omit_task_tool:
            tools = [t for t in tools if t["name"] != "arena_task"]
        return tools


def make_handler(state: McpStubState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            pass

        def _send_json(self, payload: Dict[str, Any], status: int = 200, extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send_json({"status": "ok", "stub": True})
            elif self.path == "/tools":
                self._send_json({"tools": [t["name"] for t in state.tools()]})
            elif self.path == "/mcp":
                # Streamable HTTP servers may expose an SSE keep-alive here.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json({"error": "not found"}, 404)

        def do_DELETE(self):
            self._send_json({"ok": True})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                message = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"error": "bad json"}, 400)
                return
            method = message.get("method")
            msg_id = message.get("id")
            session = self.headers.get("Mcp-Session-Id") or "stub-session"

            if method == "initialize":
                self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "driftline-mcp-stub", "version": "1.0.0"},
                        },
                    },
                    extra_headers={"Mcp-Session-Id": session},
                )
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "ping":
                self._send_json({"jsonrpc": "2.0", "id": msg_id, "result": {}})
                return
            if method == "tools/list":
                self._send_json(
                    {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": state.tools()}},
                    extra_headers={"Mcp-Session-Id": session},
                )
                return
            if method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                state.calls.append((name, arguments))
                result = self._call(name, arguments)
                self._send_json(
                    {"jsonrpc": "2.0", "id": msg_id, "result": result},
                    extra_headers={"Mcp-Session-Id": session},
                )
                return
            self._send_json(
                {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "no method"}}
            )

        def _call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            if name == "arena_task":
                if state.fail_dispatch:
                    return {"content": [{"type": "text", "text": "rejected"}], "isError": True}
                payload = dict(state.dispatch_response)
                payload["arguments_received"] = arguments
                return {"content": [{"type": "text", "text": json.dumps(payload)}]}
            if name == "arena_task_status":
                return {
                    "content": [
                        {"type": "text", "text": json.dumps({"status": state.task_status})}
                    ]
                }
            if name == "arena_task_result":
                return {
                    "content": [{"type": "text", "text": json.dumps(state.task_result)}]
                }
            return {"content": [{"type": "text", "text": "{}"}]}

    return Handler


class StubMcpServer:
    def __init__(self, state: Optional[McpStubState] = None) -> None:
        self.state = state or McpStubState()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StubMcpServer":
        self._thread.start()
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def mcp_url(self) -> str:
        return self.base_url + "/mcp"
