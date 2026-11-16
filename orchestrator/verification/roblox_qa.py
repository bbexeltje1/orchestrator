"""Roblox-specific QA support.

Hard rules enforced here:

* Studio design-time objects are the source of truth for visuals and layout.
* The orchestrator never opens Play mode or mutates Studio state during
  installation, startup, or routine verification. Studio testing is an explicit
  QA operation that a human initiates.
* Runtime scripts only handle behaviour.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import ServiceHealth
from ..policy import Policy


@dataclass
class RobloxQaReport:
    rojo_available: bool = False
    project_file: Optional[str] = None
    lune_available: bool = False
    studio_detected: bool = False
    studio_state_changed: bool = False
    diagnostics: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    health: ServiceHealth = ServiceHealth.NOT_CONFIGURED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rojo_available": self.rojo_available,
            "project_file": self.project_file,
            "lune_available": self.lune_available,
            "studio_detected": self.studio_detected,
            "studio_state_changed": self.studio_state_changed,
            "diagnostics": self.diagnostics,
            "evidence": self.evidence,
            "health": self.health.value,
        }


class RobloxQa:
    def __init__(self, repo_path: str | Path, *, policy: Optional[Policy] = None) -> None:
        self.repo_path = Path(repo_path)
        self.policy = policy or Policy()

    def _find_project_file(self) -> Optional[Path]:
        for name in ("default.project.json", "driftline.project.json"):
            candidate = self.repo_path / name
            if candidate.exists():
                return candidate
        return None

    def _detect_studio(self) -> bool:
        """Best-effort Studio detection without touching Studio state."""
        if platform.system() == "Windows":
            local = os.environ.get("LOCALAPPDATA", "")
            if local and (Path(local) / "Roblox").exists():
                return True
        return False

    def diagnostics(self, *, explicit_qa: bool = False) -> RobloxQaReport:
        report = RobloxQaReport()
        report.rojo_available = shutil.which("rojo") is not None
        project = self._find_project_file()
        report.project_file = str(project) if project else None
        report.lune_available = shutil.which("lune") is not None
        report.studio_detected = self._detect_studio()

        if report.project_file is None:
            report.diagnostics.append(
                "no Rojo project file (default.project.json) found at repository root"
            )
        if not report.rojo_available:
            report.diagnostics.append(
                "rojo is not on PATH; Rojo build checks are unavailable"
            )
        report.diagnostics.append(
            "Studio design-time objects remain the source of truth for visuals; "
            "no runtime visual replacement is performed"
        )
        if not explicit_qa:
            report.diagnostics.append(
                "Studio testing was not requested; Play mode was not opened and "
                "Studio state was not modified"
            )
            report.health = ServiceHealth.NOT_CONFIGURED
        else:
            report.health = ServiceHealth.SKIPPED if not report.rojo_available else ServiceHealth.PASS
        return report

    def collect_runtime_errors(self, log_path: Optional[str | Path]) -> List[Dict[str, Any]]:
        """Read a provided runtime log and extract error lines.

        The orchestrator never connects to a live Studio session on its own.
        """
        errors: List[Dict[str, Any]] = []
        if not log_path:
            return errors
        path = Path(log_path)
        if not path.exists():
            return errors
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            low = line.lower()
            if "error" in low or "stack begin" in low or "exception" in low:
                errors.append({"line": line[:500]})
        return errors[:200]

    def attach_evidence(self, *paths: str | Path) -> List[str]:
        """Record screenshot/log references supplied by a human or a QA run."""
        found: List[str] = []
        for p in paths:
            path = Path(p)
            if path.exists():
                found.append(str(path))
        return found
