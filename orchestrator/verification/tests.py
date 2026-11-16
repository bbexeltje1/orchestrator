"""Test execution with command allowlisting and timeouts."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..models import redact
from ..policy import Policy


class TestRunError(Exception):
    pass


@dataclass
class TestRun:
    name: str
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    passed: bool
    duration_s: float
    timeout: bool = False
    blocked_reason: str = ""
    parsed: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "returncode": self.returncode,
            "passed": self.passed,
            "duration_s": round(self.duration_s, 3),
            "timeout": self.timeout,
            "blocked_reason": self.blocked_reason,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "parsed": self.parsed,
        }


class TestRunner:
    # Tell pytest this is not a test class.
    __test__ = False

    def __init__(self, repo_path: str | Path, *, policy: Optional[Policy] = None) -> None:
        self.repo_path = Path(repo_path)
        self.policy = policy or Policy()

    def available_commands(self) -> dict:
        names = ["pytest", "rojo", "luau-analyze", "selene", "python", "python3"]
        return {n: shutil.which(n) for n in names}

    def run(
        self,
        name: str,
    argv: List[str],
        *,
        timeout: float = 300.0,
        env: Optional[dict] = None,
    ) -> TestRun:
        ok, reason = self.policy.validate_command(argv)
        if not ok:
            return TestRun(
                name=name,
                command=argv,
                returncode=-1,
                stdout="",
                stderr="",
                passed=False,
                duration_s=0.0,
                blocked_reason=reason,
            )
        import time

        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                env=env,
            )
            duration = time.monotonic() - start
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            return TestRun(
                name=name,
                command=argv,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                passed=proc.returncode == 0,
                duration_s=duration,
                parsed=self._parse(name, stdout, stderr),
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return TestRun(
                name=name,
                command=argv,
                returncode=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                passed=False,
                duration_s=duration,
                timeout=True,
            )
        except OSError as exc:
            return TestRun(
                name=name,
                command=argv,
                returncode=-1,
                stdout="",
                stderr=str(exc),
                passed=False,
                duration_s=time.monotonic() - start,
                blocked_reason=f"execution error: {redact(exc)}",
            )

    def run_suite(self, repo_path: Path, enabled: dict) -> List[TestRun]:
        runs: List[TestRun] = []
        avail = self.available_commands()
        if enabled.get("run_rojo_build") and avail.get("rojo") and (repo_path / "default.project.json").exists():
            runs.append(self.run("rojo-build", ["rojo", "build", "--output", "dist.rbxl"]))
        if enabled.get("luau_lsp_command"):
            runs.append(self.run("luau-analyze", enabled["luau_lsp_command"].split()))
        if enabled.get("run_tests") and avail.get("pytest"):
            runs.append(self.run("pytest", ["pytest", "-q", "--disable-warnings"]))
        return runs

    @staticmethod
    def _parse(name: str, stdout: str, stderr: str) -> dict:
        import re

        out: dict = {}
        if name == "pytest":
            match = re.search(r"(\d+) passed", stdout + stderr)
            if match:
                out["passed"] = int(match.group(1))
            match = re.search(r"(\d+) failed", stdout + stderr)
            if match:
                out["failed"] = int(match.group(1))
        return out
