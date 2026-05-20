"""Rotating-file + console logger for the graffito daemon."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

from .config import SETTINGS, ensure_dirs

_configured = False


def setup(level: int = logging.INFO, quiet_console: bool = False) -> logging.Logger:
    global _configured
    logger = logging.getLogger("graffito")
    if _configured:
        return logger
    ensure_dirs()
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    if not quiet_console:
        rich = RichHandler(rich_tracebacks=True, show_path=False, show_time=False, show_level=True)
        rich.setFormatter(fmt)
        logger.addHandler(rich)

    fh = RotatingFileHandler(
        SETTINGS.daemon_log, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.propagate = False
    _configured = True
    return logger
