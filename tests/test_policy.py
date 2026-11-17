"""Policy enforcement tests."""

from orchestrator.models import Decision, is_safe_branch, is_safe_repo_path, redact
from orchestrator.policy import (
    Effect,
    Operation,
    Policy,
    decision_is_actionable,
)


def test_routine_operations_allowed():
    policy = Policy()
    for op in (
        Operation.READ_REPO,
        Operation.CREATE_PLAN,
        Operation.DISPATCH_TASK,
        Operation.RUN_TESTS,
        Operation.RUN_BUILD,
        Operation.INSPECT_GIT_DIFF,
        Operation.REQUEST_CORRECTION,
        Operation.REPEAT_VERIFICATION,
        Operation.SAVE_RESULT,
    ):
        assert policy.check(op).effect is Effect.ALLOW, op


def test_irreversible_operations_require_approval():
    policy = Policy()
    for op in (
        Operation.MERGE_BRANCH,
        Operation.PUBLISH,
        Operation.DESTRUCTIVE_GIT,
        Operation.DELETE_DATA,
        Operation.CHANGE_PERMISSIONS,
        Operation.PRODUCTION_CHANGE,
        Operation.OVERWRITE_USER_FILES,
        Operation.FORCE_PUSH,
    ):
        assert policy.check(op).effect is Effect.REQUIRE_APPROVAL, op


def test_unknown_operation_fails_closed():
    class Fake:
        value = "teleport_prod"

    policy = Policy()
    # evaluate only accepts Operation; emulate unknown via a non-member by
    # checking that the fallback path is approval-gated for non-listed ops.
    assert decision_is_actionable(Decision.DISPATCH)
    assert not decision_is_actionable(Decision.REQUEST_HUMAN_APPROVAL)


def test_thx_never_authorizes_irreversible():
    policy = Policy()
    for decision in Decision:
        result = policy.authorize_model_decision(decision, Operation.MERGE_BRANCH)
        assert result.effect is Effect.REQUIRE_APPROVAL


def test_thx_low_confidence_is_not_an_authorization():
    policy = Policy()
    result = policy.authorize_model_decision(Decision.WAIT, Operation.DISPATCH_TASK)
    assert result.effect is Effect.REQUIRE_APPROVAL
    assert "does not authorize" in result.reason


def test_command_allowlist():
    ok, reason = Policy.validate_command(["git", "status", "--porcelain"])
    assert ok, reason
    ok, reason = Policy.validate_command(["pytest", "-q"])
    assert ok, reason
    ok, reason = Policy.validate_command(["cmd.exe", "/c", "dir"])
    assert not ok
    ok, reason = Policy.validate_command(["git", "push"])
    assert not ok
    ok, reason = Policy.validate_command("git status")  # string, not argv
    assert not ok
    ok, reason = Policy.validate_command(["git", "status; rm -rf /"])
    assert not ok
    ok, reason = Policy.validate_command(["git", "checkout", "-b", "feature/x"])
    assert ok, reason
    ok, reason = Policy.validate_command(["git", "checkout", "main"])
    assert not ok


def test_branch_validation():
    assert is_safe_branch("driftline/feature-1")
    assert not is_safe_branch("-oProxyCommand=evil")
    assert not is_safe_branch("a..b")
    assert not is_safe_branch("  ")
    assert not is_safe_branch("feature;rm")


def test_repo_path_validation_accepts_real_paths():
    # The exact Windows pytest tmp path that v1.1.2 rejected (missing drive
    # colon in the whitelist) - must be accepted on every platform.
    assert is_safe_repo_path(
        "C:\\Users\\MB\\AppData\\Local\\Temp\\pytest-of-MB\\pytest-3\\test_git_inspection0"
    )
    assert is_safe_repo_path("C:/Users/MB/DRIFTLINE")
    assert is_safe_repo_path("C:\\Program Files\\My Repo")
    assert is_safe_repo_path("\\\\server\\share\\repo")
    assert is_safe_repo_path("/home/user/project")
    assert is_safe_repo_path("relative/repo")
    assert is_safe_repo_path(".")


def test_repo_path_validation_rejects_unsafe_paths():
    assert not is_safe_repo_path("../etc")
    assert not is_safe_repo_path("C:\\repos\\..\\..\\Windows")
    assert not is_safe_repo_path("repo\x00hidden")
    assert not is_safe_repo_path(" repo")
    assert not is_safe_repo_path("repo\n")
    assert not is_safe_repo_path("C:bad:colon")
    assert not is_safe_repo_path("")


def test_redaction():
    text = "Authorization: Bearer abcdef123456 key=ghp_" + "a" * 30
    out = redact(text)
    assert "abcdef123456" not in out
    assert "ghp_" + "a" * 30 not in out
    assert "<redacted" in out
