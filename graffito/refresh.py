"""Write per-tick read-only state files for Claude to consume."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db, snapshot
from .config import SETTINGS


def write_progress_md(prev_pct: float | None = None) -> None:
    summary = snapshot.load_summary()
    if summary is None:
        SETTINGS.progress_md.write_text(
            "# Progress\n\n_No build/GMSJ01/report.json yet. Run `python configure.py && ninja` in the repo first._\n",
            encoding="utf-8",
        )
        return

    lines: list[str] = []
    lines.append("# Progress\n")
    lines.append(f"_Snapshot: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append(f"- **Fuzzy match**: {summary.fuzzy_match_pct:.4f}%")
    if prev_pct is not None:
        delta = summary.fuzzy_match_pct - prev_pct
        sign = "+" if delta >= 0 else ""
        lines.append(f"- **Delta since last tick**: {sign}{delta:.4f}%")
    lines.append(f"- **Matched code**: {summary.matched_code:,} / {summary.total_code:,} bytes")
    lines.append(
        f"- **Matched functions**: {summary.matched_functions:,} / {summary.total_functions:,}"
    )
    lines.append(f"- **Complete units**: {summary.complete_units} / {summary.total_units}")
    lines.append("")
    lines.append("## Top 20 non-matching units by code size")
    lines.append("")
    lines.append("| Unit | Fuzzy % | Code (bytes) | Matched fns |")
    lines.append("|------|--------:|-------------:|------------:|")
    for u in summary.top_nonmatching(20):
        lines.append(
            f"| {u.name} | {u.fuzzy_pct:.2f} | {u.total_code:,} | {u.matched_functions}/{u.total_functions} |"
        )
    lines.append("")
    lines.append("_Pick anything. You are free to choose targets. The above is sorted by size,")
    lines.append("but small near-matches and totally-stubbed huge TUs are both legitimate work._")
    lines.append("")
    SETTINGS.progress_md.write_text("\n".join(lines), encoding="utf-8")


def write_git_status_md() -> None:
    repo = SETTINGS.repo_dir
    if not repo.exists():
        SETTINGS.git_status_md.write_text(
            f"# Git status\n\n_Repo not cloned yet at {repo}_\n", encoding="utf-8"
        )
        return

    def run(args: list[str]) -> str:
        try:
            return subprocess.check_output(
                args, cwd=str(repo), stderr=subprocess.STDOUT, text=True, encoding="utf-8"
            )
        except subprocess.CalledProcessError as e:
            return e.output or "(error)"
        except FileNotFoundError:
            return "(git not found)"

    status = run(["git", "status", "-sb"])
    log = run(["git", "log", "--oneline", "-10"])
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = run(["git", "rev-parse", "--short", "HEAD"]).strip()

    body = (
        "# Git status\n\n"
        f"- **Branch**: `{branch}` @ `{head}`\n"
        f"- **Remote**: `{SETTINGS.git_remote}/{SETTINGS.git_branch}` "
        f"(https://github.com/{SETTINGS.github_repo})\n\n"
        "## `git status -sb`\n\n```\n"
        f"{status}```\n\n"
        "## Last 10 commits\n\n```\n"
        f"{log}```\n"
    )
    SETTINGS.git_status_md.write_text(body, encoding="utf-8")


def write_last_tick_md() -> None:
    last = db.last_tick()
    if last is None:
        SETTINGS.last_tick_md.write_text(
            "# Last tick\n\n_No previous tick recorded — this is the bot's first run._\n",
            encoding="utf-8",
        )
        return
    summary = last["summary"] or "(no summary captured)"
    body = (
        "# Last tick\n\n"
        f"- **id**: {last['id']}\n"
        f"- **reason**: {last['reason']}\n"
        f"- **started**: {last['started_at']}\n"
        f"- **ended**: {last['ended_at'] or '(still running?)'}\n"
        f"- **exit_code**: {last['exit_code']}\n"
        f"- **log**: `{last['log_path'] or '(none)'}`\n\n"
        f"## Summary\n\n{summary}\n"
    )
    SETTINGS.last_tick_md.write_text(body, encoding="utf-8")


def write_tick_reason_md(reason: str) -> None:
    SETTINGS.tick_reason_md.write_text(
        f"# Tick reason\n\n{reason}\n\n_Fired at {datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n",
        encoding="utf-8",
    )


def write_all(reason: str, prev_pct: float | None = None) -> None:
    write_progress_md(prev_pct=prev_pct)
    write_git_status_md()
    write_last_tick_md()
    write_tick_reason_md(reason)


def ensure_goals_stub() -> None:
    if SETTINGS.goals_file.exists():
        return
    SETTINGS.goals_file.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.goals_file.write_text(
        "# Goals\n\n"
        "You are an autonomous decompilation agent for ryanbevins/graffito (SMS GMSJ01 decomp).\n"
        "Read `CLAUDE.md` and `AGENTS.md` in the repo at the start of every tick.\n\n"
        "Maintain this file yourself: strategy, priorities, lessons. Append to it as your\n"
        "understanding evolves. Keep it readable — it's both your memory and the human's window\n"
        "into how you're thinking about the project.\n",
        encoding="utf-8",
    )


def ensure_memory_stub() -> None:
    idx = SETTINGS.memory_dir / "MEMORY.md"
    if idx.exists():
        return
    SETTINGS.memory_dir.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        "# Memory index\n\n"
        "_(Empty. Write `feedback_*.md`, `project_*.md`, `reference_*.md` entries here and link\n"
        "them from this index as `- [Title](file.md) — one-line hook`.)_\n",
        encoding="utf-8",
    )
