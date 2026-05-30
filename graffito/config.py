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

    # Operator-controlled runtime settings (changeable via dashboard)
    active_agent_file: Path = ROOT / "state" / "active_agent.md"
    tick_interval_json: Path = ROOT / "state" / "tick_interval.json"

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
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Codex (OpenAI)
    codex_bin: str = os.getenv("CODEX_BIN", "codex")
    codex_model: str = os.getenv("CODEX_MODEL", "")  # empty → codex picks default (gpt-5 / gpt-5.5)
    codex_reasoning_effort: str = os.getenv("CODEX_REASONING_EFFORT", "xhigh")  # minimal|low|medium|high|xhigh (xhigh is gpt-5+ only)
    codex_reasoning_summary: str = os.getenv("CODEX_REASONING_SUMMARY", "auto")  # none|auto|concise|detailed

    # Default agent if state/active_agent.md is absent / unreadable
    default_agent: str = os.getenv("DEFAULT_AGENT", "claude")

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


VALID_AGENTS = ("claude", "codex")


def read_active_agent() -> str:
    """Read state/active_agent.md (one line, just the agent name)."""
    try:
        agent = SETTINGS.active_agent_file.read_text(encoding="utf-8").strip().lower()
        if agent in VALID_AGENTS:
            return agent
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return SETTINGS.default_agent


def write_active_agent(agent: str) -> None:
    if agent not in VALID_AGENTS:
        raise ValueError(f"unknown agent {agent!r}; valid: {VALID_AGENTS}")
    SETTINGS.active_agent_file.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.active_agent_file.write_text(agent + "\n", encoding="utf-8")


def read_tick_interval_minutes() -> int:
    """Operator-overridable default for next_tick when Claude/Codex doesn't pick one."""
    import json
    try:
        data = json.loads(SETTINGS.tick_interval_json.read_text(encoding="utf-8"))
        m = int(data.get("minutes"))
        if m > 0:
            return m
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return SETTINGS.default_next_tick_minutes


def write_tick_interval_minutes(minutes: int) -> None:
    import json
    if minutes <= 0 or minutes > 24 * 60:
        raise ValueError("minutes must be in (0, 1440]")
    SETTINGS.tick_interval_json.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.tick_interval_json.write_text(
        json.dumps({"minutes": int(minutes)}, indent=2), encoding="utf-8"
    )
