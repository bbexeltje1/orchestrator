"""Task state machine.

Python is the only actor permitted to move tasks between states. AI decisions
are *inputs* to this machine; they can request a transition but never force
one. Every transition is validated and recorded.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Set, Tuple

from .models import TaskStatus


# The complete, closed transition table. Anything not listed is illegal.
ALLOWED_TRANSITIONS: Dict[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.READY: {
        TaskStatus.DISPATCH_PENDING,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.DISPATCH_PENDING: {
        TaskStatus.RUNNING,
        TaskStatus.DISPATCH_UNKNOWN,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    # A dispatch that may or may not have reached Arena must be reconciled
    # before any further dispatch. It can only move to RUNNING / FAILED /
    # BLOCKED after reconciliation, or be cancelled.
    TaskStatus.DISPATCH_UNKNOWN: {
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.RESULT_RECEIVED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RESULT_RECEIVED: {
        TaskStatus.VERIFYING,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.VERIFIED,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.VERIFIED: {
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.READY,  # re-queue for a correction pass
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.NEEDS_REVIEW: {
        TaskStatus.READY,  # correction pass
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.AWAITING_APPROVAL: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.READY,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,  # retry
        TaskStatus.CANCELLED,
    },
    TaskStatus.CANCELLED: set(),
}

TERMINAL_STATES: Set[TaskStatus] = {TaskStatus.VERIFIED, TaskStatus.CANCELLED}

# Statuses from which a dispatch would be illegal.
DISPATCH_FORBIDDEN: Set[TaskStatus] = {
    TaskStatus.DISPATCH_PENDING,
    TaskStatus.DISPATCH_UNKNOWN,
    TaskStatus.RUNNING,
    TaskStatus.VERIFYING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.CANCELLED,
}


class StateMachineError(Exception):
    """Raised when a transition violates the table."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal task transition {current} -> {target}")


def _as_status(value: str | TaskStatus) -> TaskStatus:
    return value if isinstance(value, TaskStatus) else TaskStatus(str(value))


def can_transition(current: str | TaskStatus, target: str | TaskStatus) -> bool:
    try:
        return _as_status(target) in ALLOWED_TRANSITIONS[_as_status(current)]
    except KeyError:
        return False


def allowed_from(current: str | TaskStatus) -> Tuple[str, ...]:
    cur = _as_status(current)
    return tuple(sorted(s.value for s in ALLOWED_TRANSITIONS.get(cur, set())))


def require_transition(current: str | TaskStatus, target: str | TaskStatus) -> None:
    if not can_transition(current, target):
        raise StateMachineError(str(current), str(target))


def is_terminal(status: str | TaskStatus) -> bool:
    return _as_status(status) in TERMINAL_STATES


def is_dispatchable(status: str | TaskStatus) -> bool:
    return _as_status(status) not in DISPATCH_FORBIDDEN
