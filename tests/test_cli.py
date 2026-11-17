"""Interactive console tests: routing, parsing, safe exit, real commands.

Everything runs offline against local stubs; no test contacts a real service.
"""

from __future__ import annotations

from orchestrator.cli import CONSOLE_HELP, ConsoleSession


def test_help_lists_commands():
    out, done = ConsoleSession().execute("help")
    assert done is False
    for command in ("doctor", "status", "submit", "approve", "quit"):
        assert command in out


def test_help_alias_question_mark():
    out, done = ConsoleSession().execute("?")
    assert out == CONSOLE_HELP
    assert done is False


def test_quit_and_exit_end_the_session():
    for line in ("quit", "exit"):
        out, done = ConsoleSession().execute(line)
        assert done is True
        assert out == ""


def test_blank_line_is_a_noop():
    assert ConsoleSession().execute("   ") == ("", False)
    assert ConsoleSession().execute("") == ("", False)


def test_unknown_command_reports_and_shows_help():
    out, done = ConsoleSession().execute("frobnicate --now")
    assert done is False
    assert "unknown command: frobnicate" in out
    assert "Commands:" in out


def test_unparseable_line_reports_error():
    out, done = ConsoleSession().execute('submit "unterminated quote')
    assert done is False
    assert "could not parse command" in out


def test_status_runs_through_the_cli(base_dir):
    out, done = ConsoleSession(base_dir=base_dir).execute("status")
    assert done is False
    assert "Task status counts" in out
    assert "Recent tasks" in out


def test_submit_empty_goal_is_blocked(base_dir):
    out, done = ConsoleSession(base_dir=base_dir).execute('submit ""')
    assert done is False
    assert "BLOCKED" in out
    assert "empty goal" in out


def test_submit_plans_via_qwen_stub(base_dir, qwen_stub, monkeypatch):
    monkeypatch.setenv("DRIFTLINE_QWEN_BASE_URL", qwen_stub.base_url)
    out, done = ConsoleSession(base_dir=base_dir).execute('submit "Add a combo counter"')
    assert done is False
    assert "created 1 task(s)" in out
    assert "planned only" in out


def test_submit_dry_run_flag_blocks_dispatch(base_dir, qwen_stub, monkeypatch):
    monkeypatch.setenv("DRIFTLINE_QWEN_BASE_URL", qwen_stub.base_url)
    out, _ = ConsoleSession(base_dir=base_dir).execute(
        'submit "Add a combo counter" --execute --dry-run'
    )
    assert "created 1 task(s)" in out
    assert "dry_run: not dispatching" in out


def test_cli_error_is_reported_not_raised(base_dir):
    # approve with an unknown id must not crash the console.
    out, done = ConsoleSession(base_dir=base_dir).execute("approve does-not-exist")
    assert done is False
    assert out.strip()


def test_init_upgrade_adds_services_section(base_dir):
    from pathlib import Path

    config_path = Path(base_dir) / "config" / "config.yaml"
    config_path.write_text("project:\n  name: DRIFTLINE\n", encoding="utf-8")
    out, done = ConsoleSession(base_dir=base_dir).execute("init --upgrade")
    assert done is False
    assert "added missing sections" in out
    assert "services" in out
    text = config_path.read_text(encoding="utf-8")
    assert "qwen_start_command" in text
    assert "project" in text  # existing content preserved


def test_init_upgrade_reports_nothing_changed(base_dir):
    out, _ = ConsoleSession(base_dir=base_dir).execute("init --upgrade")
    assert "nothing changed" in out
