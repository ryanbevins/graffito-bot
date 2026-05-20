"""Rotating-file + console logger for the graffito daemon.

All log timestamps render in Manila time (Asia/Manila, UTC+8), 12-hour format.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

from .config import SETTINGS, ensure_dirs

_configured = False

# Manila is UTC+8 year-round (no DST). Try zoneinfo first; fall back to a
# fixed offset on systems without tzdata (e.g., minimal Windows installs).
try:
    from zoneinfo import ZoneInfo
    _MANILA = ZoneInfo("Asia/Manila")
except Exception:
    _MANILA = timezone(timedelta(hours=8), name="MNL")


class ManilaFormatter(logging.Formatter):
    """Render %(asctime)s as 'YYYY-MM-DD hh:mmAM/PM MNL'."""

    def formatTime(self, record, datefmt=None):  # noqa: ARG002
        dt = datetime.fromtimestamp(record.created, tz=_MANILA)
        # 12-hour, no zero-pad on hour, am/pm lowercase
        h = dt.hour % 12 or 12
        ampm = "am" if dt.hour < 12 else "pm"
        return f"{dt:%Y-%m-%d} {h}:{dt:%M}{ampm} MNL"


def setup(level: int = logging.INFO, quiet_console: bool = False) -> logging.Logger:
    global _configured
    logger = logging.getLogger("graffito")
    if _configured:
        return logger
    ensure_dirs()
    logger.setLevel(level)

    fmt = ManilaFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")

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
