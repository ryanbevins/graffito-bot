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
    _check_auth(request)
    rows = db.recent_commits(limit=limit)
    return JSONResponse([dict(r) for r in rows])


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
  section .body { padding: 12px 14px; max-height: 420px; overflow: auto; }
  section.chart .body { max-height: none; padding: 8px 4px; }
  section.tall .body { max-height: 700px; }
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
  <span class="stat"><b id="pct">—</b> fuzzy match</span>
  <span class="stat" id="delta">Δ24h: —</span>
  <span class="stat" id="fns">fns: —</span>
  <span class="stat" id="units">units: —</span>
  <span class="stat" id="daemon-pill"></span>
  <span class="stat" id="next-tick"></span>
  <span style="margin-left:auto"><button id="pause-btn" class="pause">Pause</button></span>
</header>
<main>
  <section class="full chart">
    <h2>
      Progress over time
      <span class="controls">
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

  <section class="tall">
    <h2>Goals</h2>
    <div class="body md" id="goals">…</div>
  </section>

  <section class="tall">
    <h2>Today's journal</h2>
    <div class="body md" id="journal">…</div>
  </section>
</main>
<footer>
  <span id="last-snapshot">—</span> · graffito-bot dashboard · auto-refresh 15s
</footer>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
let chart = null;
let currentRange = "7d";
const TOKEN = new URLSearchParams(location.search).get("token") || "";
function authQ() { return TOKEN ? ("?token=" + encodeURIComponent(TOKEN)) : ""; }
function authQ_amp() { return TOKEN ? ("&token=" + encodeURIComponent(TOKEN)) : ""; }

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

async function refreshStatus() {
  const s = await jget("/api/status");
  document.getElementById("pct").textContent = fmtPct(s.fuzzy_match_pct);
  const dEl = document.getElementById("delta");
  if (s.delta_24h === null) { dEl.textContent = "Δ24h: —"; }
  else {
    dEl.textContent = "Δ24h: " + fmtSign(s.delta_24h);
    dEl.className = "stat " + (s.delta_24h >= 0 ? "pos" : "neg");
  }
  document.getElementById("fns").textContent = "fns: " + (s.matched_functions ?? "—") + "/" + (s.total_functions ?? "—");
  document.getElementById("units").textContent = "units: " + (s.complete_units ?? "—") + "/" + (s.total_units ?? "—");

  const dpill = document.getElementById("daemon-pill");
  const d = s.daemon || {};
  const alive = d.alive ? "running" : "stopped";
  const cls = d.alive ? "green" : "red";
  let pillText = alive;
  if (d.paused) pillText += " · paused";
  if (d.regression_blocked) pillText += " · regression-block";
  dpill.innerHTML = '<span class="pill ' + cls + '">' + pillText + '</span>';

  if (s.next_tick) {
    document.getElementById("next-tick").textContent = "next: " + s.next_tick.wake_at + " (" + s.next_tick.set_by + ")";
  } else {
    document.getElementById("next-tick").textContent = "next: —";
  }
  document.getElementById("last-snapshot").textContent = "last snapshot: " + (s.last_snapshot_at || "—");

  const btn = document.getElementById("pause-btn");
  if (d.paused) { btn.textContent = "Resume"; btn.className = "resume"; }
  else { btn.textContent = "Pause"; btn.className = "pause"; }
}

async function refreshChart() {
  const data = await jget("/api/progress_series?range=" + currentRange + authQ_amp());
  const labels = data.map(d => d.ts);
  const fuzzy = data.map(d => d.fuzzy_pct);
  if (!chart) {
    const ctx = document.getElementById("chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: [{
        label: "Fuzzy match %",
        data: fuzzy,
        borderColor: "#58a6ff", backgroundColor: "rgba(88,166,255,0.08)",
        tension: 0.15, fill: true, pointRadius: 0,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "rgba(255,255,255,0.04)" } },
          y: { ticks: { color: "#8b949e", callback: v => v.toFixed(2) + "%" }, grid: { color: "rgba(255,255,255,0.04)" } },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = fuzzy;
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
    tr.innerHTML = `<td>${r.id}</td><td>${r.reason}</td><td>${r.started_at}</td><td>${r.ended_at || '—'}</td><td>${exitPill}</td><td class="msg">${(r.summary||'').replace(/</g,'&lt;')}</td>`;
    tb.appendChild(tr);
  }
}

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
    tr.innerHTML = `<td><a href="${url}" target="_blank">${short}</a></td><td>${r.pushed_at}</td><td class="right">${before}</td><td class="right">${after}</td><td class="msg">${(r.message||'').replace(/</g,'&lt;')}</td>`;
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
    tr.innerHTML = `<td>${r.recorded_at}</td><td>${r.tu}</td><td>${r.symbol||''}</td><td class="right">${r.before_pct||''}</td><td class="right">${r.after_pct||''}</td><td><span class="pill ${cls}">${out}</span></td>`;
    tb.appendChild(tr);
  }
}

async function refreshJournal() {
  const md = await tget("/api/journal/today");
  document.getElementById("journal").innerHTML = marked.parse(md);
}
async function refreshGoals() {
  const md = await tget("/api/goals");
  document.getElementById("goals").innerHTML = marked.parse(md);
}

const GITHUB_REPO = "__GITHUB_REPO__";

async function refreshAll() {
  try { await Promise.all([refreshStatus(), refreshChart(), refreshTicks(), refreshCommits(), refreshAttempts(), refreshJournal(), refreshGoals()]); }
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

document.getElementById("pause-btn").addEventListener("click", async () => {
  const btn = document.getElementById("pause-btn");
  const isPaused = btn.textContent === "Resume";
  await fetch((isPaused ? "/api/resume" : "/api/pause") + authQ(), { method: "POST" });
  refreshStatus();
});

refreshAll();
setInterval(refreshStatus, 15000);
setInterval(refreshTicks, 15000);
setInterval(refreshCommits, 30000);
setInterval(refreshAttempts, 30000);
setInterval(refreshJournal, 60000);
setInterval(refreshGoals, 60000);
setInterval(refreshChart, 60000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    _check_auth(request)
    html = _INDEX_HTML.replace("__GITHUB_REPO__", SETTINGS.github_repo)
    return HTMLResponse(html)
