"""Command-line interface and readiness reporting."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agents.arena import ArenaAdapter
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
    if not cfg.arena.enabled:
        report.add("Arena MCP", ServiceHealth.NOT_CONFIGURED, "disabled")
    elif connect_arena:
        cap = app.arena.connect()
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
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
