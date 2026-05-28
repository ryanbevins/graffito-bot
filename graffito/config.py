"""Settings + path layout for graffito-bot. Mirrors trader/config.py."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(os.getenv("GRAFFITO_ROOT", Path(__file__).resolve().parent.parent))
load_dotenv(ROOT / ".env")


class Settings(BaseModel):
    root: Path = ROOT
    state_dir: Path = ROOT / "state"
    data_dir: Path = ROOT / "data"
    logs_dir: Path = ROOT / "logs"
    prompts_dir: Path = ROOT / "prompts"
    repo_dir: Path = Path(os.getenv("GRAFFITO_REPO", ROOT / "repo"))

    db_path: Path = ROOT / "data" / "bot.db"
    journal_dir: Path = ROOT / "state" / "journal"
    notes_dir: Path = ROOT / "state" / "notes"
    memory_dir: Path = ROOT / "state" / "memory"

    # Refreshed by the daemon every tick (read-only for Claude)
    progress_md: Path = ROOT / "state" / "progress.md"
    git_status_md: Path = ROOT / "state" / "git_status.md"
    last_tick_md: Path = ROOT / "state" / "last_tick.md"
    tick_reason_md: Path = ROOT / "state" / "tick_reason.md"
    tick_focus_md: Path = ROOT / "state" / "tick_focus.md"

    # Edited by Claude (persistent)
    goals_file: Path = ROOT / "state" / "goals.md"
    next_tick_json: Path = ROOT / "state" / "next_tick.json"
    campaign_tu_md: Path = ROOT / "state" / "campaign_tu.md"

    # Daemon mutexes / flags
    tick_lock: Path = ROOT / "state" / ".tick.lock"
    daemon_pid: Path = ROOT / "state" / ".daemon.pid"
    paused_flag: Path = ROOT / "state" / ".paused"
    regression_block: Path = ROOT / "state" / ".regression_block"

    daemon_log: Path = ROOT / "logs" / "daemon.log"
    tick_log_dir: Path = ROOT / "logs" / "ticks"

    # Build artifacts inside the cloned repo
    report_json: Path = Path(os.getenv("GRAFFITO_REPO", ROOT / "repo")) / "build" / "GMSJ01" / "report.json"

    # Claude
    claude_bin: str = os.getenv("CLAUDE_BIN", "claude")
    claude_model: str = os.getenv("CLAUDE_MODEL", "opus")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Daemon tuning (defaults from the plan)
    heartbeat_seconds: int = int(os.getenv("HEARTBEAT_SECONDS", "60"))
    default_next_tick_minutes: int = int(os.getenv("DEFAULT_NEXT_TICK_MINUTES", "30"))
    liveness_watchdog_seconds: int = int(os.getenv("LIVENESS_WATCHDOG_SECONDS", "300"))
    periodic_snapshot_seconds: int = int(os.getenv("PERIODIC_SNAPSHOT_SECONDS", "3600"))
    disk_guard_min_gb: float = float(os.getenv("DISK_GUARD_MIN_GB", "5"))
    daily_commit_cap: int = int(os.getenv("DAILY_COMMIT_CAP", "100"))
    regression_threshold_pct: float = float(os.getenv("REGRESSION_THRESHOLD_PCT", "0.5"))

    # Git
    git_remote: str = os.getenv("GIT_REMOTE", "origin")
    git_branch: str = os.getenv("GIT_BRANCH", "main")
    github_repo: str = os.getenv("GITHUB_REPO", "ryanbevins/graffito")

    # Dashboard
    dashboard_token: str = os.getenv("DASHBOARD_TOKEN", "")
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8081"))


SETTINGS = Settings()


def ensure_dirs() -> None:
    for p in (
        SETTINGS.state_dir,
        SETTINGS.data_dir,
        SETTINGS.logs_dir,
        SETTINGS.journal_dir,
        SETTINGS.notes_dir,
        SETTINGS.memory_dir,
        SETTINGS.tick_log_dir,
        SETTINGS.prompts_dir,
    ):
        p.mkdir(parents=True, exist_ok=True)
