"""Verification helpers: git safety, test allowlist, Roblox QA rules."""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.policy import Policy
from orchestrator.verification.git import GitInspector
from orchestrator.verification.roblox_qa import RobloxQa
from orchestrator.verification.tests import TestRunner


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.lua").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_git_inspection(tmp_path):
    _make_repo(tmp_path)
    inspector = GitInspector(tmp_path)
    inspection = inspector.inspect()
    assert inspection.is_repo
    assert inspection.branch in {"main", "master"}
    assert inspection.head
    assert inspection.changed_files == []


def test_git_finds_parent_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    inspection = GitInspector(nested).inspect()
    assert inspection.is_repo
    assert inspection.repo_root == str(repo.resolve())


def test_git_rejects_unsafe_path():
    try:
        GitInspector("../etc").resolve_repo()
        raised = False
    except Exception:
        raised = True
    assert raised


def test_branch_creation_and_validation(tmp_path):
    _make_repo(tmp_path)
    inspector = GitInspector(tmp_path)
    inspector.create_branch("driftline/feature-x")
    assert inspector.branch_exists("driftline/feature-x")
    try:
        inspector.create_branch("-oProxyCommand=evil")
        raised = False
    except Exception:
        raised = True
    assert raised


def test_test_runner_blocks_disallowed_commands(tmp_path):
    runner = TestRunner(tmp_path, policy=Policy())
    run = runner.run("bad", ["cmd.exe", "/c", "echo hi"])
    assert run.passed is False
    assert run.blocked_reason


def test_test_runner_allows_git(tmp_path):
    runner = TestRunner(tmp_path, policy=Policy())
    run = runner.run("status", ["git", "status", "--porcelain"])
    assert run.blocked_reason == ""
    assert run.passed in {True, False}


def test_roblox_qa_never_changes_studio_state(tmp_path):
    qa = RobloxQa(tmp_path)
    report = qa.diagnostics(explicit_qa=False)
    assert report.studio_state_changed is False
    assert any("Play mode was not opened" in d for d in report.diagnostics)
    assert report.health.value == "NOT_CONFIGURED"


def test_roblox_qa_runtime_error_collection(tmp_path):
    qa = RobloxQa(tmp_path)
    log = tmp_path / "runtime.log"
    log.write_text("all good\nERROR: something broke\nstack begin\n", encoding="utf-8")
    errors = qa.collect_runtime_errors(log)
    assert len(errors) == 2


def test_roblox_qa_evidence(tmp_path):
    qa = RobloxQa(tmp_path)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    assert qa.attach_evidence(shot, tmp_path / "missing.png") == [str(shot)]
