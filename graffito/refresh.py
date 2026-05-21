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


_FOCUS_BODY = {
    "implementation": (
        "# Tick focus\n\n"
        "**Mode this tick: IMPLEMENTATION (bulk new matches).**\n\n"
        "## Target this tick: ≥ +0.5% fuzzy match\n\n"
        "On a ~3.6 MB project, +0.5% ≈ 18 KB of additional matched code. That's\n"
        "achievable in one tick if you pick the right target(s): a single mid-size\n"
        "0% TU, or 2-3 smaller ones, or finishing a partially-implemented TU with\n"
        "a substantial gap. Plan against this number — it's how the project moves.\n\n"
        "## Functional correctness > codegen perfection\n\n"
        "In this mode, the bar for shipping a function is **functional accuracy**,\n"
        "not byte-perfect match. Read the original asm carefully, write C++ that\n"
        "implements the same behavior, ship it. **You do NOT need to chase**:\n\n"
        "- Stack frame size mismatches (phantom inlines, +0x10 from startTimer, etc.)\n"
        "- Register coloring (r5↔r7, r27↔r31, f30/f31)\n"
        "- `addi rN, rM, 0` vs `mr rN, rM` encoding\n"
        "- Last-mile bool/BOOL casts, ternary→if rewrites, switch-defeats-fusion tweaks\n"
        "- Single missing instruction at the tail of an otherwise-correct function\n\n"
        "Those are INVESTIGATION-mode work. A TU that lands at 40–80% fuzzy with\n"
        "**correct logic** is a successful IMPLEMENTATION outcome — the next\n"
        "investigation tick will polish it the rest of the way.\n\n"
        "Hard rule still applies: **no fake matching**. No stack-padding tricks,\n"
        "no goto control flow, no `_pad[N]` arrays. Write honest C++ that does\n"
        "what the asm does, and move on.\n\n"
        "## Create files freely\n\n"
        "You have full authority to create new files when needed:\n\n"
        "- **New `.cpp` source files** for empty stubs.\n"
        "- **New `.hpp` headers** when a class is missing one, or when an existing\n"
        "  header needs a sibling. Past agents have been hesitant about creating\n"
        "  headers — don't be. If the project structure needs one, write it.\n"
        "- **Helper files under `tools/agent/`** for repeated query/filter pipelines.\n"
        "- **Notes/memory entries** as usual.\n\n"
        "Follow existing project conventions for header guards, include order, and\n"
        "naming, but don't ask permission to create a file the project obviously\n"
        "needs. Just create it and commit it.\n\n"
        "## Prefer (targets)\n\n"
        "- TUs whose `.cpp` file is empty or near-empty (`find src/ -name '*.cpp' -size -300c`).\n"
        "- TUs where `matched_code == 0` AND `total_code > 0` AND a source file exists\n"
        "  (logic is missing inside an existing skeleton — JDRDStageGroup-style wins).\n"
        "- Medium-fuzzy whole TUs (30–70%) where most of the gap is unwritten code.\n"
        "- 0% TUs that DON'T require huge rodata table reconstruction.\n\n"
        "## Defer (this tick)\n\n"
        "- Any of the codegen-detail issues listed under 'You do NOT need to chase'\n"
        "  above. Note them in `state/notes/<tu>.md` with what's left to polish, and\n"
        "  move to the next target. INVESTIGATION ticks will pick them up.\n"
        "- Single-function predicate tweaks. Unless they're inside a TU you're\n"
        "  already implementing, save for next tick.\n"
        "- Hypothesis testing for `docs/MWCC.md`. INVESTIGATION-mode work.\n\n"
        "## How this tick should end\n\n"
        "Ideal: ≥ +0.5% fuzzy gain, distributed across 1–3 TU implementations\n"
        "or finished stubs, all functionally correct. Notes filed for any leftover\n"
        "codegen polish.\n\n"
        "Acceptable: +0.2 to +0.5% with one large TU mostly implemented and\n"
        "documented well — sometimes the right move is depth on one big target.\n\n"
        "Bad: < +0.1% because the tick got absorbed by chasing a single function\n"
        "from 87% to 99% on stack-padding. That's not this mode's job.\n\n"
        "## Override allowed (with justification)\n\n"
        "If `state/progress.md` shows no good IMPLEMENTATION-mode target available\n"
        "(e.g. every empty stub depends on a missing class hierarchy), you may run\n"
        "this tick as INVESTIGATION instead — but say so explicitly in the journal\n"
        "entry and explain what you scanned. Don't silently revert to surgical fixes."
    ),
    "investigation": (
        "# Tick focus\n\n"
        "**Mode this tick: INVESTIGATION (codegen polish + MWCC theory).**\n\n"
        "This mode does **two** things that compound across the project:\n\n"
        "1. **Polish the leftovers from prior IMPLEMENTATION ticks.** Each\n"
        "   IMPLEMENTATION tick ships functionally-correct TUs that land at\n"
        "   40–80% match, with codegen-polish work deferred. Your job here is\n"
        "   to push those toward 100% — bool/BOOL casts, ternary→if rewrites,\n"
        "   switch-defeats-fusion, `__fabsf`/`.value` bypasses, intermediate\n"
        "   `bool match` patterns, header signature audits.\n\n"
        "2. **Test hypotheses and grow `docs/MWCC.md`.** Confirm or refute\n"
        "   open questions; promote hypotheses to *Settled* with citations.\n\n"
        "Per-target gain is small but cumulative — and every Settled rule you\n"
        "add lowers the cost of the next IMPLEMENTATION tick.\n\n"
        "## Where to find polish work\n\n"
        "Start by re-reading the last 2-3 journal entries and grep your own\n"
        "notes (`state/notes/`) for phrases like 'leftover polish', 'codegen\n"
        "fix', 'still missing the bool cast', '40%', '60%', '80%'. Recent\n"
        "IMPLEMENTATION-mode TUs are your highest-yield polish targets.\n\n"
        "Then sweep with `report.json` filters: functions at 40-95% fuzzy\n"
        "where the diff against asm is small (single-block, last-mile).\n\n"
        "## Prefer (targets)\n\n"
        "- TUs the previous IMPLEMENTATION tick shipped at <100% — close them.\n"
        "- Apply a known `state/memory/` pattern across many TUs at once.\n"
        "- Header-signature sweeps (`void`-declared predicates that return BOOL).\n"
        "- Surgical near-match fixes (40-95%) where the lever is known.\n"
        "- Test one `docs/MWCC.md` *Hypotheses under investigation* with the\n"
        "  experiment that would confirm or refute, then promote/refute.\n"
        "- Pick an *Open question* whose experiment design is clear; run it.\n\n"
        "## Defer (this tick)\n\n"
        "- Writing brand-new TU implementations from scratch — IMPLEMENTATION\n"
        "  work; the next tick will handle it.\n"
        "- Empty-stub greenfield scans.\n"
        "- Long ratholes on already-documented currently-hard patterns. If a\n"
        "  function's blocker is listed in `docs/MWCC.md` *Open questions*\n"
        "  and you don't have a fresh experiment idea, skip it.\n\n"
        "## How this tick should end\n\n"
        "Ideal: 4–10 small commits closing leftovers from recent IMPLEMENTATION\n"
        "ticks, OR one new Settled rule in `docs/MWCC.md` with citations to ≥2\n"
        "TUs, OR a thorough investigation that moves a hypothesis.\n\n"
        "Acceptable: A single deep investigation that produces a useful note\n"
        "or memory entry even if no match-% gain — provided it enables future\n"
        "ticks.\n\n"
        "Bad: Burning the tick on one register-coloring puzzle that's already\n"
        "documented as currently-hard. Skip those.\n\n"
        "## Override allowed (with justification)\n\n"
        "If the previous IMPLEMENTATION tick left a half-finished whole-TU\n"
        "effort that needs more *implementation*, you may run this tick as\n"
        "IMPLEMENTATION to finish — but say so in the journal."
    ),
}


def write_tick_focus_md(mode: str) -> None:
    SETTINGS.tick_focus_md.write_text(
        _FOCUS_BODY.get(mode, _FOCUS_BODY["investigation"]) + "\n",
        encoding="utf-8",
    )


def write_all(reason: str, prev_pct: float | None = None, mode: str | None = None) -> None:
    write_progress_md(prev_pct=prev_pct)
    write_git_status_md()
    write_last_tick_md()
    write_tick_reason_md(reason)
    if mode:
        write_tick_focus_md(mode)


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
