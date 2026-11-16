"""Application bootstrap: wire config, storage, engines and adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .agents.arena import ArenaAdapter
from .config import Config, load_config
from .engines.qwen import QwenEngine
from .engines.thx02 import Thx02Engine
from .logutil import configure_logging
from .policy import Policy
from .storage.database import Database
from .task_manager import TaskManager
from .workflow import Workflow


class App:
    def __init__(self, config: Config, base_dir: Path) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.logger = configure_logging(
            self.base_dir / config.logging.log_file,
            level=config.logging.level,
            max_bytes=config.logging.max_bytes,
            backup_count=config.logging.backup_count,
            redact=config.logging.redact_secrets,
        )
        self.db = Database(self.base_dir / config.storage.database)
        self.policy = Policy(
            autonomy=config.workflow.autonomy,
            dry_run=config.workflow.dry_run,
            require_approval_for_merge=config.workflow.require_approval_for_merge,
            require_approval_for_publish=config.workflow.require_approval_for_publish,
        )
        self.thx = Thx02Engine(config.thx, logger=self.logger) if config.thx.enabled else None
        self.qwen = (
            QwenEngine(config.qwen, config.omniroute, logger=self.logger)
            if config.qwen.enabled
            else None
        )
        self.arena = ArenaAdapter(config.arena, base_dir=str(self.base_dir))
        self.tasks = TaskManager(self.db, config, arena=self.arena, policy=self.policy, logger=self.logger)
        self.workflow = Workflow(
            self.db,
            config,
            arena=self.arena,
            qwen=self.qwen,
            thx=self.thx,
            task_manager=self.tasks,
            policy=self.policy,
            logger=self.logger,
        )

    def close(self) -> None:
        self.db.close()


def bootstrap(base_dir: Optional[str | Path] = None, config_path: Optional[str | Path] = None) -> App:
    base = Path(base_dir) if base_dir else Path.cwd()
    cfg = load_config(config_path=config_path, base_dir=base)
    return App(cfg, base)
