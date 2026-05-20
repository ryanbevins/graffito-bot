"""SQLite schema + helpers. Mirrors trader/db.py."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import SETTINGS, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_code INTEGER,
    summary TEXT,
    log_path TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER REFERENCES ticks(id),
    tu TEXT NOT NULL,
    symbol TEXT,
    before_pct REAL,
    after_pct REAL,
    outcome TEXT CHECK (outcome IN ('improved','matched','regressed','no_change','errored')) NOT NULL,
    commit_sha TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS progress_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    fuzzy_match_pct REAL NOT NULL,
    matched_code INTEGER,
    total_code INTEGER,
    matched_functions INTEGER,
    total_functions INTEGER,
    complete_units INTEGER,
    total_units INTEGER,
    commit_sha TEXT,
    source TEXT NOT NULL CHECK (source IN ('pre_tick','post_tick','periodic','boot'))
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    pushed_at TEXT NOT NULL,
    message TEXT NOT NULL,
    tick_id INTEGER REFERENCES ticks(id),
    before_pct REAL,
    after_pct REAL
);

CREATE INDEX IF NOT EXISTS idx_progress_recorded ON progress_snapshots(recorded_at);
CREATE INDEX IF NOT EXISTS idx_attempts_tick ON attempts(tick_id);
CREATE INDEX IF NOT EXISTS idx_commits_pushed ON commits(pushed_at);
CREATE INDEX IF NOT EXISTS idx_ticks_started ON ticks(started_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    db_path = path or SETTINGS.db_path
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    ensure_dirs()
    if reset and SETTINGS.db_path.exists():
        SETTINGS.db_path.unlink()
    with connect() as c:
        c.executescript(SCHEMA)


# ── Tick helpers ──────────────────────────────────────────────────────────


def insert_tick(reason: str, started_at: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO ticks (reason, started_at) VALUES (?, ?)",
            (reason, started_at),
        )
        return cur.lastrowid


def finish_tick(tick_id: int, ended_at: str, exit_code: int, summary: str, log_path: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE ticks SET ended_at = ?, exit_code = ?, summary = ?, log_path = ? WHERE id = ?",
            (ended_at, exit_code, summary, log_path, tick_id),
        )


def recent_ticks(limit: int = 25) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM ticks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def last_tick() -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM ticks ORDER BY id DESC LIMIT 1").fetchone()


# ── Snapshot helpers ──────────────────────────────────────────────────────


def insert_snapshot(
    fuzzy_match_pct: float,
    matched_code: int | None,
    total_code: int | None,
    matched_functions: int | None,
    total_functions: int | None,
    complete_units: int | None,
    total_units: int | None,
    commit_sha: str | None,
    source: str,
) -> int:
    with connect() as c:
        cur = c.execute(
            """INSERT INTO progress_snapshots
               (recorded_at, fuzzy_match_pct, matched_code, total_code,
                matched_functions, total_functions, complete_units, total_units,
                commit_sha, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                now_iso(),
                fuzzy_match_pct,
                matched_code,
                total_code,
                matched_functions,
                total_functions,
                complete_units,
                total_units,
                commit_sha,
                source,
            ),
        )
        return cur.lastrowid


def snapshot_series(since_iso: str | None = None) -> list[sqlite3.Row]:
    with connect() as c:
        if since_iso:
            return c.execute(
                "SELECT * FROM progress_snapshots WHERE recorded_at >= ? ORDER BY recorded_at",
                (since_iso,),
            ).fetchall()
        return c.execute(
            "SELECT * FROM progress_snapshots ORDER BY recorded_at"
        ).fetchall()


def latest_snapshot() -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(
            "SELECT * FROM progress_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()


# ── Attempt helpers ───────────────────────────────────────────────────────


def insert_attempt(
    tick_id: int,
    tu: str,
    symbol: str | None,
    before_pct: float | None,
    after_pct: float | None,
    outcome: str,
    commit_sha: str | None,
) -> int:
    with connect() as c:
        cur = c.execute(
            """INSERT INTO attempts (tick_id, tu, symbol, before_pct, after_pct,
                                     outcome, commit_sha, recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (tick_id, tu, symbol, before_pct, after_pct, outcome, commit_sha, now_iso()),
        )
        return cur.lastrowid


def recent_attempts(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ── Commit helpers ────────────────────────────────────────────────────────


def insert_commit(sha: str, message: str, tick_id: int | None, before_pct: float | None, after_pct: float | None) -> None:
    with connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO commits (sha, pushed_at, message, tick_id, before_pct, after_pct)
               VALUES (?,?,?,?,?,?)""",
            (sha, now_iso(), message, tick_id, before_pct, after_pct),
        )


def recent_commits(limit: int = 25) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM commits ORDER BY pushed_at DESC LIMIT ?", (limit,)
        ).fetchall()


def commits_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM commits WHERE pushed_at LIKE ?", (f"{today}%",)
        ).fetchone()
        return int(row["n"])
