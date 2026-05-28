"""FastAPI dashboard — single auto-refreshing page over Tailscale.

Modeled on trader/dashboard.py. Adds a Chart.js progress-over-time graph fed
by `progress_snapshots`. No mutating endpoints except pause/resume.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import daemon as daemon_mod
from . import db, schedule as sched_mod, snapshot
from .config import SETTINGS

# Cache for the per-unit report parse — report.json is ~3 MB so we don't want to
# re-parse on every dashboard hit. Invalidated by file mtime.
_units_cache: dict[str, object] = {"mtime": 0.0, "units": [], "overall": None}

app = FastAPI(title="graffito dashboard", docs_url=None, redoc_url=None)

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", SETTINGS.dashboard_token or "")


def _check_auth(request: Request) -> None:
    if not DASHBOARD_TOKEN:
        return
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer ") and bearer.removeprefix("Bearer ").strip() == DASHBOARD_TOKEN:
        return
    if request.query_params.get("token") == DASHBOARD_TOKEN:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


# ── JSON endpoints ─────────────────────────────────────────────────────────


@app.get("/api/status")
def api_status(request: Request) -> JSONResponse:
    _check_auth(request)
    snap = snapshot.load_summary()
    latest_db = db.latest_snapshot()
    last = db.last_tick()
    nt = sched_mod.read()

    # Delta vs 24h ago
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    earlier = db.snapshot_series(since_iso=yesterday)
    delta_24h = None
    if snap is not None and earlier:
        first = earlier[0]
        delta_24h = snap.fuzzy_match_pct - float(first["fuzzy_match_pct"])

    return JSONResponse({
        "fuzzy_match_pct": snap.fuzzy_match_pct if snap else None,
        "matched_code": snap.matched_code if snap else None,
        "total_code": snap.total_code if snap else None,
        "matched_functions": snap.matched_functions if snap else None,
        "total_functions": snap.total_functions if snap else None,
        "complete_units": snap.complete_units if snap else None,
        "total_units": snap.total_units if snap else None,
        "delta_24h": delta_24h,
        "last_snapshot_at": latest_db["recorded_at"] if latest_db else None,
        "last_tick": dict(last) if last else None,
        "next_tick": {
            "wake_at": nt.wake_at.isoformat(),
            "reason": nt.reason,
            "set_by": nt.set_by,
        } if nt else None,
        "daemon": daemon_mod.status_dict(),
    })


def _fit_eta(pts: list[tuple[float, float]], current_ts: float, current_pct: float) -> dict:
    """Linear-regression fit on the given (timestamp_sec, pct) points.
    Returns a fit summary including slope_per_day, r2, and eta_utc."""
    n = len(pts)
    if n < 5:
        return {"status": "insufficient_data", "points": n, "needed": 5}
    mean_t = sum(p[0] for p in pts) / n
    mean_y = sum(p[1] for p in pts) / n
    num = sum((p[0] - mean_t) * (p[1] - mean_y) for p in pts)
    den = sum((p[0] - mean_t) ** 2 for p in pts)
    if den == 0:
        return {"status": "no_time_spread", "points": n}
    slope_per_sec = num / den
    intercept = mean_y - slope_per_sec * mean_t
    slope_per_day = slope_per_sec * 86400
    ss_tot = sum((p[1] - mean_y) ** 2 for p in pts)
    r2 = 1.0 if ss_tot == 0 else max(
        0.0,
        1.0 - sum((p[1] - (slope_per_sec * p[0] + intercept)) ** 2 for p in pts) / ss_tot,
    )
    span_hours = (pts[-1][0] - pts[0][0]) / 3600.0

    base = {
        "points": n,
        "slope_per_day": slope_per_day,
        "r2": r2,
        "span_hours": span_hours,
    }
    if slope_per_day <= 0:
        return {"status": "not_advancing", **base}
    seconds_to_100 = (100.0 - current_pct) / slope_per_sec
    if seconds_to_100 < 0:
        return {"status": "ok", "eta_utc": None, **base}
    if seconds_to_100 > 86400 * 365 * 50:
        return {"status": "not_converging", **base}
    eta_ts = current_ts + seconds_to_100
    return {
        "status": "ok",
        "eta_utc": datetime.fromtimestamp(eta_ts, tz=timezone.utc).isoformat(timespec="seconds"),
        **base,
    }


@app.get("/api/eta")
def api_eta(request: Request) -> JSONResponse:
    """Projected completion (fuzzy_match_pct → 100%) from two windows:

    - **all**: linear regression over every progress_snapshots row. Stable,
      smoothed; lags real-time pace.
    - **24h**: regression over the last 24 h of snapshots. Reactive — reflects
      whether the bot is currently making progress, not where it averages to
      over the project lifetime.

    Each window may independently come back ok / insufficient_data / no_time_spread
    / not_advancing / not_converging. The dashboard shows the 24-hour ETA as
    primary (live), the all-time as secondary (stable), and falls back
    gracefully when 24h data is too thin.
    """
    _check_auth(request)
    rows = db.snapshot_series(since_iso=None)
    pts_all: list[tuple[float, float]] = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["recorded_at"]).timestamp()
            pts_all.append((t, float(r["fuzzy_match_pct"])))
        except Exception:
            continue
    if not pts_all:
        return JSONResponse({"status": "no_data"})

    current_ts = pts_all[-1][0]
    current_pct = pts_all[-1][1]
    cutoff_24h = current_ts - 86400
    pts_24h = [p for p in pts_all if p[0] >= cutoff_24h]

    return JSONResponse({
        "current_pct": current_pct,
        "all": _fit_eta(pts_all, current_ts, current_pct),
        "h24": _fit_eta(pts_24h, current_ts, current_pct),
    })


@app.get("/api/progress_series")
def api_progress_series(request: Request, range: str = "7d") -> JSONResponse:
    _check_auth(request)
    cutoff = None
    now = datetime.now(timezone.utc)
    if range == "24h":
        cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    elif range == "7d":
        cutoff = (now - timedelta(days=7)).isoformat(timespec="seconds")
    elif range == "30d":
        cutoff = (now - timedelta(days=30)).isoformat(timespec="seconds")
    # range == "all" → cutoff stays None
    rows = db.snapshot_series(since_iso=cutoff)
    out = [
        {
            "ts": r["recorded_at"],
            "fuzzy_pct": r["fuzzy_match_pct"],
            "matched_code": r["matched_code"],
            "matched_functions": r["matched_functions"],
            "complete_units": r["complete_units"],
            "source": r["source"],
        }
        for r in rows
    ]
    return JSONResponse(out)


@app.get("/api/live")
def api_live(request: Request) -> JSONResponse:
    """Live activity for the in-flight tick (if any).

    Reads the current-tick pointer at logs/ticks/.current, tails the live
    transcript, and inspects the Claude subprocess tree for what shell
    commands the bot is currently running (ninja, mwcc, find, etc.). Falls
    back gracefully when no tick is in flight.
    """
    _check_auth(request)
    import subprocess as _sp
    current_ptr = SETTINGS.tick_log_dir / ".current"
    if not current_ptr.exists():
        # No live tick. Return the last completed tick id + a short tail so the
        # panel always has something to show.
        last = db.last_tick()
        if last is None:
            return JSONResponse({"running": False, "tick": None, "tail": "", "subprocesses": []})
        log_path = Path(last["log_path"] or "")
        tail = ""
        if log_path.exists():
            try:
                tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
            except Exception:
                tail = ""
        return JSONResponse({
            "running": False,
            "tick": dict(last),
            "elapsed_sec": None,
            "tail": tail,
            "subprocesses": [],
        })

    try:
        log_path = Path(current_ptr.read_text(encoding="utf-8").strip())
    except Exception:
        return JSONResponse({"running": False, "tick": None, "tail": "", "subprocesses": []})

    # The in-flight tick is the most recent row with ended_at IS NULL.
    with db.connect() as c:
        row = c.execute(
            "SELECT id, reason, started_at, log_path FROM ticks WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

    tail = ""
    if log_path.exists():
        try:
            # Read last 8 KB so very long ticks don't pull MB of text
            with open(log_path, "rb") as fp:
                fp.seek(0, 2)
                size = fp.tell()
                fp.seek(max(0, size - 8192))
                blob = fp.read().decode("utf-8", errors="replace")
            tail = "\n".join(blob.splitlines()[-60:])
        except Exception as e:
            tail = f"(could not read log: {e})"

    elapsed_sec = None
    if row:
        try:
            started = datetime.fromisoformat(row["started_at"])
            elapsed_sec = (datetime.now(timezone.utc) - started).total_seconds()
        except Exception:
            pass

    # Inspect the daemon's process tree to surface what the bot is currently
    # doing. We descend from the graffito daemon's pid through claude into its
    # children — those are the shell commands Claude has invoked.
    subprocs: list[dict] = []
    try:
        daemon_pid = int(SETTINGS.daemon_pid.read_text().strip()) if SETTINGS.daemon_pid.exists() else None
        if daemon_pid is not None:
            visited: set[int] = set()
            def descend(pid: int, depth: int) -> None:
                if pid in visited or depth > 6:
                    return
                visited.add(pid)
                try:
                    out = _sp.check_output(["pgrep", "-P", str(pid)], text=True).strip()
                except _sp.CalledProcessError:
                    return
                for child in (int(x) for x in out.splitlines() if x.strip()):
                    try:
                        cmd = _sp.check_output(
                            ["ps", "-o", "comm=", "-o", "args=", "-p", str(child)],
                            text=True,
                        ).strip()
                    except Exception:
                        cmd = "?"
                    # Trim the giant prompt that claude carries as argv
                    comm = cmd.split(None, 1)[0] if cmd else "?"
                    args = (cmd[len(comm):].strip())[:160] if cmd else ""
                    subprocs.append({"pid": child, "depth": depth, "comm": comm, "args": args})
                    descend(child, depth + 1)
            descend(daemon_pid, 0)
    except Exception:
        pass

    return JSONResponse({
        "running": row is not None,
        "tick": dict(row) if row else None,
        "elapsed_sec": elapsed_sec,
        "tail": tail,
        "subprocesses": subprocs,
    })


@app.get("/api/mwcc")
def api_mwcc(request: Request) -> PlainTextResponse:
    """The bot's living MWCC understanding doc, sourced from the graffito repo."""
    _check_auth(request)
    p = SETTINGS.repo_dir / "docs" / "MWCC.md"
    if not p.exists():
        return PlainTextResponse("(docs/MWCC.md not found — bot hasn't initialized it yet)")
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/memory")
def api_memory(request: Request) -> JSONResponse:
    _check_auth(request)
    files = sorted(SETTINGS.memory_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse([
        {
            "name": p.name,
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        }
        for p in files
    ])


@app.get("/api/memory/{name}")
def api_memory_one(request: Request, name: str) -> PlainTextResponse:
    _check_auth(request)
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")
    p = SETTINGS.memory_dir / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/units")
def api_units(request: Request) -> JSONResponse:
    """Per-TU progress straight from report.json. Mtime-cached so the 3 MB parse
    only happens once per build."""
    _check_auth(request)
    rj = SETTINGS.report_json
    if not rj.exists():
        return JSONResponse({"overall": None, "units": []})
    mtime = rj.stat().st_mtime
    if _units_cache["mtime"] != mtime:
        from . import report
        summary = report.load(rj)
        _units_cache["mtime"] = mtime
        _units_cache["overall"] = summary.overall_summary()
        _units_cache["units"] = [
            {
                "name": u.name,
                "fuzzy_pct": u.fuzzy_pct,
                "total_code": u.total_code,
                "matched_code": u.matched_code,
                "matched_functions": u.matched_functions,
                "total_functions": u.total_functions,
                "complete": u.complete,
                "source_path": u.source_path,
            }
            for u in summary.units
        ]
    return JSONResponse({
        "overall": _units_cache["overall"],
        "units": _units_cache["units"],
        "mtime": mtime,
    })


@app.get("/api/ticks")
def api_ticks(request: Request, limit: int = 25) -> JSONResponse:
    _check_auth(request)
    rows = db.recent_ticks(limit=limit)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/attempts")
def api_attempts(request: Request, limit: int = 50) -> JSONResponse:
    _check_auth(request)
    rows = db.recent_attempts(limit=limit)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/commits")
def api_commits(request: Request, limit: int = 25) -> JSONResponse:
    """Recent commits — source from `git log` so commits pushed by Claude
    itself (not via the daemon's auto-push) still appear. The `commits` DB
    table is consulted for any cached before/after % data."""
    _check_auth(request)
    import subprocess as _sp
    # Pull the DB rows once so we can decorate git log entries with % deltas
    db_by_sha = {r["sha"]: r for r in db.recent_commits(limit=200)}

    rows: list[dict] = []
    try:
        out = _sp.check_output(
            ["git", "log", "--pretty=format:%H%x09%cI%x09%s", f"-n{limit}"],
            cwd=str(SETTINGS.repo_dir),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sha, iso_date, message = parts
            extra = db_by_sha.get(sha)
            rows.append({
                "sha": sha,
                "pushed_at": iso_date,
                "message": message,
                "tick_id": extra["tick_id"] if extra else None,
                "before_pct": extra["before_pct"] if extra else None,
                "after_pct": extra["after_pct"] if extra else None,
            })
    except Exception as e:
        # Fall back to the DB-only view if git isn't available
        return JSONResponse([dict(r) for r in db.recent_commits(limit=limit)])
    return JSONResponse(rows)


@app.get("/api/journal/today")
def api_journal_today(request: Request) -> PlainTextResponse:
    _check_auth(request)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = SETTINGS.journal_dir / f"{today}.md"
    if not p.exists():
        return PlainTextResponse("(no journal entry for today yet)")
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/journal/recent")
def api_journal_recent(request: Request) -> PlainTextResponse:
    _check_auth(request)
    files = sorted(SETTINGS.journal_dir.glob("*.md"), reverse=True)[:2]
    if not files:
        return PlainTextResponse("(no journal entries)")
    parts = []
    for f in files:
        parts.append(f"# === {f.stem} ===\n\n{f.read_text(encoding='utf-8')}")
    return PlainTextResponse("\n\n".join(parts))


@app.get("/api/goals")
def api_goals(request: Request) -> PlainTextResponse:
    _check_auth(request)
    if not SETTINGS.goals_file.exists():
        return PlainTextResponse("(goals.md not yet written)")
    return PlainTextResponse(SETTINGS.goals_file.read_text(encoding="utf-8"))


@app.get("/api/notes")
def api_notes(request: Request) -> JSONResponse:
    _check_auth(request)
    files = sorted(SETTINGS.notes_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse([
        {
            "name": p.name,
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        }
        for p in files
    ])


@app.get("/api/notes/{name}")
def api_note_one(request: Request, name: str) -> PlainTextResponse:
    _check_auth(request)
    # Defensive: refuse paths with separators.
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")
    p = SETTINGS.notes_dir / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/tick_log/{tick_id}")
def api_tick_log(request: Request, tick_id: int) -> PlainTextResponse:
    _check_auth(request)
    with db.connect() as c:
        row = c.execute("SELECT log_path FROM ticks WHERE id = ?", (tick_id,)).fetchone()
    if not row or not row["log_path"]:
        raise HTTPException(status_code=404)
    p = Path(row["log_path"])
    if not p.exists():
        raise HTTPException(status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))


@app.post("/api/pause")
async def api_pause(request: Request) -> JSONResponse:
    _check_auth(request)
    SETTINGS.paused_flag.write_text("paused via dashboard\n", encoding="utf-8")
    return JSONResponse({"paused": True})


@app.post("/api/resume")
async def api_resume(request: Request) -> JSONResponse:
    _check_auth(request)
    if SETTINGS.paused_flag.exists():
        SETTINGS.paused_flag.unlink()
    return JSONResponse({"paused": False})


# ── HTML page ──────────────────────────────────────────────────────────────


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>graffito</title>
<style>
  :root {
    --bg: #0e1116; --panel: #161b22; --border: #2d333b;
    --text: #e6edf3; --dim: #8b949e; --green: #3fb950; --red: #f85149;
    --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: ui-monospace,SFMono-Regular,Menlo,monospace;
         background: var(--bg); color: var(--text); font-size: 14px; }
  header { padding: 14px 18px; border-bottom: 1px solid var(--border);
           display: flex; align-items: baseline; gap: 22px; flex-wrap: wrap; background: var(--panel); }
  header h1 { margin: 0; font-size: 16px; }
  .stat { color: var(--dim); }
  .stat b { color: var(--text); font-size: 20px; }
  .stat .pos { color: var(--green); }
  .stat .neg { color: var(--red); }
  main { padding: 18px; display: grid; gap: 18px; max-width: 1800px; margin: 0 auto;
         grid-template-columns: repeat(2, minmax(0, 1fr)); }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  section.full { grid-column: 1 / -1; }
  section h2 { margin: 0; padding: 10px 14px; font-size: 13px; border-bottom: 1px solid var(--border);
               color: var(--dim); text-transform: uppercase; letter-spacing: 0.06em;
               display: flex; align-items: center; gap: 10px; }
  section h2 .controls { margin-left: auto; display: flex; gap: 6px; }
  section h2 .controls button {
    background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text);
    padding: 2px 10px; border-radius: 4px; font: inherit; font-size: 11px; cursor: pointer;
  }
  section h2 .controls button.active { background: var(--blue); color: #001; border-color: var(--blue); }
  section h2 .controls input[type="search"] {
    background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text);
    padding: 2px 8px; border-radius: 4px; font: inherit; font-size: 11px; width: 200px;
  }
  section h2 .controls input[type="search"]:focus { outline: none; border-color: var(--blue); }
  section h2 .controls select {
    background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text);
    padding: 2px 8px; border-radius: 4px; font: inherit; font-size: 11px;
  }
  /* Fix unreadable browser-default option background — render options on the panel surface. */
  section h2 .controls select option {
    background: var(--panel); color: var(--text);
  }
  section h2 .controls select option:checked, section h2 .controls select option:hover {
    background: var(--blue); color: #001;
  }
  section .body { padding: 12px 14px; max-height: 420px; overflow: auto; }
  section.chart .body { max-height: none; padding: 8px 4px; }
  section.tall .body { max-height: 700px; }
  section.units .body { max-height: none; padding: 0; }
  .units-footer { padding: 6px 14px; border-top: 1px solid var(--border); color: var(--dim); font-size: 11.5px; }

  /* Treemap */
  #treemap-host { position: relative; padding: 8px; height: 640px; }
  #treemap-svg { width: 100%; height: 100%; display: block; }
  #treemap-svg .group-label { font: 600 11px/1 ui-monospace, monospace; fill: var(--text); pointer-events: none; opacity: 0.9; letter-spacing: 0.04em; }
  #treemap-svg .tu-label { font: 500 10px/1 ui-monospace, monospace; fill: #0e1116; pointer-events: none; opacity: 0.78; }
  #treemap-svg .tu-label.dark { fill: #f0f6fc; }
  #treemap-svg .tu-rect { stroke: #0e1116; stroke-width: 1; cursor: pointer; transition: filter 120ms; }
  #treemap-svg .tu-rect:hover { filter: brightness(1.35) saturate(1.15); }
  #treemap-svg .group-rect { fill: none; stroke: rgba(255,255,255,0.16); stroke-width: 1; pointer-events: none; }
  #treemap-tooltip {
    position: absolute; pointer-events: none; z-index: 5;
    background: rgba(14,17,22,0.96); border: 1px solid var(--border); border-radius: 5px;
    padding: 8px 11px; font-size: 12px; line-height: 1.4; color: var(--text);
    max-width: 360px; box-shadow: 0 4px 18px rgba(0,0,0,0.4);
    opacity: 0; transition: opacity 120ms;
  }
  #treemap-tooltip.visible { opacity: 1; }
  #treemap-tooltip .tt-name { color: var(--blue); margin-bottom: 4px; font-weight: 600; word-break: break-all; }
  #treemap-tooltip .tt-meta { color: var(--dim); font-size: 11.5px; }
  #treemap-tooltip .tt-meta b { color: var(--text); font-weight: 500; }

  /* Color scale legend */
  .legend { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--dim); }
  .legend .scale {
    width: 160px; height: 10px; border-radius: 2px;
    background: linear-gradient(90deg, #6e1f1f 0%, #b16c1a 50%, #2f8a3e 92%, #3fb950 100%);
    border: 1px solid var(--border);
  }

  /* Live activity */
  section.live h2 .pill.live { background: var(--green); color: #001; }
  section.live h2 .pill.live::before {
    content: "● "; color: rgba(0,0,0,0.4);
    animation: pulse 1.6s infinite;
  }
  @keyframes pulse { 50% { opacity: 0.35; } }
  .live-grid { display: grid; grid-template-columns: 280px 1fr; gap: 0; min-height: 280px; }
  @media (max-width: 900px) { .live-grid { grid-template-columns: 1fr; } }
  .live-meta { border-right: 1px solid var(--border); padding: 10px 12px; font-size: 12px; }
  .live-meta .kv { display: grid; grid-template-columns: 82px 1fr; gap: 4px 8px; margin-bottom: 10px; }
  .live-meta .kv .k { color: var(--dim); }
  .live-meta .kv .v { color: var(--text); word-break: break-word; }
  .live-meta .procs { margin-top: 6px; }
  .live-meta .proc { padding: 3px 0; display: grid; grid-template-columns: auto 1fr; gap: 6px; align-items: baseline; font-size: 11.5px; }
  .live-meta .proc .comm { color: var(--blue); }
  .live-meta .proc .args { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .live-meta .proc.d0 { padding-left: 0; }
  .live-meta .proc.d1 { padding-left: 10px; }
  .live-meta .proc.d2 { padding-left: 20px; }
  .live-meta .proc.d3 { padding-left: 30px; }
  .live-tail { padding: 8px 12px; font-size: 11.5px; line-height: 1.45; overflow: auto; max-height: 280px; background: rgba(0,0,0,0.18); }
  .live-tail pre { font: inherit; color: var(--text); margin: 0; }
  @media (min-width: 901px) { section.live .body { padding: 0; max-height: none; } }

  /* Module progress bars */
  section.modules .body { max-height: none; padding: 12px 14px; }
  .mod-row { display: grid; grid-template-columns: 140px 1fr 110px; gap: 10px; align-items: center; padding: 4px 0; font-size: 12px; }
  .mod-row .name { color: var(--text); }
  .mod-row .stat-text { text-align: right; color: var(--dim); font-size: 11.5px; }
  .mod-bar { background: rgba(255,255,255,0.04); border-radius: 3px; height: 16px; position: relative; overflow: hidden; border: 1px solid var(--border); }
  .mod-bar .fill { height: 100%; border-radius: 2px; }

  /* Tick log modal */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 50;
    display: none; align-items: center; justify-content: center;
  }
  .modal-bg.visible { display: flex; }
  .modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
    width: min(960px, 92vw); max-height: 84vh; display: flex; flex-direction: column;
    box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  }
  .modal h3 {
    margin: 0; padding: 10px 14px; border-bottom: 1px solid var(--border);
    font-size: 14px; display: flex; align-items: center; gap: 10px;
  }
  .modal .modal-close {
    margin-left: auto; background: none; border: 1px solid var(--border); color: var(--text);
    width: 24px; height: 24px; border-radius: 4px; cursor: pointer; font: inherit;
  }
  .modal .modal-body { padding: 12px 16px; overflow: auto; flex: 1; }
  .modal pre { white-space: pre-wrap; word-wrap: break-word; font: 12px/1.45 ui-monospace,monospace; margin: 0; }

  /* Knowledge browser */
  .kb-grid { display: grid; grid-template-columns: 260px 1fr; gap: 0; min-height: 380px; }
  @media (max-width: 900px) { .kb-grid { grid-template-columns: 1fr; } }
  .kb-list { border-right: 1px solid var(--border); overflow-y: auto; max-height: 540px; }
  .kb-section { padding: 6px 10px; font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--panel); }
  .kb-item { padding: 6px 12px; cursor: pointer; border-bottom: 1px solid rgba(255,255,255,0.03); display: grid; grid-template-columns: 1fr auto; gap: 6px; align-items: baseline; }
  .kb-item:hover { background: rgba(255,255,255,0.04); }
  .kb-item.active { background: rgba(88,166,255,0.12); }
  .kb-item .nm { font-size: 12.5px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kb-item .meta { font-size: 10.5px; color: var(--dim); }
  .kb-empty { padding: 12px; color: var(--dim); font-size: 12px; }
  .kb-view { padding: 14px 18px; overflow: auto; max-height: 540px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; white-space: nowrap; vertical-align: top; }
  th { color: var(--dim); font-weight: normal; border-bottom: 1px solid var(--border); }
  tr:hover td { background: rgba(255,255,255,0.02); }
  td.right, th.right { text-align: right; }
  td.msg { color: var(--dim); white-space: normal; max-width: 480px; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  .pill { display: inline-block; padding: 1px 6px; border-radius: 10px; font-size: 11px; background: rgba(255,255,255,0.05); }
  .pill.green { background: rgba(63,185,80,0.15); color: var(--green); }
  .pill.red { background: rgba(248,81,73,0.15); color: var(--red); }
  .pill.yellow { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .pill.blue { background: rgba(88,166,255,0.15); color: var(--blue); }
  pre { white-space: pre-wrap; word-wrap: break-word; margin: 0; font-family: inherit; font-size: 13px; line-height: 1.45; }
  .md { line-height: 1.55; }
  .md h1 { font-size: 18px; }
  .md h2 { font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
  .md h3 { font-size: 14px; color: var(--blue); }
  .md code { background: rgba(255,255,255,0.06); padding: 1px 5px; border-radius: 3px; }
  .md pre { background: rgba(255,255,255,0.04); padding: 10px 12px; border-radius: 4px; overflow-x: auto; }
  .md a { color: var(--blue); text-decoration: none; }
  .md a:hover { text-decoration: underline; }
  .md table { border-collapse: collapse; font-size: 12.5px; }
  .md th, .md td { border: 1px solid var(--border); padding: 4px 10px; }
  footer { padding: 10px 18px; border-top: 1px solid var(--border); color: var(--dim); font-size: 12px; }
  button.pause { background: var(--red); color: white; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font: inherit; }
  button.resume { background: var(--green); color: #001; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font: inherit; }
  #chart-host { height: 320px; padding: 6px 8px; }
</style>
</head>
<body>
<header>
  <h1>graffito</h1>
  <span class="stat"><b id="pct">—</b> fuzzy</span>
  <span class="stat"><b id="pct-code">—</b> code</span>
  <span class="stat"><b id="pct-fns">—</b> fns</span>
  <span class="stat"><b id="pct-units">—</b> units</span>
  <span class="stat" id="delta">Δ24h: —</span>
  <span class="stat" id="eta" title="Linear projection from progress_snapshots — improves with more data.">ETA: —</span>
  <span class="stat" id="daemon-pill"></span>
  <span class="stat" id="next-tick"></span>
  <span style="margin-left:auto"><button id="pause-btn" class="pause">Pause</button></span>
</header>
<main>
  <section class="full live">
    <h2>
      Live activity
      <span id="live-pill" class="pill dim">idle</span>
      <span id="live-elapsed" class="dim" style="font-size:11.5px;"></span>
    </h2>
    <div class="body">
      <div class="live-grid">
        <div class="live-meta" id="live-meta">—</div>
        <div class="live-tail"><pre id="live-tail">(no transcript yet)</pre></div>
      </div>
    </div>
  </section>

  <section class="full chart">
    <h2>
      Progress over time
      <span class="controls">
        <label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--dim); padding:0 8px 0 0;">
          <input type="checkbox" data-series="fuzzy" checked> <span style="color:#58a6ff;">fuzzy</span>
        </label>
        <label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--dim); padding:0 8px 0 0;">
          <input type="checkbox" data-series="code"> <span style="color:#3fb950;">code</span>
        </label>
        <label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--dim); padding:0 8px 0 0;">
          <input type="checkbox" data-series="fns" checked> <span style="color:#d29922;">fns</span>
        </label>
        <label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--dim); padding:0 8px 0 0;">
          <input type="checkbox" data-series="units"> <span style="color:#bc8cff;">units</span>
        </label>
        <span style="width:8px"></span>
        <button data-range="24h">24h</button>
        <button data-range="7d" class="active">7d</button>
        <button data-range="30d">30d</button>
        <button data-range="all">all</button>
      </span>
    </h2>
    <div class="body"><div id="chart-host"><canvas id="chart"></canvas></div></div>
  </section>

  <section>
    <h2>Recent ticks</h2>
    <div class="body">
      <table id="ticks-table"><thead><tr>
        <th>ID</th><th>Reason</th><th>Started</th><th>Ended</th><th>Exit</th><th class="msg">Summary</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </section>

  <section>
    <h2>Recent commits</h2>
    <div class="body">
      <table id="commits-table"><thead><tr>
        <th>SHA</th><th>Pushed</th><th class="right">Before</th><th class="right">After</th><th class="msg">Message</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </section>

  <section class="full">
    <h2>Recent function attempts</h2>
    <div class="body">
      <table id="attempts-table"><thead><tr>
        <th>Time</th><th>TU</th><th>Symbol</th><th class="right">Before %</th><th class="right">After %</th><th>Outcome</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </section>

  <section class="full modules">
    <h2>Progress by module</h2>
    <div class="body"><div id="modules-host">—</div></div>
  </section>

  <section class="full units">
    <h2>
      Translation units
      <span class="controls">
        <input id="unit-search" type="search" placeholder="filter (name / source)" />
        <select id="unit-status">
          <option value="all" selected>all</option>
          <option value="incomplete">incomplete</option>
          <option value="zero">0% only</option>
          <option value="near">near match (90-99.99%)</option>
          <option value="complete">complete</option>
        </select>
        <span class="legend">
          <span>0%</span><span class="scale"></span><span>100%</span>
        </span>
        <span id="unit-count" class="dim" style="font-size:11px;"></span>
      </span>
    </h2>
    <div class="body">
      <div id="treemap-host">
        <svg id="treemap-svg"></svg>
        <div id="treemap-tooltip"></div>
      </div>
    </div>
    <div class="units-footer" id="units-footer">—</div>
  </section>

  <section class="tall">
    <h2>Goals</h2>
    <div class="body md" id="goals">…</div>
  </section>

  <section class="tall">
    <h2>Today's journal</h2>
    <div class="body md" id="journal">…</div>
  </section>

  <section class="full tall">
    <h2>MWCC understanding (bot's living theory · <a href="https://github.com/__GITHUB_REPO__/blob/main/docs/MWCC.md" target="_blank" style="color:var(--blue); text-decoration:none; font-size:11px;">docs/MWCC.md ↗</a>)</h2>
    <div class="body md" id="mwcc">…</div>
  </section>

  <section class="full">
    <h2>Knowledge — notes &amp; memory</h2>
    <div class="body" style="padding:0; max-height:none;">
      <div class="kb-grid">
        <div class="kb-list" id="kb-list">
          <div class="kb-section">Notes (per-TU)</div>
          <div id="kb-notes" class="kb-empty">—</div>
          <div class="kb-section">Memory (cross-TU)</div>
          <div id="kb-memory" class="kb-empty">—</div>
        </div>
        <div class="kb-view md" id="kb-view">
          <em class="dim">Select a note or memory entry on the left.</em>
        </div>
      </div>
    </div>
  </section>
</main>

<div class="modal-bg" id="ticklog-modal-bg">
  <div class="modal">
    <h3>Tick <span id="ticklog-modal-id"></span> transcript
      <button class="modal-close" onclick="closeTicklog()">✕</button>
    </h3>
    <div class="modal-body"><pre id="ticklog-modal-body">…</pre></div>
  </div>
</div>
<footer>
  <span id="last-snapshot">—</span> · graffito-bot dashboard · auto-refresh 15s
</footer>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/d3@7.8.5/dist/d3.min.js"></script>
<script>
let chart = null;
let currentRange = "7d";
const TOKEN = new URLSearchParams(location.search).get("token") || "";
function authQ() { return TOKEN ? ("?token=" + encodeURIComponent(TOKEN)) : ""; }
function authQ_amp() { return TOKEN ? ("&token=" + encodeURIComponent(TOKEN)) : ""; }

// ── Time formatting ───────────────────────────────────────────────────────
// All DB timestamps are ISO UTC. Display them as Manila time (UTC+8), 12-hour.
const TZ = "Asia/Manila";
const FMT_LONG = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", hour12: true,
});
const FMT_SHORT = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true,
});
const FMT_DATE = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, month: "short", day: "numeric",
});
function ts(iso, kind) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  if (kind === "short") return FMT_SHORT.format(d);
  if (kind === "date") return FMT_DATE.format(d);
  return FMT_LONG.format(d);
}

async function jget(path) {
  const r = await fetch(path + authQ());
  if (!r.ok) throw new Error("fetch " + path + " → " + r.status);
  return r.json();
}
async function tget(path) {
  const r = await fetch(path + authQ());
  return r.ok ? r.text() : "(error " + r.status + ")";
}

function fmtPct(p) { return p === null || p === undefined ? "—" : (Number(p).toFixed(4) + "%"); }
function fmtSign(d) { if (d === null || d === undefined) return "—"; const s = d >= 0 ? "+" : ""; return s + Number(d).toFixed(4) + "%"; }

function _slopeStr(s) {
  if (s === null || s === undefined) return "—";
  return Math.abs(s) >= 0.1 ? s.toFixed(3) : s.toExponential(2);
}

function _fitSummary(label, fit) {
  if (!fit) return `${label}: —`;
  if (fit.status === "insufficient_data") return `${label}: ${fit.points}/${fit.needed} points`;
  if (fit.status === "no_time_spread") return `${label}: no time spread`;
  if (fit.status === "not_advancing") return `${label}: flat (${_slopeStr(fit.slope_per_day)}%/d)`;
  if (fit.status === "not_converging") return `${label}: >50y (${_slopeStr(fit.slope_per_day)}%/d)`;
  if (fit.status === "ok" && fit.eta_utc) {
    return `${label}: ${ts(fit.eta_utc, "date")} (${_slopeStr(fit.slope_per_day)}%/d, R²=${fit.r2.toFixed(2)})`;
  }
  return `${label}: —`;
}

function _confClass(r2) {
  if (r2 === undefined || r2 === null) return "dim";
  if (r2 >= 0.85) return "pos";
  if (r2 >= 0.5) return "";
  return "dim";
}

async function refreshEta() {
  const e = await jget("/api/eta");
  const el = document.getElementById("eta");
  if (e.status === "no_data") {
    el.textContent = "ETA: no data";
    el.className = "stat dim";
    return;
  }

  const live = e.h24 || {};
  const all = e.all || {};
  // Prefer the live (24h) projection if it produced an ETA; otherwise fall
  // back to all-time. If neither projects, surface whichever has more signal.
  const primary = live.status === "ok" && live.eta_utc ? live
                : all.status  === "ok" && all.eta_utc  ? all
                : (live.status === "insufficient_data" ? all : live);
  const primaryLabel = primary === live ? "live (24h)" : "all-time";

  let primaryHtml;
  if (primary.status === "ok" && primary.eta_utc) {
    const cls = _confClass(primary.r2);
    primaryHtml = `ETA: <b>${ts(primary.eta_utc, "date")}</b> ` +
                  `<span class="${cls}" style="font-size:11px;">` +
                  `(${_slopeStr(primary.slope_per_day)}%/d · R²=${primary.r2.toFixed(2)} · ${primaryLabel})</span>`;
    el.className = "stat";
  } else if (primary.status === "insufficient_data") {
    primaryHtml = `ETA: ${primary.points}/${primary.needed} points`;
    el.className = "stat dim";
  } else if (primary.status === "not_advancing") {
    primaryHtml = `ETA: flat (${_slopeStr(primary.slope_per_day)}%/d)`;
    el.className = "stat dim";
  } else if (primary.status === "not_converging") {
    primaryHtml = `ETA: >50y out (${_slopeStr(primary.slope_per_day)}%/d)`;
    el.className = "stat dim";
  } else {
    primaryHtml = "ETA: —";
    el.className = "stat dim";
  }

  // Append the secondary window inline (smaller, dim) when it's meaningful
  const secondary = primary === live ? all : live;
  const secondaryLabel = primary === live ? "all" : "24h";
  if (secondary && secondary.status && secondary !== primary) {
    if (secondary.status === "ok" && secondary.eta_utc) {
      primaryHtml += ` <span class="dim" style="font-size:11px;">| ${secondaryLabel}: ${ts(secondary.eta_utc, "date")} (${_slopeStr(secondary.slope_per_day)}%/d)</span>`;
    } else if (secondary.status === "not_advancing") {
      primaryHtml += ` <span class="dim" style="font-size:11px;">| ${secondaryLabel}: flat</span>`;
    } else if (secondary.status === "insufficient_data") {
      primaryHtml += ` <span class="dim" style="font-size:11px;">| ${secondaryLabel}: ${secondary.points}/${secondary.needed}</span>`;
    }
  }
  el.innerHTML = primaryHtml;

  el.title =
    `current ${(e.current_pct ?? 0).toFixed(4)}%\n` +
    `live (24h) → ${_fitSummary("eta", live)} · ${live.points ?? 0} pts over ${(live.span_hours ?? 0).toFixed(2)}h\n` +
    `all-time   → ${_fitSummary("eta", all)} · ${all.points ?? 0} pts over ${(all.span_hours ?? 0).toFixed(2)}h\n` +
    `Projection assumes the current pace holds — live updates with each tick, all-time is more stable.`;
}

function pctOf(num, denom) {
  if (num === null || denom === null || !denom) return null;
  return (num / denom) * 100;
}

async function refreshStatus() {
  const s = await jget("/api/status");
  document.getElementById("pct").textContent = fmtPct(s.fuzzy_match_pct);

  const codePct = pctOf(s.matched_code, s.total_code);
  const fnsPct  = pctOf(s.matched_functions, s.total_functions);
  const unitsPct = pctOf(s.complete_units, s.total_units);
  document.getElementById("pct-code").textContent  = codePct === null ? "—" : codePct.toFixed(2) + "%";
  document.getElementById("pct-fns").textContent   = fnsPct  === null ? "—" : fnsPct.toFixed(2)  + "%";
  document.getElementById("pct-units").textContent = unitsPct === null ? "—" : unitsPct.toFixed(1) + "%";
  document.getElementById("pct-code").title  = s.matched_code != null ? `${s.matched_code.toLocaleString()} / ${s.total_code.toLocaleString()} matched bytes (byte-perfect)` : "";
  document.getElementById("pct-fns").title   = s.matched_functions != null ? `${s.matched_functions} / ${s.total_functions} functions fully matched` : "";
  document.getElementById("pct-units").title = s.complete_units != null ? `${s.complete_units} / ${s.total_units} TUs fully complete (100% all sections)` : "";

  const dEl = document.getElementById("delta");
  if (s.delta_24h === null) { dEl.textContent = "Δ24h: —"; }
  else {
    dEl.textContent = "Δ24h: " + fmtSign(s.delta_24h);
    dEl.className = "stat " + (s.delta_24h >= 0 ? "pos" : "neg");
  }

  const dpill = document.getElementById("daemon-pill");
  const d = s.daemon || {};
  const alive = d.alive ? "running" : "stopped";
  const cls = d.alive ? "green" : "red";
  let pillText = alive;
  if (d.paused) pillText += " · paused";
  if (d.regression_blocked) pillText += " · regression-block";
  dpill.innerHTML = '<span class="pill ' + cls + '">' + pillText + '</span>';

  if (s.next_tick) {
    document.getElementById("next-tick").textContent = "next: " + ts(s.next_tick.wake_at) + " (" + s.next_tick.set_by + ")";
  } else {
    document.getElementById("next-tick").textContent = "next: —";
  }
  document.getElementById("last-snapshot").textContent = "last snapshot: " + ts(s.last_snapshot_at);

  const btn = document.getElementById("pause-btn");
  if (d.paused) { btn.textContent = "Resume"; btn.className = "resume"; }
  else { btn.textContent = "Pause"; btn.className = "pause"; }
}

const SERIES_DEFS = {
  fuzzy: { label: "Fuzzy match %",       color: "#58a6ff", get: (d, totals) => d.fuzzy_pct },
  code:  { label: "Matched code %",      color: "#3fb950", get: (d, totals) => d.matched_code != null && totals.total_code ? d.matched_code / totals.total_code * 100 : null },
  fns:   { label: "Matched functions %", color: "#d29922", get: (d, totals) => d.matched_functions != null && totals.total_functions ? d.matched_functions / totals.total_functions * 100 : null },
  units: { label: "Complete units %",    color: "#bc8cff", get: (d, totals) => d.complete_units != null && totals.total_units ? d.complete_units / totals.total_units * 100 : null },
};
const SERIES_ENABLED = { fuzzy: true, code: false, fns: true, units: false };

let _lastSeriesTotals = { total_code: 1, total_functions: 1, total_units: 1 };

function _buildDatasets(data) {
  const totals = _lastSeriesTotals;
  const result = [];
  for (const key of Object.keys(SERIES_DEFS)) {
    if (!SERIES_ENABLED[key]) continue;
    const def = SERIES_DEFS[key];
    result.push({
      label: def.label,
      data: data.map(d => def.get(d, totals)),
      borderColor: def.color,
      backgroundColor: def.color.replace(")", ",0.08)").replace("rgb(", "rgba(").replace(/^#/, "") ? def.color + "14" : def.color,
      tension: 0.15,
      fill: false,
      pointRadius: 0,
      borderWidth: 1.6,
    });
  }
  return result;
}

async function refreshChart() {
  const [series, status] = await Promise.all([
    jget("/api/progress_series?range=" + currentRange + authQ_amp()),
    jget("/api/status"),
  ]);
  // Use latest status totals to convert raw counts in older snapshots into %
  _lastSeriesTotals = {
    total_code: status.total_code || 1,
    total_functions: status.total_functions || 1,
    total_units: status.total_units || 1,
  };
  const labels = series.map(d => ts(d.ts, currentRange === "24h" ? "short" : "long"));
  const datasets = _buildDatasets(series);
  if (!chart) {
    const ctx = document.getElementById("chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "rgba(255,255,255,0.04)" } },
          y: { ticks: { color: "#8b949e", callback: v => v.toFixed(2) + "%" }, grid: { color: "rgba(255,255,255,0.04)" } },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update();
  }
}

async function refreshTicks() {
  const rows = await jget("/api/ticks?limit=10");
  const tb = document.querySelector("#ticks-table tbody");
  tb.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const exit = r.exit_code;
    const exitPill = exit === 0 ? '<span class="pill green">0</span>'
                   : exit === null ? '<span class="pill yellow">…</span>'
                   : '<span class="pill red">' + exit + '</span>';
    tr.style.cursor = "pointer";
    tr.title = "Click to view full transcript";
    tr.addEventListener("click", () => openTicklog(r.id));
    tr.innerHTML = `<td>${r.id}</td><td>${r.reason}</td><td>${ts(r.started_at)}</td><td>${ts(r.ended_at)}</td><td>${exitPill}</td><td class="msg">${(r.summary||'').replace(/</g,'&lt;')}</td>`;
    tb.appendChild(tr);
  }
}

function fmtElapsed(sec) {
  if (sec === null || sec === undefined) return "";
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

async function refreshLive() {
  const e = await jget("/api/live");
  const pill = document.getElementById("live-pill");
  const elap = document.getElementById("live-elapsed");
  const meta = document.getElementById("live-meta");
  const tail = document.getElementById("live-tail");

  if (e.running && e.tick) {
    pill.className = "pill live";
    pill.textContent = `tick #${e.tick.id} · ${e.tick.reason}`;
    elap.textContent = `elapsed ${fmtElapsed(e.elapsed_sec)}`;
    let metaHtml = `<div class="kv">
      <div class="k">started</div><div class="v">${ts(e.tick.started_at)}</div>
      <div class="k">reason</div><div class="v">${e.tick.reason || ''}</div>
      <div class="k">log</div><div class="v" style="font-size:11px; color: var(--dim); word-break: break-all;">${e.tick.log_path || ''}</div>
    </div>`;
    metaHtml += `<div class="dim" style="margin-top:6px; font-size:11px; text-transform:uppercase; letter-spacing:0.04em;">subprocesses</div>`;
    if (e.subprocesses && e.subprocesses.length) {
      metaHtml += '<div class="procs">';
      for (const p of e.subprocesses) {
        metaHtml += `<div class="proc d${Math.min(3, p.depth)}"><span class="comm">${p.comm}</span><span class="args" title="${(p.args||'').replace(/"/g,'&quot;')}">${p.args ? '<span class="dim"> · </span>' + (p.args.length > 60 ? p.args.slice(0,60) + '…' : p.args) : ''}</span></div>`;
      }
      metaHtml += '</div>';
    } else {
      metaHtml += '<div class="dim" style="font-size:11.5px; margin-top:4px;">(claude thinking — no shell subprocess)</div>';
    }
    meta.innerHTML = metaHtml;
  } else if (e.tick) {
    pill.className = "pill dim";
    pill.textContent = `idle · last #${e.tick.id} ${e.tick.exit_code === 0 ? '✓' : 'exit '+e.tick.exit_code}`;
    elap.textContent = `next tick will appear here when it fires`;
    meta.innerHTML = `<div class="kv">
      <div class="k">last tick</div><div class="v">#${e.tick.id} · ${e.tick.reason}</div>
      <div class="k">ended</div><div class="v">${ts(e.tick.ended_at)}</div>
      <div class="k">exit</div><div class="v">${e.tick.exit_code}</div>
    </div>
    <div class="dim" style="font-size:11.5px;">${(e.tick.summary||'').replace(/</g,'&lt;').slice(0, 400)}</div>`;
  } else {
    pill.className = "pill dim";
    pill.textContent = "no ticks yet";
    elap.textContent = "";
    meta.innerHTML = '<span class="dim">no ticks recorded</span>';
  }

  if (e.tail && e.tail.trim()) {
    tail.textContent = e.tail;
    // Auto-scroll to bottom only when content grew
    const parent = tail.parentElement;
    parent.scrollTop = parent.scrollHeight;
  } else if (!e.running) {
    tail.textContent = "(no recent transcript)";
  }
}

// ── Per-module progress ───────────────────────────────────────────────────
function renderModules() {
  if (!UNITS.length) return;
  const buckets = new Map();
  for (const u of UNITS) {
    const parts = u.name.split("/");
    const key = parts.length >= 2 ? parts[1] : parts[0];
    if (!buckets.has(key)) buckets.set(key, { matched: 0, total: 0, units: 0, complete: 0 });
    const b = buckets.get(key);
    b.matched += u.matched_code || 0;
    b.total += u.total_code || 0;
    b.units += 1;
    if (u.fuzzy_pct >= 100) b.complete += 1;
  }
  const arr = [...buckets.entries()].map(([name, b]) => ({
    name,
    matched: b.matched,
    total: b.total,
    pct: b.total ? (b.matched / b.total * 100) : 0,
    units: b.units,
    complete: b.complete,
  })).sort((a, b) => b.total - a.total);

  const host = document.getElementById("modules-host");
  host.innerHTML = arr.map(m => {
    const color = (function(p) {
      const c = PCT_COLOR(p);
      return c;
    })(m.pct);
    return `<div class="mod-row" title="${m.units} TUs, ${m.complete} complete">
      <div class="name">${m.name}</div>
      <div class="mod-bar"><div class="fill" style="width:${m.pct.toFixed(2)}%; background:${color}"></div></div>
      <div class="stat-text">${m.pct.toFixed(2)}% · ${m.complete}/${m.units}</div>
    </div>`;
  }).join("");
}

// ── Knowledge browser (notes + memory) ────────────────────────────────────
let KB_SELECTION = null;  // {kind: 'notes'|'memory', name}

function renderKbList(kind, items) {
  const el = document.getElementById(kind === "notes" ? "kb-notes" : "kb-memory");
  if (!items || !items.length) {
    el.className = "kb-empty";
    el.innerHTML = "<em>(none yet)</em>";
    return;
  }
  el.className = "";
  el.innerHTML = items.map(it => {
    const date = ts(it.modified, "date");
    const active = KB_SELECTION && KB_SELECTION.kind === kind && KB_SELECTION.name === it.name ? "active" : "";
    return `<div class="kb-item ${active}" data-kind="${kind}" data-name="${encodeURIComponent(it.name)}">
      <span class="nm">${escapeHtml(it.name.replace(/\.md$/,''))}</span>
      <span class="meta">${date}</span>
    </div>`;
  }).join("");
  el.querySelectorAll(".kb-item").forEach(el => {
    el.addEventListener("click", () => {
      const kind = el.dataset.kind;
      const name = decodeURIComponent(el.dataset.name);
      kbSelect(kind, name);
    });
  });
}

async function kbSelect(kind, name) {
  KB_SELECTION = { kind, name };
  const url = (kind === "notes" ? "/api/notes/" : "/api/memory/") + encodeURIComponent(name);
  const md = await tget(url);
  document.getElementById("kb-view").innerHTML = marked.parse(md);
  // Re-render lists to update active state
  refreshKnowledge();
}

async function refreshKnowledge() {
  try {
    const [notes, memory] = await Promise.all([jget("/api/notes"), jget("/api/memory")]);
    renderKbList("notes", notes);
    renderKbList("memory", memory);
    // If nothing is selected and notes exist, leave the placeholder; the user clicks.
  } catch (e) { console.error(e); }
}

// ── Tick log modal ────────────────────────────────────────────────────────
async function openTicklog(tickId) {
  const txt = await tget(`/api/tick_log/${tickId}`);
  document.getElementById("ticklog-modal-id").textContent = "#" + tickId;
  document.getElementById("ticklog-modal-body").textContent = txt;
  document.getElementById("ticklog-modal-bg").classList.add("visible");
}
function closeTicklog() {
  document.getElementById("ticklog-modal-bg").classList.remove("visible");
}
document.getElementById("ticklog-modal-bg").addEventListener("click", (e) => {
  if (e.target.id === "ticklog-modal-bg") closeTicklog();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTicklog();
});

async function refreshCommits() {
  const rows = await jget("/api/commits?limit=10");
  const tb = document.querySelector("#commits-table tbody");
  tb.innerHTML = "";
  for (const r of rows) {
    const short = (r.sha || '').slice(0, 10);
    const url = "https://github.com/" + GITHUB_REPO + "/commit/" + r.sha;
    const before = r.before_pct === null ? "—" : Number(r.before_pct).toFixed(4);
    const after = r.after_pct === null ? "—" : Number(r.after_pct).toFixed(4);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><a href="${url}" target="_blank">${short}</a></td><td>${ts(r.pushed_at)}</td><td class="right">${before}</td><td class="right">${after}</td><td class="msg">${(r.message||'').replace(/</g,'&lt;')}</td>`;
    tb.appendChild(tr);
  }
}

async function refreshAttempts() {
  const rows = await jget("/api/attempts?limit=30");
  const tb = document.querySelector("#attempts-table tbody");
  tb.innerHTML = "";
  for (const r of rows) {
    const out = r.outcome;
    const cls = { improved: 'green', matched: 'green', regressed: 'red', no_change: '', errored: 'red' }[out] || '';
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${ts(r.recorded_at)}</td><td>${r.tu}</td><td>${r.symbol||''}</td><td class="right">${r.before_pct||''}</td><td class="right">${r.after_pct||''}</td><td><span class="pill ${cls}">${out}</span></td>`;
    tb.appendChild(tr);
  }
}

async function refreshJournal() {
  const md = await tget("/api/journal/today");
  document.getElementById("journal").innerHTML = marked.parse(md);
}

// ── Units treemap ─────────────────────────────────────────────────────────
let UNITS = [];
let UNITS_MTIME = 0;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// Defang raw HTML in markdown the bot writes — e.g. when a journal entry mentions
// "CLBRoundf<s>" the <s> would be parsed as a strikethrough opener and leak across
// every following paragraph. Escaping raw HTML tokens means the bot can write
// template-syntax-looking text without breaking the rendering.
marked.use({
  renderer: {
    html(token) {
      const raw = typeof token === 'string' ? token : (token && token.text) || '';
      return escapeHtml(raw);
    },
  },
});

// Color scale: a perceptually smooth red → amber → green ramp that lands
// on the dashboard's palette colors at the extremes.
const PCT_COLOR = d3.scaleLinear()
  .domain([0, 35, 70, 92, 100])
  .range(["#6e1f1f", "#b16c1a", "#7a8a1a", "#2f8a3e", "#3fb950"])
  .interpolate(d3.interpolateLab)
  .clamp(true);

function tuShortLabel(name) {
  // "mario/Enemy/bosseel" → "bosseel"
  const parts = name.split("/");
  return parts[parts.length - 1];
}

function tuGroupKey(name) {
  // First two segments form the group ("mario/Enemy"), so JSystem subdirs
  // collapse to a single "mario/JSystem" tile group, etc.
  const parts = name.split("/");
  if (parts.length <= 2) return parts.join("/");
  return parts.slice(0, 2).join("/");
}

function buildHierarchy(units) {
  const groups = new Map();
  for (const u of units) {
    const k = tuGroupKey(u.name);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(u);
  }
  // Stable sort: groups by total code desc, units within group by code desc
  const children = [...groups.entries()]
    .map(([k, arr]) => ({
      name: k,
      children: arr
        .filter(u => (u.total_code || 0) > 0)
        .sort((a, b) => (b.total_code || 0) - (a.total_code || 0))
        .map(u => ({ ...u, value: u.total_code || 1 })),
    }))
    .filter(g => g.children.length > 0)
    .sort((a, b) => d3.sum(b.children, c => c.value) - d3.sum(a.children, c => c.value));
  return { name: "root", children };
}

function filterUnits(units) {
  const search = document.getElementById("unit-search").value.trim().toLowerCase();
  const status = document.getElementById("unit-status").value;
  let rows = units.slice();
  if (status === "incomplete") rows = rows.filter(u => u.fuzzy_pct < 100);
  else if (status === "zero") rows = rows.filter(u => u.fuzzy_pct === 0);
  else if (status === "near") rows = rows.filter(u => u.fuzzy_pct >= 90 && u.fuzzy_pct < 100);
  else if (status === "complete") rows = rows.filter(u => u.fuzzy_pct >= 100);
  if (search) rows = rows.filter(u =>
    u.name.toLowerCase().includes(search) ||
    (u.source_path || "").toLowerCase().includes(search));
  return rows;
}

function renderTreemap() {
  const host = document.getElementById("treemap-host");
  const svg = d3.select("#treemap-svg");
  svg.selectAll("*").remove();

  const rows = filterUnits(UNITS);
  if (rows.length === 0) {
    svg.append("text").attr("x", "50%").attr("y", "50%")
       .attr("fill", "#8b949e").attr("text-anchor", "middle")
       .text("(no TUs match the current filter)");
    document.getElementById("unit-count").textContent = `0 of ${UNITS.length}`;
    renderFooter();
    return;
  }

  const width = host.clientWidth - 16;
  const height = host.clientHeight - 16;

  const root = d3.hierarchy(buildHierarchy(rows))
    .sum(d => d.value || 0)
    .sort((a, b) => b.value - a.value);

  d3.treemap()
    .size([width, height])
    .padding(2)
    .paddingTop(d => d.depth === 0 ? 0 : 18)
    .round(true)
    (root);

  const groupG = svg.append("g");
  const groups = root.children || [];
  groupG.selectAll("rect.group-rect")
    .data(groups)
    .join("rect")
    .attr("class", "group-rect")
    .attr("x", d => d.x0)
    .attr("y", d => d.y0)
    .attr("width", d => Math.max(0, d.x1 - d.x0))
    .attr("height", d => Math.max(0, d.y1 - d.y0))
    .attr("rx", 3);

  groupG.selectAll("text.group-label")
    .data(groups.filter(g => (g.x1 - g.x0) > 80 && (g.y1 - g.y0) > 24))
    .join("text")
    .attr("class", "group-label")
    .attr("x", d => d.x0 + 6)
    .attr("y", d => d.y0 + 12)
    .text(d => d.data.name.toUpperCase());

  const leaves = root.leaves();
  const tu = svg.append("g")
    .selectAll("g")
    .data(leaves)
    .join("g")
    .attr("transform", d => `translate(${d.x0},${d.y0})`);

  tu.append("rect")
    .attr("class", "tu-rect")
    .attr("width", d => Math.max(0, d.x1 - d.x0))
    .attr("height", d => Math.max(0, d.y1 - d.y0))
    .attr("fill", d => PCT_COLOR(d.data.fuzzy_pct || 0))
    .attr("rx", 1.5)
    .on("mousemove", function(event, d) { showTooltip(event, d.data); })
    .on("mouseleave", hideTooltip)
    .on("click", function(_event, d) {
      if (d.data.source_path) {
        window.open(`https://github.com/${GITHUB_REPO}/blob/main/${d.data.source_path}`, "_blank");
      }
    });

  // Labels: only on cells big enough to read them
  tu.filter(d => (d.x1 - d.x0) > 56 && (d.y1 - d.y0) > 22)
    .append("text")
    .attr("class", d => "tu-label" + ((d.data.fuzzy_pct || 0) < 50 ? " dark" : ""))
    .attr("x", 5)
    .attr("y", 13)
    .text(d => {
      const label = tuShortLabel(d.data.name);
      const maxChars = Math.max(4, Math.floor((d.x1 - d.x0 - 10) / 6.5));
      return label.length > maxChars ? label.slice(0, maxChars - 1) + "…" : label;
    });

  tu.filter(d => (d.x1 - d.x0) > 72 && (d.y1 - d.y0) > 38)
    .append("text")
    .attr("class", d => "tu-label" + ((d.data.fuzzy_pct || 0) < 50 ? " dark" : ""))
    .attr("x", 5)
    .attr("y", 27)
    .style("opacity", 0.65)
    .text(d => `${(d.data.fuzzy_pct || 0).toFixed(1)}%`);

  document.getElementById("unit-count").textContent = `${rows.length} of ${UNITS.length}`;
  renderFooter();
}

function renderFooter() {
  const footer = document.getElementById("units-footer");
  if (!UNITS.length) { footer.textContent = "—"; return; }
  const zero = UNITS.filter(u => u.fuzzy_pct === 0).length;
  const near = UNITS.filter(u => u.fuzzy_pct >= 90 && u.fuzzy_pct < 100).length;
  const complete = UNITS.filter(u => u.fuzzy_pct >= 100).length;
  footer.textContent = `${UNITS.length} TUs · ${complete} complete · ${near} near-match (90-99.99%) · ${zero} at 0% · click any tile to open on GitHub`;
}

function showTooltip(event, u) {
  const tt = document.getElementById("treemap-tooltip");
  const host = document.getElementById("treemap-host");
  const rect = host.getBoundingClientRect();
  const x = event.clientX - rect.left + 12;
  const y = event.clientY - rect.top + 12;
  tt.innerHTML = `<div class="tt-name">${escapeHtml(u.name)}</div>
    <div class="tt-meta">
      <b>${(u.fuzzy_pct || 0).toFixed(3)}%</b> fuzzy match<br>
      <b>${u.matched_functions}/${u.total_functions}</b> functions matched<br>
      <b>${(u.total_code || 0).toLocaleString()}</b> code bytes
      ${u.source_path ? `<br><span class="dim">${escapeHtml(u.source_path)}</span>` : ""}
    </div>`;
  // Keep tooltip inside the host
  const ttWidth = 280;
  const xClamped = Math.min(x, host.clientWidth - ttWidth - 8);
  tt.style.left = Math.max(0, xClamped) + "px";
  tt.style.top = Math.min(y, host.clientHeight - 100) + "px";
  tt.classList.add("visible");
}

function hideTooltip() {
  document.getElementById("treemap-tooltip").classList.remove("visible");
}

async function refreshUnits() {
  const data = await jget("/api/units");
  if (!data.units) return;
  if (data.mtime !== UNITS_MTIME) {
    UNITS = data.units;
    UNITS_MTIME = data.mtime;
    renderTreemap();
    renderModules();
  }
}

document.getElementById("unit-search").addEventListener("input", renderTreemap);
document.getElementById("unit-status").addEventListener("change", renderTreemap);
window.addEventListener("resize", () => { if (UNITS.length) renderTreemap(); });
async function refreshGoals() {
  const md = await tget("/api/goals");
  document.getElementById("goals").innerHTML = marked.parse(md);
}
async function refreshMwcc() {
  const md = await tget("/api/mwcc");
  document.getElementById("mwcc").innerHTML = marked.parse(md);
}

const GITHUB_REPO = "__GITHUB_REPO__";

async function refreshAll() {
  try { await Promise.all([refreshStatus(), refreshEta(), refreshLive(), refreshChart(), refreshTicks(), refreshCommits(), refreshAttempts(), refreshJournal(), refreshGoals(), refreshMwcc(), refreshUnits(), refreshKnowledge()]); }
  catch (e) { console.error(e); }
}

document.querySelectorAll("[data-range]").forEach(btn => {
  btn.addEventListener("click", () => {
    currentRange = btn.dataset.range;
    document.querySelectorAll("[data-range]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    refreshChart();
  });
});

document.querySelectorAll("[data-series]").forEach(cb => {
  cb.addEventListener("change", () => {
    SERIES_ENABLED[cb.dataset.series] = cb.checked;
    refreshChart();
  });
});

document.getElementById("pause-btn").addEventListener("click", async () => {
  const btn = document.getElementById("pause-btn");
  const isPaused = btn.textContent === "Resume";
  await fetch((isPaused ? "/api/resume" : "/api/pause") + authQ(), { method: "POST" });
  refreshStatus();
});

refreshAll();
setInterval(refreshStatus, 15000);
setInterval(refreshEta, 60000);
setInterval(refreshLive, 4000);
setInterval(refreshKnowledge, 30000);
setInterval(refreshTicks, 15000);
setInterval(refreshCommits, 30000);
setInterval(refreshAttempts, 30000);
setInterval(refreshJournal, 60000);
setInterval(refreshGoals, 60000);
setInterval(refreshMwcc, 60000);
setInterval(refreshChart, 60000);
setInterval(refreshUnits, 60000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    _check_auth(request)
    html = _INDEX_HTML.replace("__GITHUB_REPO__", SETTINGS.github_repo)
    return HTMLResponse(html)
