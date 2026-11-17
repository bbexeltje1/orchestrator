"""Claude Code executor tests.

The real CLI is never required here: a fake runner plays the role of the
``claude`` executable and records every invocation. What is tested is the
adapter's honesty (no readiness without a real call), policy enforcement,
output/exit-status capture and the state-machine transitions. The REAL
invocation is covered by the Windows acceptance test (``claude-acceptance``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.agents.claude_code import (
    EXECUTION_VERIFIED,
    INSTALLED_NOT_AUTHENTICATED,
    INSTALLED_UNVERIFIED,
    NOT_INSTALLED,
    ClaudeCodeCapability,
    ClaudeCodeExecutor,
    ClaudeRunResult,
)
from orchestrator.cli import ConsoleSession, build_readiness
from orchestrator.config import ClaudeCodeConfig
from orchestrator.policy import Policy
from orchestrator.task_manager import TaskManager


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Stands in for subprocess.run; records argv and returns canned output."""

    def __init__(self, results=None, side_effect=None):
        self.results = list(results or [])
        self.side_effect = side_effect
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.side_effect is not None:
            self.side_effect(argv, **kwargs)
        if self.results:
            return self.results.pop(0)
        return FakeProc()


def _exe(tmp_path: Path, name: str = "claude") -> Path:
    path = tmp_path / name
    path.write_text("fake", encoding="utf-8")
    return path


# -- policy ----------------------------------------------------------------


def test_policy_agent_command_allows_freeform_prompt():
    argv = ["claude", "--output-format", "json", "-p", "fix greet(name) -> 'Hello, <name>!';\nuse x|y"]
    ok, reason = Policy.validate_agent_command(argv)
    assert ok, reason


def test_policy_agent_command_rejects_metacharacters_in_options():
    ok, reason = Policy.validate_agent_command(["claude", "--model", "x; rm -rf /", "-p", "hi"])
    assert not ok
    assert "metacharacter" in reason


def test_policy_agent_command_rejects_other_executables():
    ok, reason = Policy.validate_agent_command(["notclaude", "-p", "hi"])
    assert not ok
    assert "not an approved coding-agent CLI" in reason


def test_policy_command_allowlist_contains_claude():
    ok, reason = Policy.validate_command(["claude", "--version"])
    assert ok, reason


# -- detection / probe -----------------------------------------------------


def test_detect_missing_cli_is_never_ready(tmp_path):
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(command="claude-missing-xyz"), base_dir=tmp_path)
    cap = executor.detect()
    assert cap.level == NOT_INSTALLED
    assert cap.installed is False
    assert cap.execution_verified is None  # never claimed


def test_detect_installed_is_not_execution_verified(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    runner = FakeRunner([FakeProc(stdout="2.1.0 (Claude Code)\n")])
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    cap = executor.detect()
    assert cap.installed is True
    assert cap.version.startswith("2.1.0")
    assert cap.level == INSTALLED_UNVERIFIED  # detect alone is NEVER ready
    assert cap.execution_verified is None


def test_probe_success_is_execution_verified(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    probe_json = json.dumps({"type": "result", "is_error": False, "result": "OK", "session_id": "s1", "num_turns": 1})
    runner = FakeRunner([FakeProc(stdout="2.1.0\n"), FakeProc(stdout=probe_json)])
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    cap = executor.probe(scratch_dir=tmp_path / "scratch")
    assert cap.level == EXECUTION_VERIFIED
    assert cap.authenticated is True
    assert cap.execution_verified is True
    # the probe was a REAL invocation attempt through the runner
    assert any(call[0][0] == str(exe) for call in runner.calls)


def test_probe_auth_failure_reports_not_authenticated(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    runner = FakeRunner([FakeProc(stdout="2.1.0\n"), FakeProc(returncode=1, stderr="Error: not logged in. Please run `claude login`.")])
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    cap = executor.probe(scratch_dir=tmp_path / "scratch")
    assert cap.level == INSTALLED_NOT_AUTHENTICATED
    assert cap.authenticated is False
    assert cap.execution_verified is False
    assert "claude login" in cap.probe_detail


def test_probe_other_failure_stays_unverified(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    runner = FakeRunner([FakeProc(stdout="2.1.0\n"), FakeProc(returncode=2, stderr="error: unknown option --output-format")])
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    cap = executor.probe(scratch_dir=tmp_path / "scratch")
    assert cap.level == INSTALLED_UNVERIFIED
    assert cap.execution_verified is False
    assert cap.authenticated is None  # honestly unknown


# -- task execution --------------------------------------------------------


def test_run_task_captures_output_exit_and_changes(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    workdir = tmp_path / "work"
    payload = json.dumps(
        {"type": "result", "is_error": False, "result": "created test_greeting.py", "session_id": "abc", "num_turns": 3}
    )

    def write_file(argv, **kwargs):
        Path(kwargs["cwd"]).mkdir(parents=True, exist_ok=True)
        (Path(kwargs["cwd"]) / "test_greeting.py").write_text("def greet(name):\n    return 'Hello, '+name+'!'\n")

    runner = FakeRunner([FakeProc(stdout=payload)], side_effect=write_file)
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    result = executor.run_task("create test_greeting.py", workdir)
    assert result.returncode == 0
    assert result.ok is True
    assert result.is_error is False
    assert result.result_text == "created test_greeting.py"
    assert result.session_id == "abc"
    assert result.created_files == ["test_greeting.py"]
    assert result.log_path and Path(result.log_path).exists()
    # prompt is the final argv element; no shell is ever used
    argv, kwargs = runner.calls[0]
    assert argv[-2:] == ["-p", "create test_greeting.py"]
    assert kwargs.get("shell") is False


def test_run_task_records_failure_exit_status(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    payload = json.dumps({"type": "result", "is_error": True, "result": "I could not do that"})
    runner = FakeRunner([FakeProc(returncode=1, stdout=payload)])
    executor = ClaudeCodeExecutor(ClaudeCodeConfig(), base_dir=tmp_path, runner=runner)
    result = executor.run_task("something impossible", tmp_path / "w2")
    assert result.returncode == 1
    assert result.is_error is True
    assert result.ok is False


def test_run_task_refuses_bypass_permission_mode(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    monkeypatch.setattr("orchestrator.agents.claude_code.shutil.which", lambda name: str(exe))
    executor = ClaudeCodeExecutor(
        ClaudeCodeConfig(permission_mode="bypassPermissions"), base_dir=tmp_path, runner=FakeRunner()
    )
    result = executor.run_task("x", tmp_path / "w3")
    assert result.ok is False
    assert "not allowed" in result.blocked_reason


# -- TaskManager state-machine integration ---------------------------------


def _manager(base_dir, config, db):
    return TaskManager(db, config)


def test_execute_with_claude_transitions_to_needs_review(base_dir, config, db):
    executor = _StubExecutor(ClaudeRunResult(objective="o", workdir="w", returncode=0, is_error=False))
    tm = _manager(base_dir, config, db)
    task, _ = tm.create_task(project="P", branch="feature/x", objective="do it")
    outcome = tm.execute_with_claude(task.id, executor=executor, workdir=base_dir / "run" / "t")
    assert outcome.status == "NEEDS_REVIEW"
    assert outcome.dispatched is True
    stored = db.get_task(task.id)
    assert stored.status == "NEEDS_REVIEW"


def test_execute_with_claude_verifies(base_dir, config, db):
    executor = _StubExecutor(ClaudeRunResult(objective="o", workdir="w", returncode=0, is_error=False))
    tm = _manager(base_dir, config, db)
    task, _ = tm.create_task(project="P", branch="feature/x", objective="do it")
    outcome = tm.execute_with_claude(
        task.id,
        executor=executor,
        workdir=base_dir / "run" / "t",
        verify_fn=lambda result: (True, "file created; test passed"),
    )
    assert outcome.status == "VERIFIED"
    assert "VERIFIED" in outcome.detail


def test_execute_with_claude_failure_is_failed(base_dir, config, db):
    executor = _StubExecutor(ClaudeRunResult(objective="o", workdir="w", returncode=7, is_error=True, result_text="nope"))
    tm = _manager(base_dir, config, db)
    task, _ = tm.create_task(project="P", branch="feature/x", objective="do it")
    outcome = tm.execute_with_claude(task.id, executor=executor, workdir=base_dir / "run" / "t")
    assert outcome.status == "FAILED"
    stored = db.get_task(task.id)
    assert stored.status == "FAILED"


class _StubExecutor:
    def __init__(self, result):
        self.result = result

    def run_task(self, objective, workdir, *, timeout=None):
        return self.result


# -- readiness honesty -----------------------------------------------------


def test_readiness_reports_claude_rows_and_never_fakes_ready(base_dir, config, db, monkeypatch):
    from orchestrator.app import App

    class _NoArena:
        def connect(self):
            raise AssertionError("offline readiness must not connect")

    app = App.__new__(App)
    app.config = config
    app.base_dir = base_dir
    app.db = db
    app.arena = _NoArena()

    class _Wf:
        qwen = None
        thx = None

    app.workflow = _Wf()
    report = build_readiness(app, connect_arena=False)
    labels = [label for label, _ in report.rows]
    for expected in (
        "Claude Code installed",
        "Claude Code authenticated",
        "Claude Code execution available",
        "Qwen model available",
        "THX-02 available",
        "Arena bridge connected",
        "Task execution capability",
    ):
        assert expected in labels
    rows = dict(report.rows)
    # no CLI on the build machine -> nothing claude-related may be PASS
    assert rows["Claude Code installed"] != "PASS"
    assert rows["Claude Code execution available"] == "SKIPPED"  # offline: honestly unprobed


def test_readiness_pass_only_after_real_probe(base_dir, config, db, monkeypatch):
    from orchestrator.app import App

    cap = ClaudeCodeCapability(
        level=EXECUTION_VERIFIED,
        installed=True,
        executable="claude",
        version="2.1.0",
        authenticated=True,
        execution_verified=True,
        probe_detail="real probe invocation succeeded",
    )

    class _Arena:
        def connect(self):
            class _Cap:
                connected = False
                level = "NOT_CONNECTED"
                errors = []
                diagnostics = []
                tools = []
                can_dispatch = False
                task_tool = None

                def to_dict(self):
                    return {}

            return _Cap()

        def probe_rest_bridge(self):
            return {"probed": False}

    app = App.__new__(App)
    app.config = config
    app.base_dir = base_dir
    app.db = db
    app.arena = _Arena()

    class _Wf:
        qwen = None
        thx = None

    app.workflow = _Wf()
    monkeypatch.setattr(
        "orchestrator.cli.ClaudeCodeExecutor.probe", lambda self, **kw: cap
    )
    report = build_readiness(app, connect_arena=True)
    rows = dict(report.rows)
    assert rows["Claude Code installed"] == "PASS"
    assert rows["Claude Code authenticated"] == "PASS"
    assert rows["Claude Code execution available"] == "PASS"
    assert rows["Task execution capability"] == "PASS"
    assert "claude_code" in report.details["Task execution capability"]


# -- the acceptance command (offline, stubbed agent) -----------------------


def test_claude_acceptance_command_runs_full_loop(base_dir, monkeypatch, capsys):
    from orchestrator.agents.claude_code import ClaudeRunResult as _Res

    class _StubClaude:
        def __init__(self, config, *, base_dir=".", runner=None):
            self.config = config
            self.base_dir = Path(base_dir)

        def detect(self):
            return ClaudeCodeCapability(level=EXECUTION_VERIFIED, installed=True, execution_verified=True)

        def run_task(self, objective, workdir, *, timeout=None):
            workdir = Path(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "test_greeting.py").write_text(
                "def greet(name):\n    return f'Hello, {name}!'\n\n"
                "def test_greet():\n    assert greet('World') == 'Hello, World!'\n",
                encoding="utf-8",
            )
            return _Res(
                objective=objective,
                workdir=str(workdir),
                returncode=0,
                is_error=False,
                result_text="created test_greeting.py",
                created_files=["test_greeting.py"],
            )

    monkeypatch.setattr("orchestrator.cli.ClaudeCodeExecutor", _StubClaude)
    out, done = ConsoleSession(base_dir=base_dir).execute("claude-acceptance")
    assert done is False
    assert "ACCEPTANCE: PASS" in out
    assert "CREATED" in out
    assert "VERIFIED" in out


def test_claude_run_requires_yes(base_dir, monkeypatch):
    class _StubClaude:
        def __init__(self, config, *, base_dir=".", runner=None):
            pass

        def detect(self):
            return ClaudeCodeCapability(installed=True, level=EXECUTION_VERIFIED, execution_verified=True)

    monkeypatch.setattr("orchestrator.cli.ClaudeCodeExecutor", _StubClaude)
    out, _ = ConsoleSession(base_dir=base_dir).execute("claude-run write hello world")
    assert "Nothing has run yet" in out
    assert "--yes" in out
