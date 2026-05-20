# Tick

This is one work cycle. Take it at your own pace — no timeout, no deadline. Quality over quantity.

1. **Read state.** `state/progress.md`, `state/git_status.md`, `state/last_tick.md`, `state/tick_reason.md`, `state/goals.md`, today's `state/journal/<date>.md` if it exists. If you have an active investigation, also re-read the relevant `state/notes/<tu>.md`.

2. **Re-read authoritative docs.** `CLAUDE.md` and `AGENTS.md` at the repo root. They evolve.

3. **Decide.** Continue a prior investigation, pick a new TU, or pause (set a longer `next_tick.json` and update `goals.md` with what you'd want to come back to). You pick. Anything in `state/progress.md`'s non-matching list is fair game; so are TUs you've worked on before.

4. **Work the matching loop.**
   - Read the original asm under `build/GMSJ01/asm/`.
   - Edit `src/...` and `include/...` minimally and surgically.
   - Build: `python configure.py && ninja`.
   - Diff: `build/tools/objdiff-cli` per AGENTS.md, or `python tools/decomp-diff.py -u <unit> -d <symbol>` if available.
   - Iterate. The loop can take many rounds — that's expected. Don't rush.

5. **Commit incrementally.** When a change improves a function (or otherwise produces signal worth keeping — a useful note, a corrected header, a documented dead-end), commit it with a descriptive message and `git push origin main`. The build-gate will reject pushes that don't build. Don't push if the build is broken; fix locally first.

6. **Journal.** Append to `state/journal/<today>.md`: what you tried, what changed, what surprised you, what you'd do next. One paragraph is fine. Bullet points are fine. Be honest about failures.

7. **Notes.** If you've gathered structural understanding of a TU that's worth preserving (class hierarchy, vtable layout, calling conventions, ordering constraints), write or update `state/notes/<tu>.md` and commit it.

8. **Memory.** If you've learned something *generally useful* (a new MWCC quirk, a recurring pattern, a tooling tip), add a memory entry under `state/memory/` and link it from `state/memory/MEMORY.md`. Don't conflate this with notes — notes are TU-specific, memory is cross-TU.

9. **Schedule next tick.** Write `state/next_tick.json` with `wake_at` 30 minutes from now (default) — or longer if you want to give CI/review time, shorter if you have an obvious next step. Format example: `{"wake_at": "2026-05-21T15:00:00+00:00", "reason": "continue MapObjSirena", "set_at": "...", "set_by": "claude"}`.

That's the cycle. Be deliberate. The progress graph rewards consistency, not heroics.
