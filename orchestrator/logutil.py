"""Logging with secret redaction and task-id context."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Iterable, Optional

from .models import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return redact(message)


def configure_logging(
    log_file: str | Path,
    *,
    level: str = "INFO",
    max_bytes: int = 5242880,
    backup_count: int = 3,
    redact: bool = True,
) -> logging.Logger:
    logger = logging.getLogger("driftline")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
    formatter = RedactingFormatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S%z")

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


def set_task_context(logger: logging.Logger, task_id: str) -> logging.Logger:
    """Return a child logger that tags messages with a task id."""
    return logging.LoggerAdapter(logger, {"task_id": task_id})  # type: ignore[return-value]
