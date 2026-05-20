"""next_tick.json: when the daemon should fire next. Claude can write it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import SETTINGS


@dataclass
class NextTick:
    wake_at: datetime
    reason: str
    set_at: str  # iso timestamp this entry was written
    set_by: str  # 'claude' / 'operator' / 'daemon-default'


def read() -> NextTick | None:
    p = SETTINGS.next_tick_json
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return NextTick(
            wake_at=datetime.fromisoformat(data["wake_at"].replace("Z", "+00:00")),
            reason=str(data.get("reason", "scheduled")),
            set_at=str(data.get("set_at", "")),
            set_by=str(data.get("set_by", "unknown")),
        )
    except Exception:
        return None


def write(when: datetime, reason: str, set_by: str) -> NextTick:
    nt = NextTick(
        wake_at=when,
        reason=reason,
        set_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        set_by=set_by,
    )
    SETTINGS.next_tick_json.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.next_tick_json.write_text(
        json.dumps(
            {
                "wake_at": nt.wake_at.isoformat(timespec="seconds"),
                "reason": nt.reason,
                "set_at": nt.set_at,
                "set_by": nt.set_by,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return nt


def schedule_default() -> NextTick:
    when = datetime.now(timezone.utc) + timedelta(minutes=SETTINGS.default_next_tick_minutes)
    return write(when, reason="scheduled", set_by="daemon-default")


def due(now: datetime | None = None) -> bool:
    nt = read()
    if nt is None:
        return True
    cur = now or datetime.now(timezone.utc)
    return cur >= nt.wake_at
