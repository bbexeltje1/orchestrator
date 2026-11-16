"""The orchestration loop.

Python owns the workflow. Qwen plans, THX advises, Arena implements — but
every transition is performed by ``TaskManager`` and every risky operation is
gated by ``Policy``.

Polling uses exponential backoff and is strictly non-AI: a status poll never
calls an LLM.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .agents.arena import ArenaAdapter
from .config import Config
from .engines.qwen import QwenEngine
from .engines.thx02 import Thx02Engine
from .models import Decision, Task, TaskStatus, redact
from .policy import Effect, Operation, Policy
from .storage.database import Database
from .task_manager import DispatchOutcome, TaskManager


@dataclass
class GoalResult:
    goal: str
    created_tasks: List[str] = field(default_factory=list)
    outcomes: List[Dict[str, Any]] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "created_tasks": self.created_tasks,
            "outcomes": self.outcomes,
            "blocked_reason": self.blocked_reason,
        }


class Workflow:
    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        arena: Optional[ArenaAdapter] = None,
        qwen: Optional[QwenEngine] = None,
        thx: Optional[Thx02Engine] = None,
        task_manager: Optional[TaskManager] = None,
        policy: Optional[Policy] = None,
        logger: Any = None,
    ) -> None:
        self.db = db
        self.config = config
        self.arena = arena
        self.qwen = qwen
        self.thx = thx
        self.policy = policy or Policy(
            autonomy=config.workflow.autonomy,
            dry_run=config.workflow.dry_run,
            require_approval_for_merge=config.workflow.require_approval_for_merge,
            require_approval_for_publish=config.workflow.require_approval_for_publish,
        )
        self.tm = task_manager or TaskManager(db, config, arena=arena, policy=self.policy, logger=logger)
        self.log = logger or _NullLogger()

    # -- planning --------------------------------------------------------

    def submit_goal(self, goal: str, context: str = "") -> GoalResult:
        """Qwen analyses the goal and produces bounded, validated tasks."""
        result = GoalResult(goal=goal)
        if not goal.strip():
            result.blocked_reason = "empty goal"
            return result
        if self.qwen is None:
            result.blocked_reason = "Qwen engine is not configured; cannot plan"
            return result

        try:
            planned = self.qwen.plan_goal(goal, context)
        except Exception as exc:  # noqa: BLE001
            result.blocked_reason = f"Qwen planning failed: {type(exc).__name__}: {redact(exc)}"
            return result
        if not planned:
            result.blocked_reason = "Qwen returned no usable tasks (malformed plan)"
            return result

        branch = self._feature_branch()
        for item in planned:
            objective = str(item.get("objective", "")).strip()
            if not objective:
                continue
            criteria = [str(c) for c in (item.get("acceptance_criteria") or [])]
            task, created = self.tm.create_task(
                project=self.config.project.name,
                branch=branch,
                objective=objective,
                acceptance_criteria=criteria,
                parent_goal=goal,
                metadata={"planned_by": "qwen"},
            )
            result.created_tasks.append(task.id)
        if not result.created_tasks:
            result.blocked_reason = "no tasks could be created from the plan"
        return result

    def _feature_branch(self) -> str:
        """Isolated default branch. Never touches a protected branch."""
        base = self.config.project.default_branch
        if base in {"", "auto"}:
            base = "main"
        return f"driftline/orchestrator-{int(time.time())}"

    # -- execution -------------------------------------------------------

    def run_task(self, task_id: str, *, max_polls: Optional[int] = None) -> List[DispatchOutcome]:
        """Dispatch one task and poll it to a terminal-ish state."""
        outcomes: List[DispatchOutcome] = []
        outcome = self.tm.dispatch(task_id)
        outcomes.append(outcome)
        if not outcome.dispatched:
            return outcomes

        max_polls = max_polls or self.config.arena.poll_max
        interval = float(self.config.arena.poll_interval)
        base = float(self.config.arena.backoff_base or 2.0)
        for attempt in range(max_polls):
            time.sleep(min(interval, 60.0))
            poll = self.tm.poll(task_id)
            outcomes.append(poll)
            if poll.status not in {TaskStatus.RUNNING.value}:
                break
            interval = min(interval * base, 60.0)
            self.log.info(
                "task %s still running (poll %d/%d)", task_id, attempt + 1, max_polls
            )
        return outcomes

    # -- hybrid decision -------------------------------------------------

    def consult_thx(self, state: Dict[str, Any]) -> Optional[Decision]:
        """Ask THX for a routine workflow decision; never let it authorize."""
        if self.thx is None:
            return None
        res = self.thx.classify(state)
        self.log.info("THX: %s", res.detail)
        if res.decision is None:
            self.log.warning("THX produced no actionable decision; escalating")
            return None
        decision = self.policy.authorize_model_decision(
            res.decision.decision, Operation.DISPATCH_TASK
        )
        if decision.effect is Effect.ALLOW:
            return res.decision.decision
        self.log.warning(
            "THX decision %s not authorized by policy (%s)",
            res.decision.decision.value,
            decision.reason,
        )
        return None

    # -- milestone loop --------------------------------------------------

    def run_milestone(self, goal: str, context: str = "") -> GoalResult:
        """Plan, then execute tasks one at a time until the milestone ends."""
        result = self.submit_goal(goal, context)
        if result.blocked_reason:
            return result
        for task_id in list(result.created_tasks):
            outcomes = self.run_task(task_id)
            result.outcomes.extend(o.to_dict() for o in outcomes)
        return result


class _NullLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
