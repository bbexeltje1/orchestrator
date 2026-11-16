"""Local HTTP stubs for Ollama and Qwen, used only by the test suite.

They implement the *verified* shapes:

* Ollama: ``GET /api/tags``, ``POST /v1/systemone``
* Qwen/llama.cpp: ``GET /v1/models``, ``POST /v1/chat/completions``
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


class _Base:
    def __init__(self) -> None:
        self.requests: list = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self):
        raise NotImplementedError

    def _send(self, handler, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class OllamaStub(_Base):
    """Implements /api/tags and /v1/systemone."""

    def __init__(
        self,
        *,
        model: str = "HAL-X/thx-02:q4_k_m",
        decision: str = "DISPATCH",
        systemone_status: int = 200,
        chat_reply: str = "DISPATCH",
    ) -> None:
        self.model = model
        self.decision = decision
        self.systemone_status = systemone_status
        self.chat_reply = chat_reply
        self.confidence = 0.91
        self.probabilities = {
            "DISPATCH": 0.91,
            "WAIT": 0.03,
            "VERIFY": 0.02,
            "REQUEST_CORRECTION": 0.01,
            "REQUEST_QWEN": 0.01,
            "REQUEST_HUMAN_APPROVAL": 0.01,
            "STOP": 0.01,
        }
        super().__init__()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if self.path == "/api/tags":
                    outer._send(
                        self,
                        {
                            "models": [
                                {"name": outer.model},
                                {"name": "other:model"},
                            ]
                        },
                    )
                elif self.path == "/v1/systemone":
                    outer._send(self, {"error": "method not allowed"}, 405)
                else:
                    outer._send(self, {"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path, body))
                if self.path == "/v1/systemone":
                    if outer.systemone_status != 200:
                        outer._send(
                            self,
                            {"error": "systemone unavailable"},
                            outer.systemone_status,
                        )
                    else:
                        outer._send(
                            self,
                            {
                                "model": body.get("model"),
                                "answers": {
                                    "workflow": {
                                        "type": "choice",
                                        "choice": outer.decision,
                                        "probabilities": outer.probabilities,
                                        "confidence": outer.confidence,
                                    }
                                },
                                "usage": {"input_tokens": 10, "output_tokens": 1},
                            },
                        )
                elif self.path == "/v1/chat/completions":
                    outer._send(
                        self,
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": outer.chat_reply},
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    )
                else:
                    outer._send(self, {"error": "not found"}, 404)

        return Handler


class QwenStub(_Base):
    """Implements /v1/models and /v1/chat/completions."""

    def __init__(self, *, model: str = "Qwen/Qwen3-8B", reply: str = "") -> None:
        self.model = model
        self.reply = reply or (
            '[{"objective":"Implement feature X",'
            '"acceptance_criteria":["tests pass"],"branch":""}]'
        )
        super().__init__()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if self.path == "/models":
                    outer._send(self, {"data": [{"id": outer.model}, {"id": "other:model"}]})
                else:
                    outer._send(self, {"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path, body))
                if self.path == "/chat/completions":
                    outer._send(
                        self,
                        {
                            "id": "chatcmpl-stub",
                            "model": body.get("model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": outer.reply},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                        },
                    )
                else:
                    outer._send(self, {"error": "not found"}, 404)

        return Handler
