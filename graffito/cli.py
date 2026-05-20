"""graffito CLI — typer/click entrypoint."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from . import daemon as daemon_mod
from . import db, refresh, schedule as sched_mod, snapshot, tick
from .config import SETTINGS, ensure_dirs
from .logging_setup import setup as setup_logging

console = Console()


@click.group()
def main() -> None:
    """graffito — autonomous SMS decompilation agent."""


# ── setup / admin ──────────────────────────────────────────────────────────


@main.command()
@click.option("--reset", is_flag=True, help="DESTRUCTIVE: delete existing DB.")
def init(reset: bool) -> None:
    """Initialize DB + state dirs + stub files."""
    ensure_dirs()
    db.init_db(reset=reset)
    refresh.ensure_goals_stub()
    refresh.ensure_memory_stub()
    console.print("[green]Initialized.[/green]")


# ── daemon ────────────────────────────────────────────────────────────────


@main.group()
def daemon() -> None:
    """Long-running daemon control."""


@daemon.command("start")
@click.option("--foreground", is_flag=True, help="Run in foreground (for systemd Type=simple).")
def daemon_start(foreground: bool) -> None:
    daemon_mod.run(foreground=foreground)


@daemon.command("stop")
def daemon_stop() -> None:
    if daemon_mod.stop_pidfile():
        console.print("[green]SIGTERM sent.[/green]")
    else:
        console.print("[yellow]No live pidfile.[/yellow]")


@daemon.command("status")
def daemon_status() -> None:
    s = daemon_mod.status_dict()
    nt = s["next_tick"]
    console.print(f"pid={s['pid']} alive={s['alive']} paused={s['paused']} "
                  f"regression_blocked={s['regression_blocked']}")
    if nt:
        console.print(f"next_tick: {nt['wake_at']} by {nt['set_by']} (reason={nt['reason']})")
    else:
        console.print("next_tick: [dim]not scheduled[/dim]")


# ── tick ──────────────────────────────────────────────────────────────────


@main.command()
@click.option("--reason", default="manual", show_default=True)
@click.option("--dry-run", is_flag=True, help="Build prompt + state files, but don't invoke claude.")
def tick_cmd(reason: str, dry_run: bool) -> None:
    """Fire one tick now (bypassing the next_tick schedule)."""
    setup_logging(quiet_console=False)
    exit_code, log_path = tick.run_tick(reason=reason, dry_run=dry_run)
    console.print(f"exit_code={exit_code} log={log_path}")


main.add_command(tick_cmd, name="tick")


# ── status & dashboard ────────────────────────────────────────────────────


@main.command()
def status() -> None:
    """Overall %, last tick, recent commits."""
    setup_logging(quiet_console=True)
    snap = snapshot.load_summary()
    if snap:
        console.print(
            f"[bold]Fuzzy match[/bold]: {snap.fuzzy_match_pct:.4f}%   "
            f"matched_fns={snap.matched_functions:,}/{snap.total_functions:,}   "
            f"complete_units={snap.complete_units}/{snap.total_units}"
        )
    else:
        console.print("[yellow]No report.json yet.[/yellow]")

    last = db.last_tick()
    if last:
        console.print(
            f"last tick: id={last['id']} reason={last['reason']} "
            f"exit={last['exit_code']} ended={last['ended_at']}"
        )
        if last["summary"]:
            console.print(f"  summary: {last['summary'][:200]}")

    rows = db.recent_commits(5)
    if rows:
        t = Table(show_header=True, header_style="bold")
        for col in ("SHA", "Time", "Before %", "After %", "Message"):
            t.add_column(col)
        for r in rows:
            t.add_row(
                (r["sha"] or "")[:10],
                r["pushed_at"][:19],
                f"{r['before_pct']:.4f}" if r["before_pct"] is not None else "—",
                f"{r['after_pct']:.4f}" if r["after_pct"] is not None else "—",
                (r["message"] or "")[:80],
            )
        console.print(t)


@main.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
def dashboard(host: str | None, port: int | None) -> None:
    """Run the FastAPI dashboard (used by graffito-dashboard.service)."""
    import uvicorn

    from .dashboard import app

    uvicorn.run(
        app,
        host=host or SETTINGS.dashboard_host,
        port=port or SETTINGS.dashboard_port,
        log_level="info",
    )


# ── journal / scheduling ──────────────────────────────────────────────────


@main.command()
@click.option("--tail", default=2, show_default=True, help="How many recent days to print.")
def journal(tail: int) -> None:
    """Print recent journal entries."""
    files = sorted(SETTINGS.journal_dir.glob("*.md"), reverse=True)[:tail]
    if not files:
        console.print("[dim]No journal entries yet.[/dim]")
        return
    for p in files:
        console.rule(p.name)
        console.print(p.read_text(encoding="utf-8"))


@main.command()
@click.option("--in", "in_", default=None, help="e.g. 30m, 2h, 1d")
@click.option("--at", "at_", default=None, help="ISO timestamp")
@click.option("--reason", default="operator-override")
def schedule(in_: str | None, at_: str | None, reason: str) -> None:
    """Set next_tick.json."""
    if not in_ and not at_:
        nt = sched_mod.read()
        if nt is None:
            console.print("[dim]not scheduled[/dim]")
        else:
            console.print(f"{nt.wake_at.isoformat()} by {nt.set_by} (reason={nt.reason})")
        return
    if at_:
        when = datetime.fromisoformat(at_.replace("Z", "+00:00"))
    else:
        when = datetime.now(timezone.utc) + _parse_interval(in_)  # type: ignore[arg-type]
    nt = sched_mod.write(when=when, reason=reason, set_by="operator")
    console.print(f"scheduled: {nt.wake_at.isoformat()}")


def _parse_interval(s: str) -> timedelta:
    s = s.strip().lower()
    if s.endswith("s"):
        return timedelta(seconds=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    return timedelta(minutes=int(s))


@main.command()
def report() -> None:
    """Print the latest refreshed state/progress.md."""
    if not SETTINGS.progress_md.exists():
        console.print("[yellow]No progress.md yet — daemon hasn't refreshed.[/yellow]")
        return
    console.print(SETTINGS.progress_md.read_text(encoding="utf-8"))


# ── safety ────────────────────────────────────────────────────────────────


@main.command("ack-regression")
def ack_regression() -> None:
    """Clear the regression_block flag so the daemon resumes dispatching."""
    if SETTINGS.regression_block.exists():
        SETTINGS.regression_block.unlink()
        console.print("[green]Regression block cleared.[/green]")
    else:
        console.print("[dim]No regression block set.[/dim]")


@main.command()
def pause() -> None:
    """Pause the daemon (it'll keep heartbeating but skip dispatch)."""
    SETTINGS.paused_flag.write_text("paused\n", encoding="utf-8")
    console.print("[yellow]Paused.[/yellow]")


@main.command()
def resume() -> None:
    """Resume from pause."""
    if SETTINGS.paused_flag.exists():
        SETTINGS.paused_flag.unlink()
    console.print("[green]Resumed.[/green]")


if __name__ == "__main__":
    main()
