"""State machine tests."""

import pytest

from orchestrator.models import TaskStatus
from orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    StateMachineError,
    allowed_from,
    can_transition,
    is_dispatchable,
    is_terminal,
    require_transition,
)


def test_ready_can_dispatch():
    assert can_transition(TaskStatus.READY, TaskStatus.DISPATCH_PENDING)


def test_illegal_transition_rejected():
    assert not can_transition(TaskStatus.READY, TaskStatus.VERIFIED)
    with pytest.raises(StateMachineError):
        require_transition(TaskStatus.READY, TaskStatus.VERIFIED)


def test_cancelled_is_terminal_and_immovable():
    assert is_terminal(TaskStatus.CANCELLED)
    assert not can_transition(TaskStatus.CANCELLED, TaskStatus.READY)
    assert allowed_from(TaskStatus.CANCELLED) == ()


def test_dispatch_unknown_never_goes_back_to_pending():
    assert not can_transition(TaskStatus.DISPATCH_UNKNOWN, TaskStatus.DISPATCH_PENDING)
    assert can_transition(TaskStatus.DISPATCH_UNKNOWN, TaskStatus.RUNNING)


def test_dispatchable_states():
    assert is_dispatchable(TaskStatus.READY)
    assert not is_dispatchable(TaskStatus.DISPATCH_PENDING)
    assert not is_dispatchable(TaskStatus.DISPATCH_UNKNOWN)
    assert not is_dispatchable(TaskStatus.RUNNING)


def test_table_is_closed_and_consistent():
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            assert isinstance(target, TaskStatus)
        assert source in ALLOWED_TRANSITIONS


def test_verified_can_requeue():
    assert can_transition(TaskStatus.VERIFIED, TaskStatus.READY)
    assert can_transition(TaskStatus.VERIFIED, TaskStatus.AWAITING_APPROVAL)
