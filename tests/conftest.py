"""Shared pytest fixtures.

Every external service is replaced by a local stub. No test contacts a real
Arena, Ollama, Qwen or OmniRoute instance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the project root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.config import load_config  # noqa: E402
from orchestrator.policy import Policy  # noqa: E402
from orchestrator.storage.database import Database  # noqa: E402

from tests.stubs.http_servers import OllamaStub, QwenStub  # noqa: E402
from tests.stubs.mcp_server import McpStubState, StubMcpServer  # noqa: E402


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("config.yaml").write_text(
        (ROOT / "config" / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def config(base_dir: Path):
    return load_config(base_dir=base_dir)


@pytest.fixture
def db(base_dir: Path) -> Database:
    database = Database(base_dir / "data" / "test.db")
    yield database
    database.close()


@pytest.fixture
def policy() -> Policy:
    return Policy()


@pytest.fixture
def stub_mcp():
    with StubMcpServer() as server:
        yield server


@pytest.fixture
def stub_mcp_state():
    return McpStubState()


@pytest.fixture
def ollama_stub():
    with OllamaStub() as stub:
        yield stub


@pytest.fixture
def qwen_stub():
    with QwenStub() as stub:
        yield stub
