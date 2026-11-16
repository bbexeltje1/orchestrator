"""Safe Git inspection helpers.

Only read operations are provided by default. Every command goes through the
policy command allowlist, is run without a shell, and is validated against the
repository path and branch rules. Destructive operations are never implemented
here; they must be approved and executed by a human.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import is_safe_branch, is_safe_repo_path, redact
from ..policy import Policy


class GitError(Exception):
    pass


@dataclass
class GitInspection:
    repo_root: str = ""
    branch: str = ""
    is_repo: bool = False
    changed_files: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    head: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repo_root": self.repo_root,
            "branch": self.branch,
            "is_repo": self.is_repo,
            "changed_files": self.changed_files,
            "untracked": self.untracked,
            "head": self.head,
            "errors": self.errors,
        }


class GitInspector:
    def __init__(self, repo_path: str | Path, *, policy: Optional[Policy] = None) -> None:
        self.repo_path = Path(repo_path)
        self.policy = policy or Policy()
        self._root: Optional[Path] = None

    def _run(self, args: List[str], timeout: float = 30.0) -> Tuple[int, str, str]:
        ok, reason = self.policy.validate_command(["git"] + args)
        if not ok:
            raise GitError(f"git command rejected by policy: {reason}")
        try:
            proc = subprocess.run(
                ["git"] + args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git execution failed: {exc}") from exc
        return proc.returncode, proc.stdout, proc.stderr

    def resolve_repo(self) -> Optional[Path]:
        """Walk upward to find a .git directory; validate the path first."""
        if not is_safe_repo_path(str(self.repo_path)):
            raise GitError(f"unsafe repository path: {self.repo_path!r}")
        candidate = self.repo_path.resolve()
        for path in [candidate, *candidate.parents]:
            if (path / ".git").exists():
                self._root = path
                return path
        return None

    def inspect(self) -> GitInspection:
        result = GitInspection(repo_root=str(self.repo_path))
        root = self.resolve_repo()
        if root is None:
            result.errors.append(f"no git repository found at or above {self.repo_path}")
            return result
        result.repo_root = str(root)
        result.is_repo = True

        rc, out, err = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0:
            result.branch = out.strip()
        else:
            result.errors.append(redact(err.strip()))

        rc, out, err = self._run(["rev-parse", "--short", "HEAD"])
        if rc == 0:
            result.head = out.strip()

        rc, out, err = self._run(["status", "--porcelain"])
        if rc == 0:
            for line in out.splitlines():
                if len(line) < 4:
                    continue
                status = line[:2]
                path = line[3:].strip()
                if "??" in status:
                    result.untracked.append(path)
                else:
                    result.changed_files.append(path)
        else:
            result.errors.append(redact(err.strip()))
        return result

    def diff_stat(self) -> str:
        rc, out, err = self._run(["diff", "--stat"])
        return out if rc == 0 else redact(err)

    def branch_exists(self, branch: str) -> bool:
        ok, _ = self.policy.validate_branch(branch)
        if not ok:
            return False
        rc, out, _ = self._run(["rev-parse", "--verify", branch])
        return rc == 0

    def create_branch(self, branch: str, base: Optional[str] = None) -> None:
        """Create an isolated feature branch (non-destructive).

        Requires an explicit approved operation; the caller must have checked
        policy. Never checks out a branch that does not exist without creating
        it, and never deletes anything.
        """
        ok, reason = self.policy.validate_branch(branch)
        if not ok:
            raise GitError(f"refusing unsafe branch name: {reason}")
        if self.branch_exists(branch):
            raise GitError(f"branch {branch!r} already exists")
        args = ["checkout", "-b", branch]
        if base:
            ok, reason = self.policy.validate_branch(base)
            if not ok:
                raise GitError(f"unsafe base branch: {reason}")
            args.append(base)
        rc, out, err = self._run(args)
        if rc != 0:
            raise GitError(f"failed to create branch {branch!r}: {redact(err.strip())}")
