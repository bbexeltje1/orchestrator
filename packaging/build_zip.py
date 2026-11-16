"""Build the distributable ZIP for the DRIFTLINE Orchestrator.

The packager is strict about exclusions: virtual environments, downloaded
models, secrets, runtime databases, logs, caches and installers never ship.
Run from anywhere: ``python packaging/build_zip.py``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import __version__  # noqa: E402

ARTIFACT_NAME = f"DRIFTLINE-Orchestrator-v{__version__}.zip"
# NOTE: not "dist" - that directory name is excluded from workspace snapshots.
DIST_DIR = ROOT / "release"

EXCLUDE_DIR_NAMES = {
    ".venv",
    "venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "data",
    "logs",
    "dist",
    "build",
    "coverage",
    ".arena",
}

EXCLUDE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".db",
    ".db-wal",
    ".db-shm",
    ".gguf",
    ".exe",
    ".msi",
    ".dll",
    ".so",
    ".dylib",
    ".log",
}

EXCLUDE_FILE_NAMES = {".env", ".DS_Store", "Thumbs.db"}

REQUIRED_ENTRIES = [
    "TEST-REPORT.txt",
    "start.ps1",
    "stop.ps1",
    "doctor.ps1",
    "test.ps1",
    "README.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "LICENSES.md",
    "requirements.txt",
    "pyproject.toml",
    "config/config.yaml",
    "config/config.example.yaml",
    ".env.example",
    "orchestrator/main.py",
    "orchestrator/cli.py",
    "orchestrator/state_machine.py",
    "orchestrator/policy.py",
    "orchestrator/task_manager.py",
    "orchestrator/models.py",
    "orchestrator/engines/thx02.py",
    "orchestrator/engines/qwen.py",
    "orchestrator/agents/arena.py",
    "orchestrator/mcp/client.py",
    "orchestrator/storage/database.py",
    "orchestrator/verification/git.py",
    "orchestrator/verification/tests.py",
    "orchestrator/verification/roblox_qa.py",
    "tests/test_arena.py",
    "tests/test_task_manager.py",
    "tests/test_policy.py",
    "packaging/build_zip.py",
]


def _should_exclude(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    if path.suffix.lower() in EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def _iter_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if _should_exclude(relative):
            continue
        yield relative


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(dist_dir: Path | None = None) -> Path:
    dist = Path(dist_dir) if dist_dir else DIST_DIR
    dist.mkdir(parents=True, exist_ok=True)
    artifact = dist / ARTIFACT_NAME
    if artifact.exists():
        artifact.unlink()

    files = list(_iter_files())
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for relative in files:
            zf.write(ROOT / relative, arcname=relative.as_posix())
        # Manifest inside the ZIP too.
        manifest = "\n".join(relative.as_posix() for relative in files)
        zf.writestr("MANIFEST.txt", manifest + "\n")

    digest = sha256_of(artifact)
    (dist / "SHA256SUMS.txt").write_text(
        f"{digest}  {ARTIFACT_NAME}\n", encoding="utf-8"
    )
    (dist / "MANIFEST.txt").write_text(
        f"# {ARTIFACT_NAME}\n# SHA256 {digest}\n\n" + manifest + "\n", encoding="utf-8"
    )
    return artifact


def verify(artifact: Path) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(artifact) as zf:
        names = zf.namelist()
    lower = [n.lower() for n in names]
    for forbidden in (
        ".venv/",
        "__pycache__/",
        ".git/",
        "data/",
        "logs/",
        ".db",
        ".gguf",
        ".exe",
        ".msi",
    ):
        if any(forbidden in n for n in lower):
            problems.append(f"forbidden entry detected: {forbidden}")
    # Secrets: an exact .env file, but .env.example is intentional.
    for name in lower:
        parts = name.split("/")
        if ".env" in parts or name.endswith("/.env"):
            problems.append(f"forbidden secret entry detected: {name}")
    for required in REQUIRED_ENTRIES:
        if required not in names:
            problems.append(f"required entry missing: {required}")
    return problems


if __name__ == "__main__":
    artifact = build()
    problems = verify(artifact)
    print(f"built {artifact}")
    print(f"sha256 {sha256_of(artifact)}")
    if problems:
        print("VERIFY FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(f"verified {len(REQUIRED_ENTRIES)} required entries, no forbidden files")
