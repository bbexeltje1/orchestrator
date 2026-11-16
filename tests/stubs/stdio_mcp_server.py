"""A stdio MCP server used only for tests.

Run as ``python tests/stubs/stdio_mcp_server.py``. It speaks newline-delimited
JSON-RPC over stdin/stdout, exactly as the MCP stdio transport specifies.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

PROTOCOL_VERSION = "2025-03-26"

TOOLS = [
    {
        "name": "arena_task",
        "description": "Dispatch an implementation task",
        "inputSchema": {
            "type": "object",
            "properties": {"objective": {"type": "string"}, "branch": {"type": "string"}},
            "required": ["objective"],
        },
    },
    {
        "name": "arena_task_status",
        "description": "Poll a task",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


def respond(message: Dict[str, Any]) -> Dict[str, Any]:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stdio-stub", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") == "arena_task":
            payload = {"task_id": "stdio-stub-1", "status": "ACCEPTED"}
            result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"status": "DONE"})}]}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "no"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") is None:
            continue  # notification
        out = json.dumps(respond(message), separators=(",", ":")) + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
