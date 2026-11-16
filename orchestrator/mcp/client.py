"""A minimal, correct MCP client (JSON-RPC 2.0).

Supported transports
--------------------
``stdio``
    Newline-delimited JSON-RPC over a child process' stdin/stdout, as specified
    by MCP. A background reader thread demultiplexes responses by ``id``.
``streamable_http``
    MCP Streamable HTTP (protocol revision 2025-03-26): POST JSON-RPC to the
    endpoint, honour the ``Mcp-Session-Id`` header, accept ``application/json``
    or ``text/event-stream`` responses, and DELETE the session on close.

No other transport is implemented, because no other transport is part of the
specification. The client never guesses an endpoint: the caller supplies it and
discovery is reported honestly when it fails.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "driftline-orchestrator", "version": "1.0.1"}


class McpError(Exception):
    """Base class for MCP failures."""


class McpTransportError(McpError):
    """The transport could not be established or broke mid-flight."""


class McpTimeout(McpError):
    """A request timed out."""


class McpProtocolError(McpError):
    """The server returned a JSON-RPC error or malformed payload."""


class McpUnsupported(McpError):
    """The server does not support the negotiated transport/protocol."""


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> List[str]:
        return list(self.input_schema.get("required", []) or [])

    @property
    def properties(self) -> Dict[str, Any]:
        return dict(self.input_schema.get("properties", {}) or {})


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class StdioTransport:
    """Newline-delimited JSON-RPC over a subprocess."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> None:
        if not command:
            raise McpTransportError("stdio command is empty")
        self.command = [str(c) for c in command]
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._responses: Dict[Any, "queue.Queue[Optional[Dict[str, Any]]]"] = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._env = env
        self._cwd = cwd

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._cwd,
                env=self._env,
            )
        except (OSError, ValueError) as exc:
            raise McpTransportError(f"failed to start stdio command {self.command}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="mcp-stdio-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stream = self._proc.stdout
        try:
            for line in stream:
                if self._closed.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore non-JSON noise on stdout
                if not isinstance(message, dict):
                    continue
                msg_id = message.get("id")
                if msg_id is not None:
                    with self._lock:
                        q = self._responses.get(msg_id)
                    if q is not None:
                        q.put(message)
                # messages without an id are notifications; nothing to do
        except (ValueError, OSError):
            pass
        finally:
            # Unblock every waiter so callers fail rather than hang.
            with self._lock:
                for q in self._responses.values():
                    q.put(None)

    def request(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise McpTransportError("stdio transport is not started")
        msg_id = message.get("id")
        q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)
        with self._lock:
            if msg_id is not None:
                self._responses[msg_id] = q
        try:
            payload = json.dumps(message, separators=(",", ":")) + "\n"
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise McpTransportError(f"stdio write failed: {exc}") from exc
        try:
            response = q.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise McpTimeout(f"stdio request {msg_id} timed out after {self.timeout}s") from exc
        finally:
            with self._lock:
                self._responses.pop(msg_id, None)
        if response is None:
            raise McpTransportError("stdio transport closed before a response arrived")
        return response

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self) -> None:
        self._closed.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass


class StreamableHttpTransport:
    """MCP Streamable HTTP (POST JSON-RPC, optional Mcp-Session-Id)."""

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not url:
            raise McpTransportError("streamable HTTP endpoint is empty")
        self.url = url
        self.timeout = timeout
        self._headers = {k: v for k, v in (headers or {}).items() if v is not None}
        self._session_id: Optional[str] = None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout))
        )
        self._owns_client = client is None

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        headers.update(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _parse_body(response: httpx.Response) -> Optional[Dict[str, Any]]:
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            data_lines: List[str] = []
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
            for payload in data_lines:
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
            return None
        if not response.content:
            return None
        try:
            parsed = response.json()
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def request(self, message: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._client.post(
                self.url, headers=self._request_headers(), content=json.dumps(message)
            )
        except httpx.HTTPError as exc:
            raise McpTransportError(f"streamable HTTP request failed: {exc}") from exc

        if response.status_code == 404:
            raise McpUnsupported(
                f"endpoint {self.url} returned 404; this is not an MCP Streamable HTTP endpoint"
            )
        if response.status_code in {405, 400}:
            raise McpUnsupported(
                f"endpoint {self.url} returned {response.status_code}; "
                "Streamable HTTP is not supported there"
            )
        if response.status_code not in {200, 202}:
            raise McpTransportError(
                f"streamable HTTP endpoint returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if response.status_code == 202:
            # Notification accepted; there is no response payload.
            if message.get("id") is None:
                return {}
            raise McpTransportError("server returned 202 for a request expecting a response")

        parsed = self._parse_body(response)
        if parsed is None:
            raise McpProtocolError("could not parse a JSON-RPC response body")
        return parsed

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            response = self._client.post(
                self.url, headers=self._request_headers(), content=json.dumps(message)
            )
            session = response.headers.get("mcp-session-id")
            if session:
                self._session_id = session
        except httpx.HTTPError:
            pass

    def close(self) -> None:
        if self._session_id:
            try:
                self._client.delete(
                    self.url,
                    headers={
                        "Mcp-Session-Id": self._session_id,
                        "MCP-Protocol-Version": PROTOCOL_VERSION,
                    },
                )
            except httpx.HTTPError:
                pass
        if self._owns_client:
            self._client.close()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class McpClient:
    """JSON-RPC client over an MCP transport."""

    def __init__(self, transport: StdioTransport | StreamableHttpTransport) -> None:
        self.transport = transport
        self._next_id = 0
        self._init_result: Optional[Dict[str, Any]] = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        msg_id = self._id()
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        response = self.transport.request(message)
        if "error" in response and response["error"]:
            err = response["error"]
            raise McpProtocolError(
                f"MCP error for {method}: {err.get('code')} {err.get('message')}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP {method} returned no result object")
        return result

    def initialize(self) -> Dict[str, Any]:
        result = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._init_result = result
        self.transport.notify("notifications/initialized")
        return result

    def ping(self) -> bool:
        try:
            self._call("ping", {})
            return True
        except Exception:
            return False

    def list_tools(self) -> List[McpTool]:
        result = self._call("tools/list", {})
        tools: List[McpTool] = []
        for raw in result.get("tools", []) or []:
            if not isinstance(raw, dict) or "name" not in raw:
                continue
            tools.append(
                McpTool(
                    name=str(raw["name"]),
                    description=str(raw.get("description") or ""),
                    input_schema=dict(raw.get("inputSchema") or {}) or {},
                    raw=raw,
                )
            )
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def connect_stdio(
    command: Sequence[str], *, cwd: Optional[str] = None, timeout: float = 30.0
) -> McpClient:
    transport = StdioTransport(command, cwd=cwd, timeout=timeout)
    transport.start()
    client = McpClient(transport)
    client.initialize()
    return client


def connect_streamable_http(
    url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0
) -> McpClient:
    transport = StreamableHttpTransport(url, headers=headers, timeout=timeout)
    client = McpClient(transport)
    try:
        client.initialize()
    except Exception:
        transport.close()
        raise
    return client
