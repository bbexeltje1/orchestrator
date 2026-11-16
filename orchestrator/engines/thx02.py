"""THX-02 fast decision engine.

Uses the *verified* Ollama System One contract:

    POST {base_url}/v1/systemone
    {
      "model": "...",
      "state": <string or JSON>,
      "questions": {
        "workflow": {
          "type": "choice",
          "instructions": "...",
          "criteria": {"DISPATCH": "...", ...}
        }
      }
    }

    -> {"answers": {"workflow": {"type": "choice", "choice": "DISPATCH",
        "probabilities": {...}, "confidence": 0.87}}}

The decision set is closed. If the model returns anything outside it, the
engine returns ``None`` (UNKNOWN) and the caller escalates. Confidence is
reported but never used to bypass policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from ..config import ThxConfig
from ..models import Decision, ServiceHealth, ThxDecision

CRITERIA: Dict[str, str] = {
    Decision.DISPATCH.value: "Dispatch the next bounded task to Arena now",
    Decision.WAIT.value: "Wait; the current task is still running or not ready",
    Decision.VERIFY.value: "Run verification on the current result",
    Decision.REQUEST_CORRECTION.value: "Ask Arena for a targeted correction",
    Decision.REQUEST_QWEN.value: "Escalate to Qwen for deep reasoning",
    Decision.REQUEST_HUMAN_APPROVAL.value: "Stop and ask a human for approval",
    Decision.STOP.value: "Stop the workflow; something is wrong or unsafe",
}

INSTRUCTIONS = (
    "You are the DRIFTLINE workflow router. Given the current workflow state, "
    "choose exactly one workflow action from the criteria. Do not invent tools, "
    "commands or arguments. If the state is ambiguous or unsafe, choose "
    "REQUEST_HUMAN_APPROVAL."
)


class ThxError(Exception):
    pass


@dataclass
class ThxResult:
    decision: Optional[ThxDecision]
    raw: Dict[str, Any]
    health: ServiceHealth
    detail: str = ""
    degraded: bool = False


class Thx02Engine:
    def __init__(
        self, config: ThxConfig, *, client: Optional[httpx.Client] = None, logger: Any = None
    ) -> None:
        self.config = config
        self.log = logger
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout, connect=min(10.0, config.timeout))
        )
        self._owns_client = client is None

    # -- health -----------------------------------------------------------

    def health(self) -> ThxResult:
        """Verify Ollama is reachable AND the configured model is present."""
        base = self.config.base_url.rstrip("/")
        try:
            resp = self._client.get(base + "/api/tags", timeout=self.config.timeout)
        except httpx.HTTPError as exc:
            return ThxResult(
                None, {}, ServiceHealth.BLOCKED, f"ollama unreachable at {base}: {exc}"
            )
        if resp.status_code != 200:
            return ThxResult(
                None,
                {},
                ServiceHealth.FAIL,
                f"ollama /api/tags returned HTTP {resp.status_code}",
            )
        try:
            tags = resp.json()
        except ValueError:
            return ThxResult(None, {}, ServiceHealth.FAIL, "ollama /api/tags returned invalid JSON")
        models = [m.get("name", "") for m in (tags.get("models") or [])]
        wanted = self.config.model
        if wanted == "auto":
            return ThxResult(
                None,
                {"models": models},
                ServiceHealth.PASS,
                f"ollama reachable; {len(models)} model(s) installed",
            )
        matches = [m for m in models if m == wanted or m.split(":")[0] == wanted.split(":")[0]]
        if not matches:
            return ThxResult(
                None,
                {"models": models},
                ServiceHealth.BLOCKED,
                f"model {wanted!r} is not installed. Installed: {models or 'none'}",
            )
        return ThxResult(
            None,
            {"models": models, "matched": matches[0]},
            ServiceHealth.PASS,
            f"ollama reachable; model {matches[0]!r} installed",
        )

    def decision_endpoint_supported(self) -> bool:
        base = self.config.base_url.rstrip("/")
        try:
            resp = self._client.post(
                base + self.config.decision_endpoint,
                json={"model": self.config.model, "state": "ping", "questions": {}},
                timeout=min(10.0, self.config.timeout),
            )
        except httpx.HTTPError:
            return False
        # A 404/405 means unsupported; a 4xx validation error means it exists.
        return resp.status_code not in {404, 405}

    # -- decision ---------------------------------------------------------

    def classify(self, state: Dict[str, Any] | str) -> ThxResult:
        """Ask THX-02 for a workflow decision and validate it strictly."""
        health = self.health()
        if health.health is not ServiceHealth.PASS:
            return ThxResult(None, health.raw, health.health, health.detail)

        base = self.config.base_url.rstrip("/")
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        question_name = "workflow"
        payload = {
            "model": self.config.model,
            "state": state_text,
            "questions": {
                question_name: {
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "criteria": CRITERIA,
                }
            },
        }
        try:
            resp = self._client.post(
                base + self.config.decision_endpoint, json=payload, timeout=self.config.timeout
            )
        except httpx.HTTPError as exc:
            return ThxResult(
                None, payload, ServiceHealth.BLOCKED, f"systemone request failed: {exc}"
            )
        if resp.status_code in {404, 405}:
            return self._fallback_chat(state_text, payload)
        if resp.status_code != 200:
            return ThxResult(
                None,
                payload,
                ServiceHealth.FAIL,
                f"systemone returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        try:
            body = resp.json()
        except ValueError:
            return ThxResult(None, payload, ServiceHealth.FAIL, "systemone returned invalid JSON")

        answer = ((body.get("answers") or {}).get(question_name)) or {}
        choice = answer.get("choice")
        probabilities = answer.get("probabilities") or {}
        confidence = answer.get("confidence")

        if choice not in CRITERIA:
            return ThxResult(
                None,
                body,
                ServiceHealth.UNVERIFIED,
                f"model returned unknown decision {choice!r}; escalating (not executing)",
            )
        if not isinstance(probabilities, dict) or not probabilities:
            return ThxResult(
                None,
                body,
                ServiceHealth.UNVERIFIED,
                "model returned no probability distribution; escalating",
            )

        conf = float(confidence) if isinstance(confidence, (int, float)) else None
        if conf is not None and conf < self.config.min_confidence:
            return ThxResult(
                None,
                body,
                ServiceHealth.UNVERIFIED,
                f"confidence {conf:.3f} below conservative threshold "
                f"{self.config.min_confidence:.3f}; escalating",
            )

        decision = ThxDecision(
            decision=Decision(choice),
            probabilities={str(k): float(v) for k, v in probabilities.items()},
            confidence=conf,
            raw=body,
            model=self.config.model,
        )
        return ThxResult(
            decision,
            body,
            ServiceHealth.PASS,
            f"decision={decision.decision.value} confidence={conf}",
        )

    def _fallback_chat(self, state_text: str, original_payload: Dict[str, Any]) -> ThxResult:
        """Optional degraded fallback to the OpenAI-compatible chat endpoint.

        Only used when ``/v1/systemone`` is unavailable and
        ``allow_fallback_chat`` is true. The result is marked degraded because a
        chat model is not a calibrated decision model.
        """
        if not self.config.allow_fallback_chat:
            return ThxResult(
                None,
                original_payload,
                ServiceHealth.BLOCKED,
                "systemone endpoint unavailable and chat fallback is disabled",
            )
        base = self.config.base_url.rstrip("/")
        prompt = (
            INSTRUCTIONS
            + "\n\nWorkflow state:\n"
            + state_text
            + "\n\nRespond with exactly one word: "
            + ", ".join(CRITERIA.keys())
            + "."
        )
        try:
            resp = self._client.post(
                base + "/v1/chat/completions",
                json={
                    "model": self.config.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
                timeout=self.config.timeout,
            )
        except httpx.HTTPError as exc:
            return ThxResult(
                None, original_payload, ServiceHealth.BLOCKED, f"chat fallback failed: {exc}"
            )
        if resp.status_code != 200:
            return ThxResult(
                None,
                original_payload,
                ServiceHealth.FAIL,
                f"chat fallback returned HTTP {resp.status_code}",
            )
        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ThxResult(None, original_payload, ServiceHealth.FAIL, "chat fallback malformed")

        token = (text or "").strip().split()[0].strip().upper() if text.strip() else ""
        if token not in CRITERIA:
            return ThxResult(
                None,
                body,
                ServiceHealth.UNVERIFIED,
                f"chat fallback returned non-decision {token!r}; escalating",
            )
        return ThxResult(
            ThxDecision(
                decision=Decision(token),
                probabilities={k: (1.0 if k == token else 0.0) for k in CRITERIA},
                confidence=None,
                raw=body,
                model=self.config.model,
            ),
            body,
            ServiceHealth.UNVERIFIED,
            "degraded chat fallback used (not a calibrated System One decision)",
            degraded=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
