"""Core domain models and enumerations.

Everything here is dependency-free and platform-neutral so the offline test
suite can import it on Linux, macOS and Windows alike.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    """Timezone-aware current UTC timestamp."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def new_id(prefix: str = "task") -> str:
    """Generate a short, collision-resistant identifier."""
    import secrets

    return f"{prefix}-{secrets.token_hex(8)}"


def make_idempotency_key(project: str, objective: str, acceptance: str) -> str:
    """Stable key so the same logical task is never dispatched twice."""
    raw = json.dumps(
        {"project": project, "objective": objective, "acceptance": acceptance},
        sort_keys=True,
        ensure_ascii=True,
    )
    return "idem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TaskStatus(str, enum.Enum):
    """Explicit, durable task states.

    The state machine in ``state_machine.py`` is the only thing allowed to
    move a task between these values.
    """

    READY = "READY"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Decision(str, enum.Enum):
    """The closed decision set THX-02 is permitted to emit.

    THX may only ever choose from these values. Anything else is treated as
    UNKNOWN and escalated; it is never mapped onto an executable action.
    """

    DISPATCH = "DISPATCH"
    WAIT = "WAIT"
    VERIFY = "VERIFY"
    REQUEST_CORRECTION = "REQUEST_CORRECTION"
    REQUEST_QWEN = "REQUEST_QWEN"
    REQUEST_HUMAN_APPROVAL = "REQUEST_HUMAN_APPROVAL"
    STOP = "STOP"


# A decision that cannot be acted on without escalation.
NON_EXECUTABLE_DECISIONS = {
    Decision.WAIT,
    Decision.REQUEST_HUMAN_APPROVAL,
    Decision.STOP,
}


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class ServiceHealth(str, enum.Enum):
    """Result vocabulary for every probe.

    PASS is never reported merely because a TCP port accepts a connection.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SKIPPED = "SKIPPED"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class AcceptanceCriterion:
    description: str
    check: str = ""  # command or verification id that proves it


@dataclass
class Task:
    """A single bounded implementation unit.

    The database layer persists every column. ``idempotency_key`` is unique in
    the database and is the primary defence against duplicate dispatches.
    """

    id: str
    idempotency_key: str
    project: str
    branch: str
    objective: str
    acceptance_criteria: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)
    dispatch_at: Optional[str] = None
    arena_ref: Optional[str] = None  # conversation / task reference
    status: str = TaskStatus.READY.value
    changed_files: List[str] = field(default_factory=list)
    test_results: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    retry_count: int = 0
    final_result: Optional[str] = None
    parent_goal: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "project": self.project,
            "branch": self.branch,
            "objective": self.objective,
            "acceptance_criteria": json.dumps(self.acceptance_criteria),
            "created_at": self.created_at,
            "dispatch_at": self.dispatch_at,
            "arena_ref": self.arena_ref,
            "status": self.status,
            "changed_files": json.dumps(self.changed_files),
            "test_results": json.dumps(self.test_results),
            "verification": json.dumps(self.verification),
            "error": self.error,
            "retry_count": self.retry_count,
            "final_result": self.final_result,
            "parent_goal": self.parent_goal,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Task":
        def _loads(value: Optional[str], default: Any) -> Any:
            if value in (None, ""):
                return default
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return default

        return cls(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            project=row["project"],
            branch=row["branch"],
            objective=row["objective"],
            acceptance_criteria=_loads(row.get("acceptance_criteria"), []),
            created_at=row["created_at"],
            dispatch_at=row.get("dispatch_at"),
            arena_ref=row.get("arena_ref"),
            status=row["status"],
            changed_files=_loads(row.get("changed_files"), []),
            test_results=_loads(row.get("test_results"), {}),
            verification=_loads(row.get("verification"), {}),
            error=row.get("error"),
            retry_count=int(row.get("retry_count") or 0),
            final_result=row.get("final_result"),
            parent_goal=row.get("parent_goal") or "",
            metadata=_loads(row.get("metadata"), {}),
        )


@dataclass
class ThxDecision:
    """A validated THX-02 decision plus its probability distribution."""

    decision: Decision
    probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "probabilities": self.probabilities,
            "confidence": self.confidence,
            "model": self.model,
        }


@dataclass
class HealthReport:
    name: str
    status: ServiceHealth
    detail: str = ""
    discovered: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "discovered": self.discovered,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Validation helpers (used by policy + config + verification)
# ---------------------------------------------------------------------------

_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/\\ ]+$")


def is_safe_branch(branch: str) -> bool:
    """Reject branch names that could smuggle shell/git options."""
    if not branch or branch.startswith("-") or ".." in branch:
        return False
    if branch != branch.strip():
        return False
    return bool(_GIT_BRANCH_RE.match(branch))


def is_safe_repo_path(path: str) -> bool:
    """Basic repository path validation (no traversal, no control chars)."""
    if not path or "\x00" in path:
        return False
    if path != path.strip():
        return False
    if ".." in path.replace("\\", "/").split("/"):
        return False
    return bool(_SAFE_PATH_RE.match(path))


def redact(text: Any, secrets: Optional[List[str]] = None) -> str:
    """Best-effort secret redaction for logs and reports."""
    if text is None:
        return ""
    out = str(text)
    patterns = [
        (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
        (re.compile(r"(?i)(api[_\-]?key\s*[:=]\s*)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
        (re.compile(r"(?i)(token\s*[:=]\s*)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
        (re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
        (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
        (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<redacted-github-token>"),
        (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<redacted-github-token>"),
    ]
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    for secret in secrets or []:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "<redacted>")
    return out
