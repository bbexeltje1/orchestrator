"""Configuration loading, validation and secret handling.

Config comes from ``config/config.yaml`` with optional overrides from the
process environment and from a local ``.env`` file (never packaged).

Secrets are *never* written to YAML or logged. Anything that looks like a
credential belongs in ``.env`` and is referenced by name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - import guard for docs builds
    yaml = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "project": {
        "name": "DRIFTLINE",
        "repository": "auto",
        "default_branch": "auto",
        "workspace_root": ".",
    },
    "qwen": {
        "enabled": True,
        "provider": "llama_cpp",
        "base_url": "http://127.0.0.1:8090/v1",
        "model": "auto",
        "timeout": 60.0,
        "max_tokens": 4096,
    },
    "omniroute": {
        "enabled": False,
        "base_url": "http://127.0.0.1:20128",
        "health_path": "/health",
        "api_key_env": "OMNIROUTE_API_KEY",
    },
    "thx": {
        "enabled": True,
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "HAL-X/thx-02:q4_k_m",
        "decision_endpoint": "/v1/systemone",
        "timeout": 60.0,
        "min_confidence": 0.55,
        "allow_fallback_chat": True,
    },
    "arena": {
        "enabled": True,
        "transport": "auto",
        "endpoint": "auto",
        "stdio_command": "",
        "task_tool": "auto",
        "status_tool": "auto",
        "result_tool": "auto",
        "token_env": "ARENA_BRIDGE_TOKEN",
        "request_timeout": 30.0,
        "poll_interval": 10.0,
        "poll_max": 120,
        "backoff_base": 2.0,
    },
    "workflow": {
        "autonomy": "hybrid",
        "max_parallel_tasks": 1,
        "max_retries": 2,
        "dry_run": True,
        "require_approval_for_merge": True,
        "require_approval_for_publish": True,
        "safe_idle_on_start": True,
        "poll_interval": 10.0,
    },
    "verification": {
        "run_git_checks": True,
        "run_tests": True,
        "run_rojo_build": False,
        "luau_lsp_command": "",
        "roblox_studio_diagnostics": False,
    },
    "logging": {
        "level": "INFO",
        "log_file": "logs/orchestrator.log",
        "redact_secrets": True,
        "max_bytes": 5242880,
        "backup_count": 3,
    },
    "storage": {
        "database": "data/orchestrator.db",
    },
    "services": {
        "auto_start": True,
        "health_wait_seconds": 60,
        "ollama_start_command": "",
        "qwen_start_command": "",
        "omniroute_start_command": "",
        "arena_start_command": "",
    },
    "server": {
        "bind_host": "127.0.0.1",
        "port": 17870,
        "enabled": False,
    },
}


@dataclass
class ProjectConfig:
    name: str = "DRIFTLINE"
    repository: str = "auto"
    default_branch: str = "auto"
    workspace_root: str = "."


@dataclass
class QwenConfig:
    enabled: bool = True
    provider: str = "llama_cpp"
    base_url: str = "http://127.0.0.1:8090/v1"
    model: str = "auto"
    timeout: float = 60.0
    max_tokens: int = 4096


@dataclass
class OmniRouteConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:20128"
    health_path: str = "/health"
    api_key_env: str = "OMNIROUTE_API_KEY"


@dataclass
class ThxConfig:
    enabled: bool = True
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "HAL-X/thx-02:q4_k_m"
    decision_endpoint: str = "/v1/systemone"
    timeout: float = 60.0
    min_confidence: float = 0.55
    allow_fallback_chat: bool = True


@dataclass
class ArenaConfig:
    enabled: bool = True
    transport: str = "auto"
    endpoint: str = "auto"
    stdio_command: str = ""
    task_tool: str = "auto"
    status_tool: str = "auto"
    result_tool: str = "auto"
    token_env: str = "ARENA_BRIDGE_TOKEN"
    request_timeout: float = 30.0
    poll_interval: float = 10.0
    poll_max: int = 120
    backoff_base: float = 2.0


@dataclass
class WorkflowConfig:
    autonomy: str = "hybrid"
    max_parallel_tasks: int = 1
    max_retries: int = 2
    dry_run: bool = True
    require_approval_for_merge: bool = True
    require_approval_for_publish: bool = True
    safe_idle_on_start: bool = True
    poll_interval: float = 10.0


@dataclass
class VerificationConfig:
    run_git_checks: bool = True
    run_tests: bool = True
    run_rojo_build: bool = False
    luau_lsp_command: str = ""
    roblox_studio_diagnostics: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "logs/orchestrator.log"
    redact_secrets: bool = True
    max_bytes: int = 5242880
    backup_count: int = 3


@dataclass
class StorageConfig:
    database: str = "data/orchestrator.db"


@dataclass
class ServicesConfig:
    """Background service management used by the one-command launcher.

    The launcher (start.ps1) reads this section to decide whether it may start
    local services itself, so nothing needs a separate terminal. Python never
    starts services directly; commands stay explicit and user-controlled. An
    empty start command means "never auto-start this service".
    """

    auto_start: bool = True
    health_wait_seconds: int = 60
    ollama_start_command: str = ""
    qwen_start_command: str = ""
    omniroute_start_command: str = ""
    arena_start_command: str = ""


@dataclass
class ServerConfig:
    bind_host: str = "127.0.0.1"
    port: int = 17870
    enabled: bool = False


@dataclass
class Config:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    qwen: QwenConfig = field(default_factory=QwenConfig)
    omniroute: OmniRouteConfig = field(default_factory=OmniRouteConfig)
    thx: ThxConfig = field(default_factory=ThxConfig)
    arena: ArenaConfig = field(default_factory=ArenaConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project.__dict__,
            "qwen": self.qwen.__dict__,
            "omniroute": self.omniroute.__dict__,
            "thx": self.thx.__dict__,
            "arena": self.arena.__dict__,
            "workflow": self.workflow.__dict__,
            "verification": self.verification.__dict__,
            "logging": self.logging.__dict__,
            "storage": self.storage.__dict__,
            "services": self.services.__dict__,
            "server": self.server.__dict__,
        }


_SECTION_DTYPES = {
    "project": ProjectConfig,
    "qwen": QwenConfig,
    "omniroute": OmniRouteConfig,
    "thx": ThxConfig,
    "arena": ArenaConfig,
    "workflow": WorkflowConfig,
    "verification": VerificationConfig,
    "logging": LoggingConfig,
    "storage": StorageConfig,
    "services": ServicesConfig,
    "server": ServerConfig,
}

_SECRET_KEYS = {"api_key", "token", "password", "secret", "authorization"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _type_name(ftype: Any) -> str:
    """Resolve a dataclass field type, including string annotations."""
    if isinstance(ftype, str):
        return ftype
    return getattr(ftype, "__name__", "")


def _coerce(dtype: Any, key: str, value: Any) -> Any:
    """Coerce a YAML/env value to the dataclass field type, or raise ValueError."""
    if value is None:
        return None
    name = _type_name(dtype)
    if name in {"bool", "boolean"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if name in {"int", "integer"}:
        return int(value)
    if name in {"float"}:
        return float(value)
    if name in {"str", "string"}:
        return str(value)
    return value


def _build_section(dtype: type, raw: Dict[str, Any]) -> Any:
    valid = {f.name: f.type for f in fields(dtype)}
    kwargs: Dict[str, Any] = {}
    for name, ftype in valid.items():
        if name in raw:
            try:
                kwargs[name] = _coerce(ftype, name, raw[name])
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"config.{dtype.__name__}.{name} has invalid value {raw[name]!r}: {exc}"
                ) from exc
    return dtype(**kwargs)


class ConfigError(Exception):
    """Raised for invalid or incompatible configuration."""


def load_dotenv(path: Path) -> Dict[str, str]:
    """Minimal .env parser. Does not override existing environment variables."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            values[key] = val
    return values


def env_override(section: str, cfg: Any, env: Dict[str, str]) -> Any:
    """Apply ``DRIFTLINE_<SECTION>_<KEY>`` overrides to a config section."""
    prefix = f"DRIFTLINE_{section.upper()}_"
    for f in fields(cfg):
        key = prefix + f.name.upper()
        if key in env:
            try:
                setattr(cfg, f.name, _coerce(f.type, f.name, env[key]))
            except (TypeError, ValueError):
                # Non-fatal: report during validation instead.
                pass
    return cfg


def load_config(
    config_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    base_dir: Optional[Path] = None,
) -> Config:
    """Load configuration from YAML + .env + environment.

    Precedence (lowest to highest): defaults < config.yaml < .env < environment.
    """
    if yaml is None:
        raise ConfigError("PyYAML is required to load configuration")
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    config_path = Path(config_path) if config_path else base_dir / "config" / "config.yaml"

    raw = _deep_merge(DEFAULT_CONFIG, {})
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping")
        raw = _deep_merge(raw, loaded)

    env_map = dict(os.environ if env is None else env)
    dotenv = load_dotenv(base_dir / ".env")
    for key, value in dotenv.items():
        env_map.setdefault(key, value)

    cfg = Config()
    for section, dtype in _SECTION_DTYPES.items():
        section_raw = raw.get(section, {}) or {}
        if not isinstance(section_raw, dict):
            raise ConfigError(f"config section '{section}' must be a mapping")
        setattr(cfg, section, env_override(section, _build_section(dtype, section_raw), env_map))

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Validate configuration values and raise ConfigError on problems."""
    problems: List[str] = []

    if not cfg.project.name:
        problems.append("project.name must not be empty")

    for label, url in (
        ("qwen.base_url", cfg.qwen.base_url),
        ("omniroute.base_url", cfg.omniroute.base_url),
        ("thx.base_url", cfg.thx.base_url),
    ):
        if url and not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"{label} must be an http(s) URL, got {url!r}")

    if cfg.thx.min_confidence < 0.0 or cfg.thx.min_confidence > 1.0:
        problems.append("thx.min_confidence must be between 0.0 and 1.0")

    if cfg.thx.model and cfg.thx.model != "auto":
        if any(ch.isspace() for ch in cfg.thx.model):
            problems.append(f"thx.model looks invalid: {cfg.thx.model!r}")

    if cfg.workflow.max_parallel_tasks < 1:
        problems.append("workflow.max_parallel_tasks must be >= 1")
    if cfg.workflow.max_retries < 0:
        problems.append("workflow.max_retries must be >= 0")
    if cfg.workflow.autonomy not in {"hybrid", "manual", "autonomous"}:
        problems.append("workflow.autonomy must be one of hybrid|manual|autonomous")

    if cfg.services.health_wait_seconds < 1 or cfg.services.health_wait_seconds > 900:
        problems.append("services.health_wait_seconds must be between 1 and 900")

    if cfg.server.bind_host not in {"127.0.0.1", "localhost", "::1"}:
        problems.append(
            "server.bind_host must be a loopback address (127.0.0.1); "
            "the orchestrator must never be exposed publicly"
        )

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


def resolve_secret(cfg: Config, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve the Arena bridge token from the environment only."""
    env_map = dict(os.environ if env is None else env)
    name = cfg.arena.token_env
    return env_map.get(name) or None


def config_path_for(base_dir: Path) -> Path:
    return Path(base_dir) / "config" / "config.yaml"
