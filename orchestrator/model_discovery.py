"""Discovery of local GGUF models for Qwen startup configuration.

The launcher can never know which model file a user means, and it must not
guess. This module *discovers* candidate ``.gguf`` files on the machine and
presents them; the user chooses exactly one and puts the matching
``services.qwen_start_command`` line into ``config/config.yaml``. Nothing is
ever started or downloaded here.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_ROOT_NAMES = ("models", "Models", "Downloads", "Desktop")

SKIP_DIR_NAMES = {
    "appdata",
    "windows",
    "program files",
    "program files (x86)",
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "$recycle.bin",
    "system volume information",
    "dist",
    "build",
    "release",
    "run",
    "logs",
    "data",
    "coverage",
}

MAX_MODELS = 8
MAX_VISITED = 20000


def human_size(num: int) -> str:
    """Human-readable file size (``5.2 GB``)."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def find_llama_server() -> Optional[str]:
    """Locate ``llama-server`` on PATH, if present. Never executed here."""
    for name in ("llama-server", "llama-server.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def default_search_roots() -> List[Path]:
    """Sensible locations only: user model folders and the server's folder."""
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    roots = [home / name for name in DEFAULT_ROOT_NAMES]
    server = find_llama_server()
    if server:
        roots.append(Path(server).resolve().parent)
    return roots


def discover_gguf_models(
    roots: Optional[Iterable[Path]] = None,
    *,
    max_results: int = MAX_MODELS,
    max_visited: int = MAX_VISITED,
) -> List[Dict[str, Any]]:
    """Return candidate ``.gguf`` models (newest first).

    Discovery only: candidates are listed for the user to choose from. A
    model is never selected automatically, never modified and never run.
    """
    search_roots = [Path(r) for r in roots] if roots is not None else default_search_roots()
    found: Dict[str, Dict[str, Any]] = {}
    visited = 0

    def scan(directory: Path, depth: int) -> None:
        nonlocal visited
        if depth < 0 or visited >= max_visited:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if visited >= max_visited:
                return
            visited += 1
            name = entry.name.lower()
            if name.startswith(".") or name in SKIP_DIR_NAMES:
                continue
            try:
                if entry.is_file():
                    if entry.suffix.lower() == ".gguf":
                        key = str(entry).lower()
                        if key not in found:
                            stat = entry.stat()
                            found[key] = {
                                "path": str(entry),
                                "size_bytes": stat.st_size,
                                "size": human_size(stat.st_size),
                                "modified": int(stat.st_mtime),
                            }
                elif entry.is_dir():
                    scan(entry, depth - 1)
            except OSError:
                continue

    for root in search_roots:
        if root and root.is_dir():
            scan(root, 1)
    results = sorted(found.values(), key=lambda m: m["modified"], reverse=True)
    return results[:max_results]
