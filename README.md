# graffito-bot

Autonomous decompilation agent for [ryanbevins/graffito](https://github.com/ryanbevins/graffito) — the Super Mario Sunshine (GMSJ01) matching decomp project. Runs `claude` as a subprocess in a heartbeat loop, lets it pick targets, edit C++ source, build, verify with objdiff, and push directly to `main`.

Modeled on the WallstreetTrading bot pattern: a daemon owns the loop and refreshes read-only state files; Claude owns persistent narrative state (`goals.md`, `journal/`, `notes/<tu>.md`); SQLite captures the audit trail; systemd supervises; a FastAPI dashboard renders progress over Tailscale.

## Layout

```
graffito/      Python package (daemon, tick runner, dashboard, CLI)
prompts/       system_context.md + tick_prompt.md + recovery_prompt.md
deploy/        install.sh, update.sh, systemd units
state/         bot's working memory (goals, journal, notes, refreshed inputs)
data/          SQLite database
logs/          daemon + per-tick transcripts
repo/          (not committed) cloned graffito repo, the bot's workspace
```

## Quick start (on the VPS)

```
bash deploy/install.sh
systemctl status graffito-daemon graffito-dashboard
# Dashboard: http://<vps>:8081/   (over Tailscale)
```

## Local development

```
python -m venv .venv
.venv/Scripts/activate    # Windows
pip install -e .
graffito --help
```
