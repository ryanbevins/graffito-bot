# Tick

This is one work cycle. Take it at your own pace — no timeout, no deadline. Quality over quantity.

1. **Read `state/tick_focus.md` FIRST.** It tells you whether this is an IMPLEMENTATION tick (bulk new matches, write whole TUs) or an INVESTIGATION tick (surgical fixes + MWCC theory). The mode is binding by default — your target choice in step 3 must match it unless you have a strong reason to override.

2. **Read the rest of state.** `state/progress.md`, `state/git_status.md`, `state/last_tick.md`, `state/tick_reason.md`, `state/goals.md`, today's `state/journal/<date>.md` if it exists. If you have an active investigation, also re-read the relevant `state/notes/<tu>.md`.

3. **Re-read authoritative docs.** `CLAUDE.md`, `AGENTS.md`, and `docs/MWCC.md` at the repo root. They evolve — the last one you write to yourself.

4. **Decide, constrained by the mode.**
   - **IMPLEMENTATION mode:** read `state/campaign_tu.md`. If empty / `functionally_complete` / `blocked` → pick ONE new TU, write it to `campaign_tu.md`, **begin work this same tick** (don't pick-and-exit). Otherwise → continue the existing campaign TU. Stop criteria: +0.2% fuzzy gain since tick start, OR campaign TU functionally complete, OR genuinely blocked. Related/dependency TUs are allowed under the carve-out in `tick_focus.md`.
   - **INVESTIGATION mode:** priority 1 is closing leftover codegen polish on the active campaign TU and recent IMPLEMENTATION ticks' shipped TUs. Priority 2 is memory-pattern sweeps + `docs/MWCC.md` hypothesis testing.
   - If you decide to override the mode, journal a one-sentence justification.

5. **Work the matching loop.**
   - Read the original asm under `build/GMSJ01/asm/`.
   - Edit `src/...` and `include/...` minimally and surgically.
   - Build: `python configure.py && ninja`.
   - Diff: `build/tools/objdiff-cli` per AGENTS.md, or `python tools/decomp-diff.py -u <unit> -d <symbol>` if available.
   - Iterate. The loop can take many rounds — that's expected. Don't rush.
   - **Build your own tools when it helps.** If you're running the same multi-step pipeline twice, write it as a script in `tools/agent/` and commit it. See `system_context.md` for conventions.

6. **Commit incrementally.** When a change improves a function (or otherwise produces signal worth keeping — a useful note, a corrected header, a documented dead-end), commit it with a descriptive message and `git push origin main`. The build-gate will reject pushes that don't build. Don't push if the build is broken; fix locally first.

7. **Journal — this is where tick stories go.** Append to `state/journal/<today>.md`: what you tried, what changed, what surprised you, what you'd do next. **Lead with the tick's mode and headline outcome** (e.g. "tick 12 [IMPLEMENTATION]: wrote SunGlass.cpp 0→100%, finished MapObjGate logic +3 fns"). One paragraph + bullets is fine. Be honest about failures. **Do not** mirror this content into `goals.md` — that file is strategy, not log.

8. **Notes.** If you've gathered structural understanding of a TU that's worth preserving (class hierarchy, vtable layout, calling conventions, ordering constraints), write or update `state/notes/<tu>.md` and commit it.

9. **Memory.** If you've learned something *generally useful* (a new MWCC quirk, a recurring pattern, a tooling tip), add a memory entry under `state/memory/` and link it from `state/memory/MEMORY.md`. Don't conflate this with notes — notes are TU-specific, memory is cross-TU.

10. **Goals audit (mandatory, every tick).** Open `state/goals.md`. If your strategic view *actually changed* this tick, **edit** the relevant section in place — don't append. If the file has grown past ~80 lines, contains "New observations from tick N" / "Updates from <date>" sections, or duplicate priority lists, **clean it up before ending the tick**. Move tick-log content into the journal, cross-TU lessons into memory, compiler hypotheses into `docs/MWCC.md`. A clean goals.md after every tick is a hard requirement, not a nice-to-have.

11. **Schedule next tick.** Write `state/next_tick.json` with `wake_at` 30 minutes from now (default) — or longer if you want to give CI/review time, shorter if you have an obvious next step. Format example: `{"wake_at": "2026-05-21T15:00:00+00:00", "reason": "continue MapObjSirena", "set_at": "...", "set_by": "claude"}`. The *mode* of the next tick is set by the daemon (auto-alternates) — don't try to override it in `next_tick.json`.

That's the cycle. Be deliberate. The progress graph rewards consistency, not heroics.
