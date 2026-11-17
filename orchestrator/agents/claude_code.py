"""Claude Code executor adapter (official CLI, own subscription login).

This module runs real coding tasks through the official Claude Code CLI in
headless print mode (``claude -p ... --output-format json``). It captures the
agent's actual output and exit status and reports exactly what changed in the
target directory.

Discipline, mirroring ``agents/arena.py``:

* Nothing is ever faked. A capability is only ``EXECUTION_VERIFIED`` after a
  **real** CLI invocation succeeded - the executable merely existing is never
  reported as ready.
* Credentials are never read, stored or exported (see SECURITY.md). The CLI
  authenticates with its own login (``claude login``); an unauthenticated CLI
  is reported as such, never worked around.
* Execution is explicit and bounded: one objective, one working directory,
  one timeout. The permission mode stays non-bypassing (default
  ``acceptEdits``); ``--dangerously-skip-permissions`` is never used.
* Every invocation passes ``Policy.validate_agent_command`` first.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import ClaudeCodeConfig
from ..policy import Policy
from ..models import redact

# Capability levels, reported separately and never conflated.
NOT_CONFIGURED = "NOT_CONFIGURED"
NOT_INSTALLED = "NOT_INSTALLED"
INSTALLED_UNVERIFIED = "INSTALLED_UNVERIFIED"
INSTALLED_NOT_AUTHENTICATED = "INSTALLED_NOT_AUTHENTICATED"
EXECUTION_VERIFIED = "EXECUTION_VERIFIED"

# Failure text that indicates missing/expired CLI login (subscription auth).
_AUTH_ERROR_PATTERNS = re.compile(
    r"(not logged in|log[\s_-]?in (is )?required|please (run |use )?`?claude (auth )?log"
    r"|authentication (required|failed|error)|unauthorized|invalid api key"
    r"|oauth|credential|login required|logged out|subscription)",
    re.IGNORECASE,
)

_PROBE_PROMPT = "Reply with exactly the single word OK."


@dataclass
class ClaudeCodeCapability:
    level: str = NOT_CONFIGURED
    installed: bool = False
    executable: str = ""
    version: str = ""
    authenticated: Optional[bool] = None
    execution_verified: Optional[bool] = None
    probe_detail: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "installed": self.installed,
            "executable": self.executable,
            "version": self.version,
            "authenticated": self.authenticated,
            "execution_verified": self.execution_verified,
            "probe_detail": self.probe_detail,
            "errors": list(self.errors),
        }


@dataclass
class ClaudeRunResult:
    """Outcome of one real coding-task run: output, exit status, changes."""

    objective: str
    workdir: str
    argv: List[str] = field(default_factory=list)
    returncode: int = -1
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    result_text: str = ""
    is_error: Optional[bool] = None
    session_id: str = ""
    num_turns: Optional[int] = None
    created_files: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    log_path: str = ""
    timeout: bool = False
    blocked_reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.blocked_reason == "" and self.returncode == 0 and self.is_error is not True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "workdir": self.workdir,
            "argv": [redact(a) for a in self.argv],
            "returncode": self.returncode,
            "duration_s": round(self.duration_s, 3),
            "ok": self.ok,
            "is_error": self.is_error,
            "session_id": self.session_id,
            "num_turns": self.num_turns,
            "created_files": self.created_files,
            "modified_files": self.modified_files,
            "log_path": self.log_path,
            "timeout": self.timeout,
            "blocked_reason": self.blocked_reason,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
            "result_text_tail": self.result_text[-2000:],
        }


class ClaudeCodeError(Exception):
    pass


class ClaudeCodeExecutor:
    """Runs real coding tasks through the official Claude Code CLI."""

    def __init__(
        self,
        config: ClaudeCodeConfig,
        *,
        base_dir: str | Path = ".",
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.runner = runner or subprocess.run

    # -- detection --------------------------------------------------------

    def resolve(self) -> Optional[str]:
        """Locate the CLI. Never executes anything beyond --version."""
        command = (self.config.command or "").strip()
        if not command:
            return None
        if "/" in command or "\\" in command:
            path = Path(command)
            return str(path) if path.exists() else None
        return shutil.which(command)

    def detect(self) -> ClaudeCodeCapability:
        """Installed + version only. Never reports readiness."""
        cap = ClaudeCodeCapability()
        if not self.config.enabled:
            cap.level = NOT_CONFIGURED
            cap.probe_detail = "claude_code.enabled is false"
            return cap
        exe = self.resolve()
        if not exe:
            cap.level = NOT_INSTALLED
            cap.probe_detail = (
                f"Claude Code CLI not found (claude_code.command={self.config.command!r}); "
                "install from https://claude.com/claude-code and run 'claude login'"
            )
            cap.errors.append(cap.probe_detail)
            return cap
        cap.installed = True
        cap.executable = exe
        try:
            proc = self.runner(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
            first_line = (proc.stdout or "").strip().splitlines()
            cap.version = first_line[0] if first_line else ""
            if proc.returncode != 0:
                cap.errors.append(f"'--version' exited {proc.returncode}")
        except Exception as exc:  # noqa: BLE001 - any spawn failure means unusable
            cap.errors.append(f"could not run --version: {redact(exc)}")
            cap.level = INSTALLED_UNVERIFIED
            cap.probe_detail = "CLI found but not runnable"
            return cap
        cap.level = INSTALLED_UNVERIFIED
        cap.probe_detail = f"installed ({cap.version or 'version unknown'}); not yet execution-verified"
        return cap

    def probe(self, *, scratch_dir: Optional[Path] = None) -> ClaudeCodeCapability:
        """One REAL minimal invocation - the only path to EXECUTION_VERIFIED.

        A successful probe proves installed + authenticated + executable in a
        single honest step. Auth-looking failures are classified as
        ``INSTALLED_NOT_AUTHENTICATED``; anything else stays unverified.
        """
        cap = self.detect()
        if cap.level in (NOT_CONFIGURED, NOT_INSTALLED):
            return cap
        exe = cap.executable
        argv = [exe, "--output-format", "json", "-p", _PROBE_PROMPT]
        ok, reason = Policy.validate_agent_command(argv)
        if not ok:
            cap.level = INSTALLED_UNVERIFIED
            cap.probe_detail = f"probe command refused by policy: {reason}"
            cap.errors.append(cap.probe_detail)
            return cap
        scratch = scratch_dir or (self.base_dir / "run" / f"claude-probe-{uuid.uuid4().hex[:8]}")
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            proc = self.runner(
                argv,
                cwd=str(scratch),
                capture_output=True,
                text=True,
                timeout=self.config.probe_timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            cap.level = INSTALLED_UNVERIFIED
            cap.execution_verified = False
            cap.probe_detail = f"probe timed out after {self.config.probe_timeout}s"
            cap.errors.append(cap.probe_detail)
            return cap
        except Exception as exc:  # noqa: BLE001
            cap.level = INSTALLED_UNVERIFIED
            cap.execution_verified = False
            cap.probe_detail = f"probe could not run: {redact(exc)}"
            cap.errors.append(cap.probe_detail)
            return cap
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        parsed = self._parse_json(proc.stdout or "")
        is_error = bool(parsed.get("is_error")) if parsed else proc.returncode != 0
        if proc.returncode == 0 and not is_error:
            cap.level = EXECUTION_VERIFIED
            cap.authenticated = True
            cap.execution_verified = True
            cap.probe_detail = "real probe invocation succeeded (subscription login OK)"
            return cap
        cap.execution_verified = False
        if _AUTH_ERROR_PATTERNS.search(output):
            cap.level = INSTALLED_NOT_AUTHENTICATED
            cap.authenticated = False
            cap.probe_detail = (
                "CLI requires login - run 'claude login' and use your Claude subscription "
                "(no API key is read or configured by DRIFTLINE)"
            )
        else:
            cap.level = INSTALLED_UNVERIFIED
            cap.authenticated = None
            cap.probe_detail = (
                f"probe invocation failed (exit {proc.returncode}): {redact(output.strip()[-300:])}"
            )
        cap.errors.append(cap.probe_detail)
        return cap

    # -- task execution ---------------------------------------------------

    def build_task_argv(self, objective: str) -> List[str]:
        """Assemble the headless invocation. Prompt is always the last element."""
        exe = self.resolve()
        if not exe:
            raise ClaudeCodeError("Claude Code CLI is not installed")
        argv: List[str] = [exe, "--output-format", "json"]
        mode = (self.config.permission_mode or "").strip()
        if mode:
            if mode in {"bypassPermissions", "dangerously-skip-permissions"}:
                raise ClaudeCodeError(
                    f"permission_mode {mode!r} is not allowed; use a non-bypassing mode"
                )
            argv += ["--permission-mode", mode]
        if (self.config.model or "").strip():
            argv += ["--model", self.config.model.strip()]
        if (self.config.extra_args or "").strip():
            argv += shlex.split(self.config.extra_args, posix=False)
        argv += ["-p", objective]
        return argv

    def run_task(
        self,
        objective: str,
        workdir: str | Path,
        *,
        timeout: Optional[float] = None,
    ) -> ClaudeRunResult:
        """Invoke Claude Code for real; capture output, exit status and changes."""
        workdir = Path(workdir)
        objective = (objective or "").strip()
        result = ClaudeRunResult(objective=objective, workdir=str(workdir))
        if not objective:
            result.blocked_reason = "objective must not be empty"
            return result
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.blocked_reason = f"cannot create workdir: {redact(exc)}"
            return result
        try:
            argv = self.build_task_argv(objective)
        except ClaudeCodeError as exc:
            result.blocked_reason = str(exc)
            return result
        ok, reason = Policy.validate_agent_command(argv)
        if not ok:
            result.blocked_reason = f"policy refused the invocation: {reason}"
            return result
        result.argv = [redact(a) for a in argv]

        before = self._snapshot(workdir)
        started = time.monotonic()
        try:
            proc = self.runner(
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout or self.config.timeout,
                shell=False,
            )
            result.returncode = proc.returncode
            result.stdout = proc.stdout or ""
            result.stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            result.timeout = True
            result.returncode = -1
            result.stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            result.stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            result.blocked_reason = f"claude run timed out after {timeout or self.config.timeout}s"
        except Exception as exc:  # noqa: BLE001
            result.returncode = -1
            result.blocked_reason = f"claude run failed to start: {redact(exc)}"
        result.duration_s = time.monotonic() - started

        after = self._snapshot(workdir)
        result.created_files = sorted(set(after) - set(before))
        result.modified_files = sorted(
            name for name in set(before) & set(after) if before[name] != after[name]
        )

        parsed = self._parse_json(result.stdout)
        if parsed:
            result.raw = parsed
            result.result_text = str(parsed.get("result") or "")
            result.is_error = bool(parsed.get("is_error"))
            result.session_id = str(parsed.get("session_id") or "")
            turns = parsed.get("num_turns")
            result.num_turns = int(turns) if isinstance(turns, int) else None
        else:
            result.result_text = result.stdout.strip()
            result.is_error = result.returncode != 0
        result.log_path = self._write_log(result)
        return result

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _parse_json(stdout: str) -> Dict[str, Any]:
        text = (stdout or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _snapshot(workdir: Path) -> Dict[str, tuple]:
        snap: Dict[str, tuple] = {}
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(workdir)).replace("\\", "/")
            if rel.startswith(".git/"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snap[rel] = (stat.st_size, stat.st_mtime_ns)
        return snap

    def _write_log(self, result: ClaudeRunResult) -> str:
        try:
            log_dir = self.base_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            path = log_dir / f"claude-run-{stamp}-{uuid.uuid4().hex[:6]}.log"
            lines = [
                f"objective: {result.objective}",
                f"workdir:   {result.workdir}",
                f"argv:      {' '.join(redact(a) for a in result.argv)}",
                f"exit:      {result.returncode}  duration: {result.duration_s:.1f}s  "
                f"timeout: {result.timeout}  is_error: {result.is_error}",
                f"session:   {result.session_id}  turns: {result.num_turns}",
                f"created:   {result.created_files}",
                f"modified:  {result.modified_files}",
                "",
                "--- result text ---",
                result.result_text,
                "",
                "--- stdout ---",
                result.stdout,
                "",
                "--- stderr ---",
                result.stderr,
            ]
            path.write_text("\n".join(lines), encoding="utf-8")
            return str(path)
        except OSError:
            return ""
