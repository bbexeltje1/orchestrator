"""Command-line interface and readiness reporting."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agents.arena import ArenaAdapter
from .agents.claude_code import ClaudeCodeExecutor
from .app import App, bootstrap
from .config import ConfigError, load_config
from .engines.qwen import QwenEngine
from .engines.thx02 import Thx02Engine
from .models import ServiceHealth, TaskStatus, redact
from .verification.git import GitInspector

BANNER = "DRIFTLINE ORCHESTRATOR"


def _hr(label: str, value: str) -> str:
    return f"{label:<24} {value}"


class ReadinessReport:
    def __init__(self) -> None:
        self.rows: List[tuple[str, str]] = []
        self.details: Dict[str, Any] = {}
        self.errors: List[str] = []

    def add(self, label: str, status: ServiceHealth | str, detail: str = "") -> None:
        value = status.value if isinstance(status, ServiceHealth) else str(status)
        self.rows.append((label, value))
        if detail:
            self.details[label] = detail

    def render(self) -> str:
        lines = [BANNER, "=" * len(BANNER), ""]
        for label, value in self.rows:
            lines.append(_hr(label, value))
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  - {redact(err)}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rows": {label: value for label, value in self.rows},
            "details": self.details,
            "errors": [redact(e) for e in self.errors],
        }


def build_readiness(app: App, *, connect_arena: bool = True) -> ReadinessReport:
    report = ReadinessReport()
    cfg = app.config

    report.add("Python runtime", ServiceHealth.PASS, f"{sys.version.split()[0]}")
    report.add("Dependencies", ServiceHealth.PASS, "pyyaml, httpx")
    try:
        app.db.count_by_status()
        report.add("SQLite", ServiceHealth.PASS, str(app.db.path))
    except Exception as exc:  # noqa: BLE001
        report.add("SQLite", ServiceHealth.FAIL, str(exc))

    # Qwen
    if not cfg.qwen.enabled:
        report.add("Qwen local", ServiceHealth.NOT_CONFIGURED, "disabled")
    else:
        health, detail, discovered = app.workflow.qwen.health() if app.workflow.qwen else (
            ServiceHealth.SKIPPED,
            "engine unavailable",
            {},
        )
        report.add("Qwen local", health, detail)
        report.details["Qwen local"] = detail
        report.details["Qwen discovered"] = discovered

    # OmniRoute
    if not cfg.omniroute.enabled:
        report.add("OmniRoute", ServiceHealth.NOT_CONFIGURED, "disabled (optional)")
    elif app.workflow.qwen is not None:
        usable, detail, discovered = app.workflow.qwen.validate_omniroute()
        report.add(
            "OmniRoute",
            ServiceHealth.PASS if usable else ServiceHealth.BLOCKED,
            detail,
        )
        report.details["OmniRoute discovered"] = discovered

    # THX
    if not cfg.thx.enabled:
        report.add("THX-02", ServiceHealth.NOT_CONFIGURED, "disabled (fallback mode)")
    elif app.workflow.thx is not None:
        res = app.workflow.thx.health()
        report.add("THX-02", res.health, res.detail)
        report.details["THX-02 discovered"] = res.raw
        if res.health is ServiceHealth.PASS:
            supported = app.workflow.thx.decision_endpoint_supported()
            report.add(
                "THX decision endpoint",
                ServiceHealth.PASS if supported else ServiceHealth.UNVERIFIED,
                "/v1/systemone supported" if supported else "/v1/systemone NOT available",
            )

    # Arena
    arena_cap = None
    if not cfg.arena.enabled:
        report.add("Arena MCP", ServiceHealth.NOT_CONFIGURED, "disabled")
    elif connect_arena:
        cap = app.arena.connect()
        arena_cap = cap
        report.add("Arena MCP", ServiceHealth.PASS if cap.connected else ServiceHealth.BLOCKED, cap.level)
        report.details["Arena capability"] = cap.to_dict()
        if cap.connected:
            report.add(
                "Arena tools discovered",
                ServiceHealth.PASS if cap.tools else ServiceHealth.UNVERIFIED,
                f"{len(cap.tools)} tool(s): {[t.name for t in cap.tools][:12]}",
            )
            report.add(
                "Arena task capability",
                ServiceHealth.PASS if cap.can_dispatch else ServiceHealth.UNVERIFIED,
                f"task_tool={cap.task_tool.name if cap.task_tool else None}",
            )
        else:
            report.add("Arena tools discovered", ServiceHealth.BLOCKED, "not connected")
            report.add("Arena task capability", ServiceHealth.UNVERIFIED, "ARENA_NOT_CONNECTED")
            for err in cap.errors[:5]:
                report.errors.append(err)
            for diag in cap.diagnostics[:5]:
                report.errors.append(diag)
        # Documented REST bridge probe is reported separately.
        rest = app.arena.probe_rest_bridge()
        report.details["Arena REST bridge"] = rest
    else:
        report.add("Arena MCP", ServiceHealth.SKIPPED, "not probed")

    # Claude Code (official CLI; subscription login lives in the CLI itself -
    # never read, never bypassed). EXECUTION_VERIFIED requires a real probe call.
    claude_cap = None
    if not cfg.claude_code.enabled:
        report.add("Claude Code installed", ServiceHealth.NOT_CONFIGURED, "claude_code disabled in config")
        report.add("Claude Code authenticated", ServiceHealth.NOT_CONFIGURED, "claude_code disabled in config")
        report.add("Claude Code execution available", ServiceHealth.NOT_CONFIGURED, "claude_code disabled in config")
    else:
        executor = ClaudeCodeExecutor(cfg.claude_code, base_dir=app.base_dir)
        claude_cap = executor.probe() if connect_arena else executor.detect()
        report.add(
            "Claude Code installed",
            ServiceHealth.PASS if claude_cap.installed else ServiceHealth.BLOCKED,
            f"{claude_cap.executable} ({claude_cap.version})" if claude_cap.installed else claude_cap.probe_detail,
        )
        if not connect_arena:
            report.add("Claude Code authenticated", ServiceHealth.SKIPPED, "not probed (offline)")
            report.add("Claude Code execution available", ServiceHealth.SKIPPED, "not probed (offline)")
        elif claude_cap.execution_verified is True:
            report.add("Claude Code authenticated", ServiceHealth.PASS, "subscription login via claude CLI")
            report.add("Claude Code execution available", ServiceHealth.PASS, "real probe invocation succeeded")
        else:
            if claude_cap.authenticated is False:
                report.add("Claude Code authenticated", ServiceHealth.FAIL, claude_cap.probe_detail)
            elif claude_cap.installed:
                report.add("Claude Code authenticated", ServiceHealth.UNVERIFIED, claude_cap.probe_detail)
            else:
                report.add("Claude Code authenticated", ServiceHealth.BLOCKED, "CLI not installed")
            report.add(
                "Claude Code execution available",
                ServiceHealth.BLOCKED if claude_cap.installed else ServiceHealth.BLOCKED,
                claude_cap.probe_detail or "CLI not installed",
            )

    # Qwen model available (a real model, not just an open port)
    if not cfg.qwen.enabled:
        report.add("Qwen model available", ServiceHealth.NOT_CONFIGURED, "qwen disabled")
    elif app.workflow.qwen is not None:
        q_health, q_detail, q_disc = app.workflow.qwen.health()
        if q_health is ServiceHealth.PASS:
            report.add("Qwen model available", ServiceHealth.PASS, f"model={q_disc.get('model')}")
        elif q_health is ServiceHealth.UNVERIFIED:
            report.add("Qwen model available", ServiceHealth.BLOCKED, "qwen reachable but no model loaded")
        else:
            report.add("Qwen model available", q_health, q_detail)
    else:
        report.add("Qwen model available", ServiceHealth.SKIPPED, "engine unavailable")

    # THX-02 available (Ollama reachable AND the configured model present)
    if not cfg.thx.enabled:
        report.add("THX-02 available", ServiceHealth.NOT_CONFIGURED, "thx disabled")
    elif app.workflow.thx is not None:
        res = app.workflow.thx.health()
        report.add(
            "THX-02 available",
            ServiceHealth.PASS if res.health is ServiceHealth.PASS else res.health,
            res.detail,
        )
    else:
        report.add("THX-02 available", ServiceHealth.SKIPPED, "engine unavailable")

    # Arena bridge connected (the bridge at the configured endpoint)
    if not cfg.arena.enabled:
        report.add("Arena bridge connected", ServiceHealth.NOT_CONFIGURED, "arena disabled")
    elif arena_cap is not None:
        report.add(
            "Arena bridge connected",
            ServiceHealth.PASS if arena_cap.connected else ServiceHealth.BLOCKED,
            f"MCP {arena_cap.level}",
        )
    else:
        report.add("Arena bridge connected", ServiceHealth.SKIPPED, "not probed (offline)")

    # Actual task execution capability: which executor can really run a task now.
    executors: List[str] = []
    if claude_cap is not None and claude_cap.execution_verified is True:
        executors.append("claude_code")
    if arena_cap is not None and arena_cap.connected and arena_cap.can_dispatch:
        executors.append("arena")
    dry_state = "on" if cfg.workflow.dry_run else "off"
    dry_note = (
        f"workflow.dry_run={dry_state} (dry_run suppresses all Arena dispatch; "
        "Claude Code runs only via the explicit claude-run/claude-acceptance commands)"
    )
    if executors:
        report.add(
            "Task execution capability",
            ServiceHealth.PASS,
            f"executors: {', '.join(executors)}; {dry_note}",
        )
    elif not connect_arena:
        report.add("Task execution capability", ServiceHealth.SKIPPED, "not fully probed (offline); run doctor")
    else:
        report.add(
            "Task execution capability",
            ServiceHealth.BLOCKED,
            f"no verified executor (claude_code not execution-verified, arena not dispatch-capable); {dry_note}",
        )

    # Git
    try:
        repo = cfg.project.repository
        path = Path(repo) if repo not in {"auto", ""} else Path(app.base_dir)
        inspector = GitInspector(path)
        inspection = inspector.inspect()
        if inspection.is_repo:
            report.add(
                "Git repository",
                ServiceHealth.PASS,
                f"{inspection.repo_root} @ {inspection.branch}",
            )
        else:
            report.add("Git repository", ServiceHealth.BLOCKED, "; ".join(inspection.errors[:2]))
    except Exception as exc:  # noqa: BLE001
        report.add("Git repository", ServiceHealth.FAIL, redact(str(exc)))

    counts = app.db.count_by_status()
    workflow = "IDLE" if not counts else ",".join(f"{k}:{v}" for k, v in counts.items())
    report.add("Workflow", workflow)

    pending = app.db.pending_approvals()
    if pending:
        report.add("Pending approvals", str(len(pending)), ", ".join(p["id"] for p in pending))
    return report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        app = bootstrap(base_dir=args.base_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return 2
    try:
        report = build_readiness(app)
        print(report.render())
        return 0 if not any(v in {"FAIL", "BLOCKED"} for _, v in report.rows) else 1
    finally:
        app.close()


def cmd_report(args: argparse.Namespace) -> int:
    app = bootstrap(base_dir=args.base_dir)
    try:
        report = build_readiness(app, connect_arena=not args.offline)
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0
    finally:
        app.close()


def cmd_status(args: argparse.Namespace) -> int:
    app = bootstrap(base_dir=args.base_dir)
    try:
        counts = app.db.count_by_status()
        print("Task status counts:")
        for status in TaskStatus:
            if counts.get(status.value):
                print(f"  {status.value:<20} {counts[status.value]}")
        tasks = app.db.list_tasks(limit=args.limit)
        print(f"\nRecent tasks (limit {args.limit}):")
        for task in tasks:
            print(
                f"  {task.id}  {task.status:<18} {task.branch:<28} {task.objective[:50]}"
            )
        pending = app.db.pending_approvals()
        if pending:
            print("\nPending approvals:")
            for approval in pending:
                print(f"  {approval['id']}  {approval['operation']}  {approval['reason']}")
        return 0
    finally:
        app.close()


def cmd_submit(args: argparse.Namespace) -> int:
    app = bootstrap(base_dir=args.base_dir)
    try:
        if args.dry_run:
            app.config.workflow.dry_run = True
        result = app.workflow.submit_goal(args.goal, context=args.context)
        print(f"goal: {result.goal}")
        print(f"created {len(result.created_tasks)} task(s): {result.created_tasks}")
        if result.blocked_reason:
            print(f"BLOCKED: {result.blocked_reason}")
            return 1
        if args.execute:
            if app.config.workflow.dry_run:
                print("dry_run: not dispatching. Re-run with --execute after disabling dry_run.")
                return 0
            for task_id in result.created_tasks:
                outcomes = app.workflow.run_task(task_id)
                for outcome in outcomes:
                    print(f"  {outcome.status:<18} {outcome.detail}")
        else:
            print("planned only. Use --execute to dispatch after review.")
        return 0
    finally:
        app.close()


def cmd_idle(args: argparse.Namespace) -> int:
    app = bootstrap(base_dir=args.base_dir)
    try:
        report = build_readiness(app)
        print(report.render())
        print("\nSafe idle mode. No implementation task has been dispatched.")
        print('Submit a goal with: python -m orchestrator.cli submit "<goal>"')
        print("Or run the interactive console: python -m orchestrator.cli console")
        return 0
    finally:
        app.close()


# ---------------------------------------------------------------------------
# Interactive console (everything from one window)
# ---------------------------------------------------------------------------

CONSOLE_COMMANDS = {
    "doctor",
    "report",
    "status",
    "submit",
    "approve",
    "init",
    "qwen-models",
    "claude-run",
    "claude-acceptance",
}

CONSOLE_HELP = """\
Commands:
  doctor                        probe every service and print a readiness report
  report [--offline]            readiness report as JSON
  status [--limit N]            show task status
  submit "<goal>" [--context "<c>"] [--execute] [--dry-run]
                                submit a development goal (planning only unless
                                --execute and workflow.dry_run is false)
  approve <approval-id> [--deny]
                                resolve a pending approval
  init [--force]                write config/config.yaml from defaults
  qwen-models [--json] [--root DIR]
                                discover local .gguf models and print
                                paste-ready services.qwen_start_command lines
  claude-run <objective> --yes  run one real coding task via the Claude Code CLI
  claude-acceptance             safe acceptance test in a disposable repository
  help                          show this help
  quit                          leave the console (Ctrl+D also works)"""


class ConsoleSession:
    """One interactive console session.

    Lines are routed through the exact same argparse handlers as the
    non-interactive CLI, so behaviour never diverges between the two. All
    output is captured and returned, which keeps the console fully testable
    offline without stdin/stdout games.
    """

    def __init__(self, base_dir: str | Path = ".") -> None:
        self.base_dir = str(base_dir)

    def execute(self, line: str) -> Tuple[str, bool]:
        """Run one console line.

        Returns ``(output, done)``; ``done`` is True when the session must end
        (``quit`` / ``exit``).
        """
        line = (line or "").strip()
        if not line:
            return "", False
        if line in {"quit", "exit"}:
            return "", True
        if line in {"help", "?"}:
            return CONSOLE_HELP, False
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return f"could not parse command: {exc}\n{CONSOLE_HELP}", False
        if parts[0] not in CONSOLE_COMMANDS:
            return f"unknown command: {parts[0]}\n{CONSOLE_HELP}", False
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = main(["--base-dir", self.base_dir, *parts])
        except SystemExit:
            buffer.write("usage error; type 'help' for commands\n")
        except Exception as exc:  # noqa: BLE001
            buffer.write(f"error: {type(exc).__name__}: {redact(str(exc))}\n")
        else:
            if code:
                buffer.write(f"(exit code {code})\n")
        return buffer.getvalue(), False


def cmd_console(args: argparse.Namespace) -> int:
    session = ConsoleSession(base_dir=args.base_dir)
    print(f"{BANNER} - console")
    print("Everything runs from this window. Type 'help' for commands, 'quit' to leave.")
    while True:
        try:
            line = input("driftline> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue
        output, done = session.execute(line)
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        if done:
            break
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    app = bootstrap(base_dir=args.base_dir)
    try:
        status = app.db.resolve_approval(args.approval_id, granted=not args.deny, resolver="human")
        print(f"approval {args.approval_id} -> {status}")
        return 0
    finally:
        app.close()


def cmd_qwen_models(args: argparse.Namespace) -> int:
    """Discover local .gguf models; print paste-ready Qwen start commands.

    Discovery only - a model is never chosen automatically. The user picks
    exactly one line and puts it in services.qwen_start_command.
    """
    from pathlib import Path as _Path

    from .model_discovery import discover_gguf_models, find_llama_server

    server = getattr(args, "server", "") or find_llama_server() or ""
    roots = [_Path(r) for r in (getattr(args, "root", None) or [])] or None
    models = discover_gguf_models(roots)
    if getattr(args, "json", False):
        print(json.dumps({"llama_server": server, "models": models}))
        return 0
    print(f"llama-server: {server or 'NOT FOUND on PATH'}")
    if not models:
        print("No .gguf models found in the default locations")
        print("  (~/models, ~/Models, ~/Downloads, ~/Desktop, llama-server folder).")
        print("Search elsewhere with: qwen-models --root <directory>")
        print("Or edit config/config.yaml directly once you know your model path.")
        return 0
    print("Candidate models (newest first):")
    for i, model in enumerate(models, 1):
        print(f"  [{i}] {model['path']}  ({model['size']})")
    if server:
        print("\nPaste-ready services.qwen_start_command lines (choose exactly one):")
        for model in models:
            print(f'  "{server}" -m "{model["path"]}" --port 8090')
    return 0


ACCEPTANCE_OBJECTIVE = (
    "Create exactly one file named test_greeting.py in the current directory. "
    "It must define a function greet(name) that returns exactly 'Hello, <name>!' "
    "and include a pytest test named test_greet that asserts "
    "greet('World') == 'Hello, World!'. "
    "Do not create or modify any other files."
)


def _print_claude_outcome(outcome) -> None:
    print(f"task:   {outcome.task_id}")
    print(f"status: {outcome.status}")
    print(f"detail: {outcome.detail}")
    raw = outcome.raw or {}
    if raw:
        print(f"exit:   {raw.get('returncode')}  session: {raw.get('session_id') or '-'}  turns: {raw.get('num_turns') or '-'}")
        print(f"created:  {raw.get('created_files')}")
        print(f"modified: {raw.get('modified_files')}")
        if raw.get("log_path"):
            print(f"log:    {raw['log_path']} (full agent output)")
        tail = (raw.get("result_text_tail") or "").strip()
        if tail:
            print("agent result (tail):")
            for line in tail.splitlines()[-12:]:
                print(f"  {line}")


def cmd_claude_run(args: argparse.Namespace) -> int:
    """Submit a real coding task to the official Claude Code CLI."""
    objective = " ".join(args.objective).strip()
    if not objective:
        print("objective must not be empty")
        return 1
    app = bootstrap(base_dir=args.base_dir)
    try:
        cfg = app.config.claude_code
        if not cfg.enabled:
            print("claude_code is disabled in config/config.yaml")
            return 1
        executor = ClaudeCodeExecutor(cfg, base_dir=app.base_dir)
        cap = executor.detect()
        if not cap.installed:
            print(f"BLOCKED: {cap.probe_detail}")
            return 1
        workdir = (
            Path(args.workdir)
            if args.workdir
            else Path(app.base_dir) / "run" / f"claude-task-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        )
        if not args.yes:
            print("claude-run is a real Claude Code CLI invocation (your subscription).")
            print(f"  workdir:   {workdir}")
            print(f"  objective: {objective}")
            print("Nothing has run yet. Re-run with --yes to execute.")
            return 0
        task, _ = app.tasks.create_task(
            project=app.config.project.name,
            branch="feature/claude-local",
            objective=objective,
            metadata={"executor": "claude_code", "workdir": str(workdir), "kind": "claude-run"},
        )
        outcome = app.tasks.execute_with_claude(
            task.id,
            executor=executor,
            workdir=workdir,
            timeout=args.timeout or cfg.timeout,
        )
        _print_claude_outcome(outcome)
        return 0 if outcome.status in {"NEEDS_REVIEW", "VERIFIED"} else 1
    finally:
        app.close()


def cmd_claude_acceptance(args: argparse.Namespace) -> int:
    """TASK-4 acceptance: one real coding task in a disposable repository."""
    from .verification.tests import TestRunner

    app = bootstrap(base_dir=args.base_dir)
    try:
        cfg = app.config.claude_code
        if not cfg.enabled:
            print("claude_code is disabled in config/config.yaml")
            return 1
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        workdir = (
            Path(args.workdir)
            if args.workdir
            else Path(app.base_dir) / "run" / f"claude-acceptance-{stamp}"
        )
        workdir.mkdir(parents=True, exist_ok=True)
        print(f"disposable test repository: {workdir}")
        runner = TestRunner(workdir, policy=app.policy)
        init_run = runner.run("git-init", ["git", "init"])
        if init_run.passed:
            print("  git repository initialised (disposable)")
        else:
            print(f"  (git init skipped: {init_run.blocked_reason or 'exit ' + str(init_run.returncode)})")

        executor = ClaudeCodeExecutor(cfg, base_dir=app.base_dir)
        cap = executor.detect()
        if not cap.installed:
            print(f"BLOCKED: {cap.probe_detail}")
            return 2

        task, _ = app.tasks.create_task(
            project=app.config.project.name,
            branch="feature/claude-local",
            objective=ACCEPTANCE_OBJECTIVE,
            acceptance_criteria=[
                "test_greeting.py created by the agent",
                "pytest test_greet passes",
            ],
            metadata={"executor": "claude_code", "workdir": str(workdir), "kind": "claude-acceptance"},
        )
        expected = workdir / "test_greeting.py"

        def verify(result) -> tuple[bool, str]:
            if not expected.exists():
                return False, "expected file test_greeting.py was NOT created"
            parts = ["file test_greeting.py created"]
            test_run = runner.run(
                "pytest-test-greeting",
                [sys.executable, "-m", "pytest", "test_greeting.py", "-q"],
                timeout=120.0,
            )
            parts.append(f"pytest exit={test_run.returncode}")
            if not test_run.passed:
                tail = (test_run.stdout + test_run.stderr)[-400:].replace("\n", " ")
                return False, "; ".join(parts) + f"; test FAILED: {tail}"
            parts.append("test passed")
            return True, "; ".join(parts)

        outcome = app.tasks.execute_with_claude(
            task.id,
            executor=executor,
            workdir=workdir,
            verify_fn=verify,
            timeout=args.timeout or cfg.timeout,
        )
        _print_claude_outcome(outcome)
        print(f"expected file: {'CREATED' if expected.exists() else 'MISSING'} ({expected})")
        ok = outcome.status == "VERIFIED"
        print("ACCEPTANCE: PASS" if ok else "ACCEPTANCE: FAIL")
        print(f"artifacts kept in: {workdir} (disposable; under run/, never in your project files)")
        return 0 if ok else 1
    finally:
        app.close()


def cmd_init(args: argparse.Namespace) -> int:
    from .config import DEFAULT_CONFIG, config_path_for, upgrade_config_file
    import yaml

    base = Path(args.base_dir)
    target = config_path_for(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force and not args.upgrade:
        print(f"{target} already exists; use --force to overwrite or --upgrade to add missing sections")
        return 1
    if args.upgrade and target.exists() and not args.force:
        try:
            added = upgrade_config_file(target)
        except ConfigError as exc:
            print(f"configuration error: {exc}")
            return 2
        if added:
            print(f"upgraded {target}: added missing sections: {', '.join(added)}")
        else:
            print(f"{target} already has every known section; nothing changed")
        return 0
    target.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driftline-orchestrator")
    parser.add_argument("--base-dir", default=".", help="project base directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="probe every service and print a readiness report")
    sub.add_parser("report", help="readiness report as JSON").add_argument(
        "--offline", action="store_true", help="do not connect to Arena"
    )
    sub.add_parser("status", help="show task status").add_argument("--limit", type=int, default=20)
    p_submit = sub.add_parser("submit", help="submit a development goal")
    p_submit.add_argument("goal")
    p_submit.add_argument("--context", default="")
    p_submit.add_argument("--execute", action="store_true")
    p_submit.add_argument("--dry-run", action="store_true")
    sub.add_parser("idle", help="safe idle mode (no dispatch)")
    sub.add_parser(
        "console",
        help="interactive console: drive everything from one window",
    )
    p_approve = sub.add_parser("approve", help="resolve a pending approval")
    p_approve.add_argument("approval_id")
    p_approve.add_argument("--deny", action="store_true")
    p_init = sub.add_parser("init", help="write config/config.yaml from defaults")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.add_argument(
        "--upgrade",
        action="store_true",
        help="add sections missing from an existing config; existing values are never touched",
    )
    p_qm = sub.add_parser(
        "qwen-models",
        help="discover local .gguf models and print paste-ready Qwen start commands",
    )
    p_qm.add_argument("--json", action="store_true", help="machine-readable output")
    p_qm.add_argument("--server", default="", help="llama-server path (default: search PATH)")
    p_qm.add_argument(
        "--root",
        action="append",
        default=[],
        help="extra directory to search (repeatable; replaces the default roots)",
    )
    p_cr = sub.add_parser(
        "claude-run",
        help="run one real coding task through the official Claude Code CLI",
    )
    p_cr.add_argument("objective", nargs="+", help="the coding task for Claude Code")
    p_cr.add_argument("--workdir", default="", help="directory to work in (default: run/claude-task-<ts>)")
    p_cr.add_argument("--timeout", type=float, default=0.0, help="run timeout in seconds (default: config)")
    p_cr.add_argument("--yes", action="store_true", help="actually execute (without this, only the plan is shown)")
    p_ca = sub.add_parser(
        "claude-acceptance",
        help="safe acceptance test: one coding task in a disposable repository",
    )
    p_ca.add_argument("--workdir", default="", help="disposable directory (default: run/claude-acceptance-<ts>)")
    p_ca.add_argument("--timeout", type=float, default=0.0, help="run timeout in seconds (default: config)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "report": cmd_report,
        "status": cmd_status,
        "submit": cmd_submit,
        "idle": cmd_idle,
        "console": cmd_console,
        "approve": cmd_approve,
        "init": cmd_init,
        "qwen-models": cmd_qwen_models,
        "claude-run": cmd_claude_run,
        "claude-acceptance": cmd_claude_acceptance,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
