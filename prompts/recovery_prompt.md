# Recovery tick

The previous tick failed (non-zero exit, or hung). Before doing anything else:

1. **Read `state/last_tick.md`** to see the reason and any captured summary.
2. **Read the full transcript** at the path noted in `state/last_tick.md` (`logs/ticks/tick-<ts>.log`). Skim for the error.
3. **Check `git status` and `git log`** in the repo. Did the previous attempt leave half-finished edits in the working tree? Is the build currently broken at HEAD?

## Triage

- **If the working tree is dirty with broken edits** — decide: was this work salvageable or should you revert it? Use your judgment. If you revert, do it explicitly (`git checkout -- <file>`), don't `git clean -fd` (destructive).
- **If HEAD itself is broken** (build fails on a fresh checkout) — that means a prior push slipped past the build-gate, which shouldn't happen. Fix it: identify the offending commit (`git log -p`), make a fix-forward commit, push.
- **If the failure was tooling/environment** (claude binary not found, disk full, configure.py crashed) — write what you learned to `state/journal/<today>.md` and to `state/memory/` under a `feedback_*` entry, then set `next_tick.json` 30 min out.

**Do NOT immediately retry whatever the previous tick was attempting.** That's how loops happen. Diagnose first. If you're not sure, pause longer (set `next_tick.json` 2-4 hours out) so a human can intervene if needed.

## Then

Once the situation is understood and the repo is in a clean state, you can either:
- Pick something different and do a normal tick (see `tick_prompt.md`).
- Or, if the failure exposed a useful lesson, just journal it, set the next tick, and end — better to take a short tick after a failure than to charge into more work without thinking.
