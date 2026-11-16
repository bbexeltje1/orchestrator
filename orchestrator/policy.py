"""Safety policy: the authoritative decision layer.

THX-02 confidence is explicitly *not* an authorization mechanism. This module
is the only thing that decides whether an operation may run automatically,
must be approved by a human, or is denied outright.

Policy is deliberately conservative and fails closed: unknown operations are
not allowed automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .models import Decision


class Effect(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class Operation(str, Enum):
    # Routine, read-only or reversible development work.
    READ_REPO = "read_repo"
    CREATE_PLAN = "create_plan"
    DISPATCH_TASK = "dispatch_task"
    RUN_TESTS = "run_tests"
    RUN_BUILD = "run_build"
    INSPECT_GIT_DIFF = "inspect_git_diff"
    REQUEST_CORRECTION = "request_correction"
    REPEAT_VERIFICATION = "repeat_verification"
    SAVE_RESULT = "save_result"

    # Irreversible or security-sensitive.
    MERGE_BRANCH = "merge_branch"
    PUBLISH = "publish"
    DESTRUCTIVE_GIT = "destructive_git"
    DELETE_DATA = "delete_data"
    CHANGE_PERMISSIONS = "change_permissions"
    PRODUCTION_CHANGE = "production_change"
    OVERWRITE_USER_FILES = "overwrite_user_files"
    FORCE_PUSH = "force_push"


AUTO_ALLOWED: Set[Operation] = {
    Operation.READ_REPO,
    Operation.CREATE_PLAN,
    Operation.DISPATCH_TASK,
    Operation.RUN_TESTS,
    Operation.RUN_BUILD,
    Operation.INSPECT_GIT_DIFF,
    Operation.REQUEST_CORRECTION,
    Operation.REPEAT_VERIFICATION,
    Operation.SAVE_RESULT,
}

REQUIRE_APPROVAL: Set[Operation] = {
    Operation.MERGE_BRANCH,
    Operation.PUBLISH,
    Operation.DESTRUCTIVE_GIT,
    Operation.DELETE_DATA,
    Operation.CHANGE_PERMISSIONS,
    Operation.PRODUCTION_CHANGE,
    Operation.OVERWRITE_USER_FILES,
    Operation.FORCE_PUSH,
}

DENIED_ALWAYS: Set[Operation] = set()  # nothing is silently hard-coded off


# Operations that must never be triggered by a model decision alone.
MODEL_NEVER_AUTHORIZED: Set[Operation] = REQUIRE_APPROVAL | {
    Operation.DISPATCH_TASK,  # needs a human-submitted goal in manual/hybrid
}


# Command allowlist. Anything not matching is refused before execution.
ALLOWED_COMMAND_PATTERNS: List[re.Pattern] = [
    re.compile(r"^git\b"),
    re.compile(r"^python3?\b"),
    re.compile(r"^pytest\b"),
    re.compile(r"^rojo\b"),
    re.compile(r"^rojo.exe\b"),
    re.compile(r"^luau-analyze\b"),
    re.compile(r"^selene\b"),
    re.compile(r"^ollama\b"),
    re.compile(r"^ollama.exe\b"),
]

# Git subcommands that are never allowed to be run by the orchestrator.
GIT_DENIED_SUBCOMMANDS = {
    "push",
    "reset",
    "clean",
    "branch",  # branch deletion / -D handled via destructive ops
    "checkout",  # only explicit branch switch via approved helper
    "rebase",
    "cherry-pick",
    "filter-branch",
    "gc",
    "prune",
}

# Shell metacharacters that would indicate command injection / chaining.
_UNSAFE_SHELL = re.compile(r"[;&|`$><\n\r]")


@dataclass
class PolicyDecision:
    effect: Effect
    operation: Operation
    reason: str
    approval_id: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.effect is Effect.REQUIRE_APPROVAL


class Policy:
    """Stateless policy engine. Configuration only relaxes *documented* knobs."""

    def __init__(
        self,
        *,
        require_approval_for_merge: bool = True,
        require_approval_for_publish: bool = True,
        autonomy: str = "hybrid",
        dry_run: bool = True,
    ) -> None:
        self.require_approval_for_merge = require_approval_for_merge
        self.require_approval_for_publish = require_approval_for_publish
        self.autonomy = autonomy
        self.dry_run = dry_run

    # -- operation classification -----------------------------------------

    def evaluate(self, operation: Operation) -> PolicyDecision:
        if operation in DENIED_ALWAYS:
            return PolicyDecision(Effect.DENY, operation, "operation is denied by policy")
        if operation in AUTO_ALLOWED:
            return PolicyDecision(Effect.ALLOW, operation, "routine operation")
        if operation in REQUIRE_APPROVAL:
            return PolicyDecision(
                Effect.REQUIRE_APPROVAL,
                operation,
                f"{operation.value} is irreversible and requires explicit human approval",
            )
        return PolicyDecision(
            Effect.REQUIRE_APPROVAL,
            operation,
            "unknown operation: failing closed, approval required",
        )

    def check(self, operation: Operation) -> PolicyDecision:
        """Evaluate, honouring the merge/publish configuration toggles."""
        decision = self.evaluate(operation)
        if decision.effect is Effect.REQUIRE_APPROVAL:
            if operation is Operation.MERGE_BRANCH and not self.require_approval_for_merge:
                return PolicyDecision(Effect.ALLOW, operation, "merge approval disabled by config")
            if operation is Operation.PUBLISH and not self.require_approval_for_publish:
                return PolicyDecision(Effect.ALLOW, operation, "publish approval disabled by config")
        return decision

    # -- model decisions ---------------------------------------------------

    def decision_to_effect(self, decision: Decision) -> Effect:
        """Map a THX decision to an effect.

        No THX decision can authorize an irreversible operation. REQUEST_* and
        STOP/WAIT never authorize execution.
        """
        if decision is Decision.DISPATCH:
            return Effect.ALLOW
        if decision is Decision.VERIFY:
            return Effect.ALLOW
        if decision is Decision.REQUEST_CORRECTION:
            return Effect.ALLOW
        if decision is Decision.REQUEST_QWEN:
            return Effect.ALLOW
        return Effect.REQUIRE_APPROVAL

    def authorize_model_decision(self, decision: Decision, operation: Operation) -> PolicyDecision:
        """A model decision must still pass the operation policy."""
        op_decision = self.check(operation)
        model_effect = self.decision_to_effect(decision)
        if op_decision.effect is Effect.DENY:
            return op_decision
        if op_decision.effect is Effect.REQUIRE_APPROVAL:
            return op_decision
        if model_effect is Effect.REQUIRE_APPROVAL:
            return PolicyDecision(
                Effect.REQUIRE_APPROVAL,
                operation,
                f"THX decision {decision.value} does not authorize execution",
            )
        return op_decision

    # -- command safety ----------------------------------------------------

    @staticmethod
    def validate_command(argv: Sequence[str]) -> tuple[bool, str]:
        """Validate an argv list (never a shell string).

        Returns ``(ok, reason)``.
        """
        if not argv:
            return False, "empty command"
        if not isinstance(argv, (list, tuple)):
            return False, "command must be an argv list, not a shell string"
        exe = str(argv[0])
        base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
        allowed_names = {
            "git", "python", "python3", "pytest", "rojo", "rojo.exe",
            "luau-analyze", "selene", "ollama", "ollama.exe",
        }
        if base not in allowed_names:
            return False, f"executable {base!r} is not in the command allowlist"
        for token in argv[1:]:
            token = str(token)
            if _UNSAFE_SHELL.search(token):
                return False, f"unsafe shell metacharacter in argument {token!r}"
        if base == "git" and len(argv) >= 2:
            sub = argv[1]
            if sub == "checkout":
                # Only branch *creation* is allowed; plain switching is not.
                if len(argv) >= 3 and argv[2] in {"-b", "-B"}:
                    return True, ""
                return False, "git checkout is only permitted for branch creation (-b)"
            if sub in GIT_DENIED_SUBCOMMANDS:
                return False, f"git {sub} is not permitted"
        return True, ""

    # -- repository path / branch safety -----------------------------------

    @staticmethod
    def validate_branch(branch: str) -> tuple[bool, str]:
        from .models import is_safe_branch

        if not is_safe_branch(branch):
            return False, f"unsafe branch name {branch!r}"
        return True, ""


def decision_is_actionable(decision: Decision) -> bool:
    """True only for decisions that may lead to routine execution."""
    return decision in {Decision.DISPATCH, Decision.VERIFY, Decision.REQUEST_CORRECTION}
