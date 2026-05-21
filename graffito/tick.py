"""Single tick: refresh state, snapshot pre-state, invoke claude, snapshot post-state."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import db, refresh, snapshot
from .config import SETTINGS, ensure_dirs

log = logging.getLogger("graffito.tick")


def _prompt_body(recovery: bool = False) -> str:
    sys_ctx = (SETTINGS.prompts_dir / "system_context.md").read_text(encoding="utf-8")
    specific_name = "recovery_prompt.md" if recovery else "tick_prompt.md"
    specific = (SETTINGS.prompts_dir / specific_name).read_text(encoding="utf-8")
    return sys_ctx + "\n\n---\n\n" + specific


def _extract_summary(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1].strip()
    try:
        obj = json.loads(last)
        if isinstance(obj, dict):
            for key in ("result", "response", "text", "content"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:2000]
    except Exception:
        pass
    return last[:2000]


def _last_exit_was_error() -> bool:
    last = db.last_tick()
    return last is not None and last["exit_code"] not in (None, 0)


def run_tick(reason: str, dry_run: bool = False, mode: str | None = None) -> tuple[int, str | None]:
    """Run one Claude cycle (or dry-run to print the constructed prompt).

    Returns (exit_code, log_path). NO subprocess timeout — Claude can think as long
    as it needs. The daemon's tick lock prevents overlapping ticks.

    `mode` selects the focus directive written to state/tick_focus.md
    ('implementation' or 'investigation') — the daemon alternates this per tick.
    """
    ensure_dirs()
    refresh.ensure_goals_stub()
    refresh.ensure_memory_stub()

    # Snapshot BEFORE the tick so the graph has a data point even if the tick errors
    pre_snap = snapshot.load_summary()
    pre_pct = pre_snap.fuzzy_match_pct if pre_snap else None
    if pre_snap is not None:
        db.insert_snapshot(
            fuzzy_match_pct=pre_snap.fuzzy_match_pct,
            matched_code=pre_snap.matched_code,
            total_code=pre_snap.total_code,
            matched_functions=pre_snap.matched_functions,
            total_functions=pre_snap.total_functions,
            complete_units=pre_snap.complete_units,
            total_units=pre_snap.total_units,
            commit_sha=None,
            source="pre_tick",
        )

    refresh.write_all(reason, prev_pct=pre_pct, mode=mode)

    recovery = reason == "recovery_after_fail" or (reason == "scheduled" and _last_exit_was_error())
    prompt = _prompt_body(recovery=recovery)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = SETTINGS.tick_log_dir / f"tick-{ts}.log"

    if dry_run:
        log.info("DRY RUN — would invoke claude. Prompt written to %s", log_path)
        log_path.write_text(
            f"# DRY RUN tick — reason={reason}\n# recovery={recovery}\n\n"
            f"=== PROMPT ({len(prompt)} chars) ===\n{prompt}\n",
            encoding="utf-8",
        )
        return 0, str(log_path)

    tick_id = db.insert_tick(reason, db.now_iso(), mode=mode)
    log.info("tick %s starting (reason=%s, mode=%s, recovery=%s)", tick_id, reason, mode, recovery)

    claude_bin = shutil.which(SETTINGS.claude_bin) or SETTINGS.claude_bin
    cmd = [
        claude_bin,
        "-p", prompt,
        "--model", SETTINGS.claude_model,
        "--add-dir", str(SETTINGS.state_dir),
        "--add-dir", str(SETTINGS.repo_dir),
        "--allowedTools",
        "Read,Write,Edit,Bash,Glob,Grep,WebFetch,WebSearch",
    ]

    # Also write to a "current tick" pointer file so the dashboard can find
    # the active log without scanning the directory.
    current_ptr = SETTINGS.tick_log_dir / ".current"
    current_ptr.write_text(str(log_path), encoding="utf-8")

    # Stream stdout+stderr line-by-line so the log is tailable while the tick
    # runs — capture_output=True would buffer everything until exit.
    stdout_chunks: list[str] = []
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# tick {tick_id} — {reason}\n")
            f.write(f"# recovery: {recovery}\n")
            f.write(f"# cwd: {SETTINGS.repo_dir}\n")
            f.write(f"# claude_bin: {claude_bin}\n")
            f.write(f"# model: {SETTINGS.claude_model}\n")
            f.write(f"# started: {db.now_iso()}\n\n=== STREAM ===\n")
            f.flush()

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(SETTINGS.repo_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # merge for chronological order
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,  # line-buffered
                )
            except FileNotFoundError:
                msg = f"claude binary not found: {SETTINGS.claude_bin}"
                log.error(msg)
                f.write(f"ERROR: {msg}\n")
                db.finish_tick(tick_id, db.now_iso(), 127, msg, str(log_path))
                return 127, str(log_path)

            assert proc.stdout is not None
            for line in proc.stdout:
                f.write(line)
                f.flush()
                stdout_chunks.append(line)
            returncode = proc.wait()
            f.write(f"\n=== exit_code: {returncode} ===\n")
    finally:
        try:
            current_ptr.unlink(missing_ok=True)
        except Exception:
            pass

    class _Proc:
        pass
    proc_summary = _Proc()
    proc_summary.returncode = returncode  # type: ignore[attr-defined]
    proc = proc_summary
    stdout = "".join(stdout_chunks)
    summary = _extract_summary(stdout)
    db.finish_tick(tick_id, db.now_iso(), proc.returncode, summary, str(log_path))

    # Post-tick snapshot
    post_snap = snapshot.load_summary()
    if post_snap is not None:
        try:
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(SETTINGS.repo_dir),
                text=True,
                encoding="utf-8",
            ).strip()
        except Exception:
            head_sha = None
        db.insert_snapshot(
            fuzzy_match_pct=post_snap.fuzzy_match_pct,
            matched_code=post_snap.matched_code,
            total_code=post_snap.total_code,
            matched_functions=post_snap.matched_functions,
            total_functions=post_snap.total_functions,
            complete_units=post_snap.complete_units,
            total_units=post_snap.total_units,
            commit_sha=head_sha,
            source="post_tick",
        )

    log.info("tick %s done (exit=%s)", tick_id, proc.returncode)
    return proc.returncode, str(log_path)
