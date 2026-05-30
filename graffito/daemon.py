"""Long-running daemon: heartbeat, dispatch ticks, snapshot progress, supervise."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from . import db, git_ops, schedule as sched_mod, snapshot, tick
from .config import SETTINGS, ensure_dirs
from .logging_setup import setup as setup_logging

log = logging.getLogger("graffito.daemon")

# Global socket timeout — same defensive measure as the trader's daemon.
socket.setdefaulttimeout(60)


_stop = threading.Event()
_heartbeat_inflight_since: float | None = None
_tick_inflight: bool = False
_last_periodic_snapshot: float = 0.0


def _disk_ok() -> bool:
    try:
        usage = shutil.disk_usage(str(SETTINGS.root))
        free_gb = usage.free / (1024**3)
        if free_gb < SETTINGS.disk_guard_min_gb:
            log.warning(
                "disk guard tripped: %.2f GB free < %.2f GB threshold",
                free_gb, SETTINGS.disk_guard_min_gb,
            )
            return False
        return True
    except Exception as e:
        log.warning("disk guard check failed: %s", e)
        return True  # don't block on a probe failure


def _paused() -> bool:
    return SETTINGS.paused_flag.exists()


def _regression_blocked() -> bool:
    return SETTINGS.regression_block.exists()


def _commit_cap_reached() -> bool:
    n = db.commits_today()
    if n >= SETTINGS.daily_commit_cap:
        log.warning("daily commit cap reached: %d >= %d", n, SETTINGS.daily_commit_cap)
        return True
    return False


def _check_regression(pre_pct: float | None, post_pct: float | None) -> None:
    if pre_pct is None or post_pct is None:
        return
    delta = post_pct - pre_pct
    if delta < -SETTINGS.regression_threshold_pct:
        log.error(
            "REGRESSION ALARM: fuzzy %% dropped %.4f (threshold -%.4f). "
            "Blocking further dispatch until `graffito ack-regression`.",
            delta, SETTINGS.regression_threshold_pct,
        )
        SETTINGS.regression_block.write_text(
            f"Regression detected at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"pre={pre_pct:.4f}% post={post_pct:.4f}% delta={delta:.4f}%\n",
            encoding="utf-8",
        )


def _select_mode() -> str:
    """Alternate ticks between IMPLEMENTATION (bulk new matches) and INVESTIGATION
    (surgical fixes + theory). Bias toward investigation only after an
    implementation tick produced commits — so a back-to-back blank implementation
    tick doesn't trap the bot in low-yield surgical work."""
    last = db.last_completed_mode()
    if last == "implementation":
        return "investigation"
    # last is None, "investigation", or unknown -> default to implementation so the
    # first scheduled tick after deploy / restart aims for bulk matches.
    return "implementation"


def _do_tick(reason: str) -> bool:
    """Acquire lock, run a tick, handle next_tick fallback. Returns True if Claude ran."""
    global _tick_inflight, _heartbeat_inflight_since
    lock = FileLock(str(SETTINGS.tick_lock), timeout=1)
    try:
        with lock:
            if not _disk_ok():
                return False
            if _commit_cap_reached():
                return False
            if _regression_blocked():
                log.warning("regression_block set; refusing to dispatch. Run `graffito ack-regression`.")
                return False

            pre_snap = snapshot.load_summary()
            pre_pct = pre_snap.fuzzy_match_pct if pre_snap else None
            pre_nt = sched_mod.read()

            mode = _select_mode()
            from .config import read_active_agent
            active_agent = read_active_agent()
            log.info("dispatching tick mode=%s agent=%s reason=%s", mode, active_agent, reason)

            _tick_inflight = True
            try:
                try:
                    exit_code, log_path = tick.run_tick(reason, mode=mode, agent=active_agent)
                    log.info("tick reason=%s mode=%s agent=%s exit=%s log=%s",
                             reason, mode, active_agent, exit_code, log_path)
                except Exception as e:
                    log.exception("tick crashed: %s", e)
                    exit_code = 1

                # Auto-push if there are local commits ahead of remote (Claude commits but
                # may have forgotten to push, or hit network trouble mid-tick).
                # Stays under _tick_inflight because the build-gate runs configure.py + ninja
                # and can take minutes — must not trip the watchdog.
                try:
                    pushed_sha = git_ops.push_main(tick_id=None, before_pct=pre_pct)
                    if pushed_sha:
                        log.info("auto-pushed local commits to remote (%s)", pushed_sha)
                except git_ops.BuildGateError as e:
                    log.error("auto-push aborted by build-gate: %s", e)
                except Exception as e:
                    log.warning("auto-push attempt failed: %s", e)

                post_snap = snapshot.load_summary()
                post_pct = post_snap.fuzzy_match_pct if post_snap else None
                _check_regression(pre_pct, post_pct)

                # The operator-set interval (state/tick_interval.json) is
                # authoritative for scheduling — the bot's `next_tick.json`
                # write is informational. Preserve the bot's `reason` text so
                # we still see what it intends to work on next, but always
                # rewrite `wake_at` to `now + interval`. This makes the
                # dashboard's "every <interval>" picker do what the user
                # expects regardless of what the agent wrote in the prompt.
                from .config import read_tick_interval_minutes
                from datetime import datetime, timedelta, timezone
                post_nt = sched_mod.read()
                interval_min = read_tick_interval_minutes()
                bot_reason = (
                    post_nt.reason
                    if (post_nt is not None
                        and pre_nt is not None
                        and post_nt.set_at != pre_nt.set_at)
                    else "scheduled"
                )
                nt = sched_mod.write(
                    when=datetime.now(timezone.utc) + timedelta(minutes=interval_min),
                    reason=bot_reason,
                    set_by="operator-interval",
                )
                log.info(
                    "next_tick set by operator-interval (%d min) reason=%s wake_at=%s",
                    interval_min, bot_reason, nt.wake_at.isoformat(),
                )
            finally:
                # Reset the heartbeat start so the post-tick heartbeat tail doesn't
                # appear to the watchdog as a 30-min-stuck heartbeat.
                _heartbeat_inflight_since = time.time()
                _tick_inflight = False
            return True
    except Timeout:
        log.info("tick already running; skipping overlap (reason=%s)", reason)
        return False


def _maybe_periodic_snapshot() -> None:
    global _last_periodic_snapshot
    if time.time() - _last_periodic_snapshot < SETTINGS.periodic_snapshot_seconds:
        return
    snap_id = snapshot.record(source="periodic")
    if snap_id is not None:
        log.debug("periodic snapshot #%s recorded", snap_id)
    _last_periodic_snapshot = time.time()


def _heartbeat_body() -> None:
    if _paused():
        log.debug("paused (state/.paused exists); skipping heartbeat")
        return

    _maybe_periodic_snapshot()

    if sched_mod.due():
        nt = sched_mod.read()
        reason = (nt.reason if nt else "scheduled") or "scheduled"
        _do_tick(reason)


def _heartbeat() -> None:
    global _heartbeat_inflight_since
    _heartbeat_inflight_since = time.time()
    try:
        _heartbeat_body()
    except Exception as e:
        log.exception("heartbeat crashed: %s", e)
    finally:
        _heartbeat_inflight_since = None


def _watchdog_loop() -> None:
    """Watch only the daemon's own heartbeat — NOT the Claude subprocess."""
    while not _stop.is_set():
        time.sleep(15)
        started = _heartbeat_inflight_since
        if started is None:
            continue
        if _tick_inflight:
            # During a tick, the heartbeat is "inflight" for as long as Claude is
            # working — possibly hours. That's expected; don't fire.
            continue
        elapsed = time.time() - started
        if elapsed > SETTINGS.liveness_watchdog_seconds:
            log.error(
                "watchdog: heartbeat stuck %.0fs > %ds (no tick in flight). Killing daemon.",
                elapsed, SETTINGS.liveness_watchdog_seconds,
            )
            os._exit(2)


def _record_boot_snapshot() -> None:
    snap_id = snapshot.record(source="boot")
    if snap_id is not None:
        log.info("boot snapshot recorded (#%s)", snap_id)


def run(foreground: bool = True) -> None:
    ensure_dirs()
    setup_logging(quiet_console=not foreground)
    db.init_db()

    # PID file
    SETTINGS.daemon_pid.write_text(str(os.getpid()), encoding="utf-8")

    def _shutdown(signum, _frame):
        log.info("signal %s received; stopping", signum)
        _stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("graffito daemon starting (pid=%s)", os.getpid())
    _record_boot_snapshot()

    if sched_mod.read() is None:
        nt = sched_mod.write(datetime.now(timezone.utc), reason="boot", set_by="daemon")
        log.info("no next_tick.json found; firing immediately")

    wd = threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True)
    wd.start()

    try:
        while not _stop.is_set():
            _heartbeat()
            # Sleep in small slices so SIGTERM is responsive.
            for _ in range(SETTINGS.heartbeat_seconds):
                if _stop.is_set():
                    break
                time.sleep(1)
    finally:
        try:
            SETTINGS.daemon_pid.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("graffito daemon stopped")


def stop_pidfile() -> bool:
    """Send SIGTERM to the daemon based on the pidfile. Returns True if signaled."""
    if not SETTINGS.daemon_pid.exists():
        return False
    try:
        pid = int(SETTINGS.daemon_pid.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        SETTINGS.daemon_pid.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def status_dict() -> dict:
    pid = None
    alive = False
    if SETTINGS.daemon_pid.exists():
        try:
            pid = int(SETTINGS.daemon_pid.read_text().strip())
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, ValueError, OSError):
            alive = False
    nt = sched_mod.read()
    return {
        "pid": pid,
        "alive": alive,
        "paused": _paused(),
        "regression_blocked": _regression_blocked(),
        "next_tick": (
            {
                "wake_at": nt.wake_at.isoformat(),
                "reason": nt.reason,
                "set_by": nt.set_by,
            }
            if nt else None
        ),
    }
