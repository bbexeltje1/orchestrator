"""Qwen complex-reasoning engine.

Connects to the *existing* local llama.cpp OpenAI-compatible service and
discovers the real model id from ``/v1/models`` instead of assuming one.
Optionally routes through OmniRoute, but only after validating that OmniRoute
actually exposes a compatible health endpoint and chat route. OmniRoute's API
shape is never assumed; if it cannot be validated it stays disabled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from ..config import OmniRouteConfig, QwenConfig
from ..models import ServiceHealth


class QwenError(Exception):
    pass


@dataclass
class QwenResult:
    text: str
    model: str
    raw: Dict[str, Any]
    routed_via: str = "direct"
    usage: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = {}


class QwenEngine:
    def __init__(
        self,
        config: QwenConfig,
        omniroute: Optional[OmniRouteConfig] = None,
        *,
        client: Optional[httpx.Client] = None,
        logger: Any = None,
    ) -> None:
        self.config = config
        self.omniroute = omniroute
        self.log = logger
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout, connect=min(10.0, config.timeout))
        )
        self._owns_client = client is None
        self._resolved_model: Optional[str] = None
        self._omniroute_model: Optional[str] = None

    # -- discovery --------------------------------------------------------

    def discover_model(self) -> Optional[str]:
        """Inspect /v1/models and return the real model id, or None."""
        base = self.config.base_url.rstrip("/")
        try:
            resp = self._client.get(base + "/models", timeout=self.config.timeout)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if not data:
            return None
        ids: List[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        if not ids:
            return None
        if self.config.model and self.config.model != "auto":
            for mid in ids:
                if mid == self.config.model or mid.split(":")[0] == self.config.model.split(":")[0]:
                    self._resolved_model = mid
                    return mid
        self._resolved_model = ids[0]
        return ids[0]

    def health(self) -> tuple[ServiceHealth, str, Dict[str, Any]]:
        """Return (health, detail, discovered)."""
        if not self.config.enabled:
            return ServiceHealth.NOT_CONFIGURED, "qwen.enabled is false", {}
        base = self.config.base_url.rstrip("/")
        try:
            resp = self._client.get(base + "/models", timeout=self.config.timeout)
        except httpx.HTTPError as exc:
            return ServiceHealth.BLOCKED, f"qwen unreachable at {base}: {exc}", {}
        if resp.status_code != 200:
            return ServiceHealth.FAIL, f"qwen /models returned HTTP {resp.status_code}", {}
        try:
            body = resp.json()
        except ValueError:
            return ServiceHealth.FAIL, "qwen /models returned invalid JSON", {}
        ids = [str(m.get("id")) for m in (body.get("data") or []) if isinstance(m, dict)]
        if not ids:
            return ServiceHealth.UNVERIFIED, "qwen reachable but /models listed no models", {}
        model = self.discover_model()
        return (
            ServiceHealth.PASS,
            f"qwen reachable; model={model}",
            {"base_url": base, "model": model, "available_models": ids},
        )

    # -- OmniRoute validation ---------------------------------------------

    def validate_omniroute(self) -> tuple[bool, str, Dict[str, Any]]:
        """Detect whether OmniRoute exposes a usable OpenAI-compatible route.

        Returns (usable, detail, discovered). ``usable`` is only True when a
        real health check passes and a real chat route responds.
        """
        if self.omniroute is None or not self.omniroute.enabled:
            return False, "omniroute is disabled", {}
        base = self.omniroute.base_url.rstrip("/")
        health_path = (self.omniroute.health_path or "/health").split("?")[0]
        discovered: Dict[str, Any] = {"base_url": base, "health_path": health_path}
        try:
            resp = self._client.get(base + health_path, timeout=self.config.timeout)
        except httpx.HTTPError as exc:
            return False, f"omniroute health unreachable: {exc}", discovered
        discovered["health_status"] = resp.status_code
        if resp.status_code != 200:
            return False, f"omniroute health returned HTTP {resp.status_code}", discovered
        try:
            discovered["health_body"] = resp.json()
        except ValueError:
            discovered["health_body"] = resp.text[:300]

        # Validate a chat route with a tiny completion. Only a 2xx response
        # proves the route is real; we never assume the path.
        chat_candidates = ["/v1/chat/completions", "/chat/completions", "/v1/completions"]
        for path in chat_candidates:
            try:
                probe = self._client.post(
                    base + path,
                    json={
                        "model": self.config.model if self.config.model != "auto" else "probe",
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                    timeout=min(20.0, self.config.timeout),
                )
            except httpx.HTTPError:
                continue
            discovered.setdefault("chat_probe", {})[path] = probe.status_code
            if probe.status_code == 200:
                discovered["chat_route"] = path
                return True, f"omniroute usable at {base}{path}", discovered
        return (
            False,
            "omniroute health is OK but no OpenAI-compatible chat route could be validated",
            discovered,
        )

    # -- inference --------------------------------------------------------

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> QwenResult:
        if not self.config.enabled:
            raise QwenError("qwen is disabled")
        model = self._resolved_model or self.discover_model()
        if not model:
            raise QwenError("could not discover a Qwen model id from /v1/models")

        routed_via = "direct"
        base = self.config.base_url.rstrip("/")
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.config.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        if self.omniroute is not None and self.omniroute.enabled:
            usable, detail, discovered = self.validate_omniroute()
            if usable:
                base = self.omniroute.base_url.rstrip("/")
                payload["model"] = discovered.get("model") or model
                routed_via = "omniroute"
            else:
                # Never silently claim routing. Record and continue direct.
                self.last_omniroute_note = detail  # type: ignore[attr-defined]

        resp = self._client.post(base + "/chat/completions", json=payload, timeout=self.config.timeout)
        if resp.status_code != 200:
            raise QwenError(f"qwen chat returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise QwenError(f"qwen chat response malformed: {exc}") from exc
        return QwenResult(
            text=text,
            model=model,
            raw=body,
            routed_via=routed_via,
            usage=body.get("usage") or {},
        )

    # -- planning ----------------------------------------------------------

    def plan_goal(self, goal: str, context: str = "") -> List[Dict[str, Any]]:
        """Ask Qwen to decompose a goal into bounded tasks.

        Returns a validated list of task dicts. The orchestrator re-validates
        everything; a malformed plan yields an empty list, never a crash.
        """
        system = (
            "You are the planning engine for the DRIFTLINE Roblox project. "
            "Break the goal into 1-6 small, bounded implementation tasks. "
            "Each task must be independently verifiable and must respect that "
            "Roblox Studio design-time objects are the source of truth for "
            "visuals; runtime scripts only handle behavior. "
            "Respond with ONLY a JSON array of objects with keys: "
            "objective (string), acceptance_criteria (array of strings), "
            "branch (string, optional). Do not invent tools or commands."
        )
        user = f"Goal:\n{goal}\n\nRepository context:\n{context or '(none)'}\n\nPlan:"
        result = self.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
            temperature=0.1,
        )
        tasks = self._parse_tasks(result.text)
        return tasks

    @staticmethod
    def _parse_tasks(text: str) -> List[Dict[str, Any]]:
        import re

        candidates = [text]
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            candidates.insert(0, match.group(0))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, list):
                out: List[Dict[str, Any]] = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    objective = str(item.get("objective") or item.get("task") or "").strip()
                    if not objective:
                        continue
                    criteria = item.get("acceptance_criteria") or []
                    if isinstance(criteria, str):
                        criteria = [criteria]
                    out.append(
                        {
                            "objective": objective,
                            "acceptance_criteria": [str(c) for c in criteria],
                            "branch": str(item.get("branch") or "").strip(),
                        }
                    )
                return out
        return []

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
