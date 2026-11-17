"""Model discovery tests: find candidates, never choose one."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.cli import ConsoleSession
from orchestrator.model_discovery import (
    discover_gguf_models,
    human_size,
)


def _touch(path: Path, size: int = 1024, mtime: int = 1000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    import os

    os.utime(path, (mtime, mtime))


def test_discover_finds_gguf_newest_first(tmp_path):
    _touch(tmp_path / "models" / "old.gguf", size=100, mtime=1000)
    _touch(tmp_path / "models" / "new.gguf", size=200, mtime=2000)
    _touch(tmp_path / "models" / "notes.txt", mtime=3000)
    models = discover_gguf_models([tmp_path / "models"])
    assert [Path(m["path"]).name for m in models] == ["new.gguf", "old.gguf"]
    assert models[0]["size_bytes"] == 200


def test_discover_skips_hidden_and_known_dirs(tmp_path):
    _touch(tmp_path / "models" / ".hidden" / "secret.gguf")
    _touch(tmp_path / "models" / "node_modules" / "pkg.gguf")
    _touch(tmp_path / "models" / "sub" / "ok.gguf")
    models = discover_gguf_models([tmp_path / "models"])
    assert [Path(m["path"]).name for m in models] == ["ok.gguf"]


def test_discover_caps_results(tmp_path):
    for i in range(12):
        _touch(tmp_path / "models" / f"m{i:02d}.gguf", mtime=1000 + i)
    models = discover_gguf_models([tmp_path / "models"], max_results=3)
    assert len(models) == 3
    assert Path(models[0]["path"]).name == "m11.gguf"


def test_discover_empty_and_missing_roots(tmp_path):
    assert discover_gguf_models([tmp_path / "nope"]) == []
    (tmp_path / "empty").mkdir()
    assert discover_gguf_models([tmp_path / "empty"]) == []


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(5 * 1024**3).endswith("GB")


def test_qwen_models_cli_json(base_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    _touch(tmp_path / "models" / "qwen3-8b.gguf", size=4096)
    out, done = ConsoleSession(base_dir=base_dir).execute("qwen-models --json")
    assert done is False
    payload = json.loads(out.strip().splitlines()[-1])
    assert "llama_server" in payload
    assert len(payload["models"]) == 1
    assert payload["models"][0]["path"].endswith("qwen3-8b.gguf")


def test_qwen_models_cli_human_output(base_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    _touch(tmp_path / "models" / "qwen3-8b.gguf", size=4096)
    out, _ = ConsoleSession(base_dir=base_dir).execute("qwen-models")
    assert "llama-server:" in out
    assert "Candidate models" in out
    assert "qwen3-8b.gguf" in out
