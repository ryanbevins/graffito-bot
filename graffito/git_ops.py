"""Git helpers — direct push to main, build-gated."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import db, snapshot
from .config import SETTINGS

log = logging.getLogger("graffito.git")


class BuildGateError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cwd = cwd or SETTINGS.repo_dir
    log.debug("$ %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        encoding="utf-8",
    )


def current_sha() -> str:
    return _run(["git", "rev-parse", "HEAD"]).stdout.strip()


def has_unstaged_changes() -> bool:
    out = _run(["git", "status", "--porcelain"]).stdout
    return bool(out.strip())


def commits_ahead_of_remote() -> int:
    """How many local commits exist on main that are not yet pushed."""
    try:
        _run(["git", "fetch", SETTINGS.git_remote, SETTINGS.git_branch])
        out = _run(
            ["git", "rev-list", "--count", f"{SETTINGS.git_remote}/{SETTINGS.git_branch}..HEAD"]
        ).stdout
        return int(out.strip() or "0")
    except subprocess.CalledProcessError:
        return 0


def ensure_clean_main() -> None:
    """Fetch + fast-forward main. Raise if there's a divergence we can't resolve."""
    _run(["git", "fetch", SETTINGS.git_remote, SETTINGS.git_branch])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != SETTINGS.git_branch:
        log.warning("On branch %s, expected %s — switching", branch, SETTINGS.git_branch)
        _run(["git", "checkout", SETTINGS.git_branch])
    # Fast-forward only; if we have local commits ahead, leave them (they'll push next).
    try:
        _run(["git", "merge", "--ff-only", f"{SETTINGS.git_remote}/{SETTINGS.git_branch}"])
    except subprocess.CalledProcessError as e:
        # Non-fast-forward usually means we have unpushed local commits — that's fine.
        log.debug("merge --ff-only failed (probably have local commits ahead): %s", e.stderr)


def stage_all() -> None:
    _run(["git", "add", "-A"])


def commit(message: str) -> str | None:
    """Commit staged changes. Returns the new SHA, or None if nothing to commit."""
    proc = _run(["git", "diff", "--cached", "--quiet"], check=False)
    if proc.returncode == 0:
        log.info("Nothing staged to commit.")
        return None
    _run(["git", "commit", "-m", message])
    return current_sha()


def _build_succeeds() -> tuple[bool, str]:
    """Run `python3 configure.py && ninja` in the repo. Returns (ok, last_lines)."""
    import shutil as _sh
    python_bin = _sh.which("python3") or _sh.which("python") or "python3"
    try:
        cfg = _run([python_bin, "configure.py"], check=True)
        log.info("configure.py ok")
    except subprocess.CalledProcessError as e:
        tail = (e.stdout or "") + (e.stderr or "")
        return False, _tail(tail)

    try:
        build = _run(["ninja"], check=True)
        log.info("ninja ok")
        return True, _tail(build.stdout)
    except subprocess.CalledProcessError as e:
        tail = (e.stdout or "") + (e.stderr or "")
        return False, _tail(tail)


def _tail(s: str, lines: int = 40) -> str:
    parts = s.strip().splitlines()
    return "\n".join(parts[-lines:])


def push_main(tick_id: int | None = None, before_pct: float | None = None) -> str | None:
    """Verify build → push to origin/main → record commits in DB.

    Returns the pushed SHA (the most recent local commit), or None if push was a no-op
    (no local commits ahead of remote). Raises BuildGateError if build fails — the
    daemon should NOT swallow that, the bot needs to see the broken commit locally
    so it can fix forward without re-pushing.
    """
    ensure_clean_main()
    n_ahead = commits_ahead_of_remote()
    if n_ahead == 0:
        log.info("No local commits ahead of remote — nothing to push.")
        return None

    log.info("Build-gate: running configure.py && ninja before push (%d commit(s) ahead)", n_ahead)
    ok, tail = _build_succeeds()
    if not ok:
        raise BuildGateError(f"Build failed before push. Last lines:\n{tail}")

    # Snapshot AFTER build so report.json reflects HEAD
    sha_before_push = current_sha()
    after_pct: float | None = None
    summary = snapshot.load_summary()
    if summary is not None:
        after_pct = summary.fuzzy_match_pct
        db.insert_snapshot(
            fuzzy_match_pct=summary.fuzzy_match_pct,
            matched_code=summary.matched_code,
            total_code=summary.total_code,
            matched_functions=summary.matched_functions,
            total_functions=summary.total_functions,
            complete_units=summary.complete_units,
            total_units=summary.total_units,
            complete_code=summary.complete_code,
            commit_sha=sha_before_push,
            source="post_tick",
        )

    log.info("Pushing %d commit(s) to %s/%s", n_ahead, SETTINGS.git_remote, SETTINGS.git_branch)
    _run(["git", "push", SETTINGS.git_remote, SETTINGS.git_branch])

    # Log each pushed commit in the DB
    commit_log = _run(
        [
            "git",
            "log",
            "--pretty=format:%H%x09%s",
            f"{SETTINGS.git_remote}/{SETTINGS.git_branch}@{{1}}..HEAD",
        ],
        check=False,
    )
    if commit_log.returncode != 0:
        # Fallback: just record HEAD
        db.insert_commit(sha_before_push, _short_message(), tick_id, before_pct, after_pct)
    else:
        for line in commit_log.stdout.strip().splitlines():
            if "\t" in line:
                sha, msg = line.split("\t", 1)
                db.insert_commit(sha, msg, tick_id, before_pct, after_pct)

    return sha_before_push


def _short_message() -> str:
    return _run(["git", "log", "-1", "--pretty=%s"]).stdout.strip()
