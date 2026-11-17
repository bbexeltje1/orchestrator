"""Packaging tests: build the ZIP, verify exclusions, validate a fresh extract."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_packager():
    """Load packaging/build_zip.py by path to avoid shadowing PyPI `packaging`."""
    spec = importlib.util.spec_from_file_location(
        "driftline_build_zip", ROOT / "packaging" / "build_zip.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_packager = _load_packager()
build = _packager.build
verify = _packager.verify


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_zip_builds_and_is_clean(tmp_path):
    artifact = build(dist_dir=tmp_path)
    assert artifact.exists()
    assert artifact.name.startswith("DRIFTLINE-Orchestrator-")
    problems = verify(artifact)
    assert problems == []


def test_zip_excludes_secret_and_runtime_files(tmp_path):
    artifact = build(dist_dir=tmp_path)
    with zipfile.ZipFile(artifact) as zf:
        names = zf.namelist()
    lowered = [n.lower() for n in names]
    for forbidden in (
        ".venv/",
        "__pycache__/",
        ".git/",
        "data/",
        "logs/",
        "run/",
        ".db",
        ".gguf",
        ".exe",
        ".msi",
    ):
        assert not any(forbidden in n for n in lowered), (forbidden, names)
    # A real .env file must never ship, but .env.example is intentional.
    assert ".env.example" in names
    assert not any("/.env" in n or n == ".env" or n.startswith(".env/") for n in lowered)


def test_zip_sha256_matches_manifest(tmp_path):
    artifact = build(dist_dir=tmp_path)
    sums = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    expected = _sha256(artifact)
    assert expected in sums


def test_extracted_package_passes_offline_tests(tmp_path):
    artifact = build(dist_dir=tmp_path)
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(artifact) as zf:
        zf.extractall(extract_dir)

    # Required source files exist in the fresh extract.
    for required in ("start.ps1", "orchestrator/cli.py", "config/config.yaml", "tests/conftest.py"):
        assert (extract_dir / required).exists(), required

    # Run the offline suite from the extract only, with no source workspace on
    # the import path.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--disable-warnings",
            "-p",
            "no:cacheprovider",
            "--ignore=tests/test_packaging.py",
        ],
        cwd=str(extract_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
