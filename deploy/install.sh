#!/usr/bin/env bash
# One-shot deploy for graffito-bot on the reused WallstreetTrading VPS.
# Run from /opt/graffito as root. Idempotent: safe to re-run after `git pull`.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "==> ROOT=$ROOT"

# ── 1. Terminate the existing WallstreetTrading services ────────────────────
echo "==> stopping trader services (if running)"
systemctl stop trader-daemon trader-dashboard 2>/dev/null || true
systemctl disable trader-daemon trader-dashboard 2>/dev/null || true

echo "==> defensive: killing any lingering trader/claude processes"
pkill -f '/root/trader/.venv/bin/trader' 2>/dev/null || true
pkill -f 'trader daemon' 2>/dev/null || true
# Don't pkill 'claude -p' here — we're about to need claude for ourselves and
# orphan trader-claude processes will have already been reaped by the trader
# daemon shutdown. If any survive, they're ours now anyway.

if pgrep -f '/root/trader' > /dev/null; then
  echo "ERROR: trader still running after stop+kill. Aborting." >&2
  exit 1
fi

# ── 2. Ensure system deps ───────────────────────────────────────────────────
echo "==> ensuring build deps (idempotent apt-get)"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git build-essential ninja-build libxml2-utils

# ── 3. Python venv + package install ────────────────────────────────────────
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "==> creating venv"
  python3 -m venv .venv
fi
echo "==> installing graffito package"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

# ── 4. Ensure runtime dirs ──────────────────────────────────────────────────
mkdir -p state/journal state/notes state/memory data logs/ticks

# ── 5. Clone the graffito repo (the bot's workspace) ────────────────────────
GRAFFITO_REMOTE="${GRAFFITO_REMOTE:-git@github-graffito:ryanbevins/graffito.git}"

if [[ ! -d "$ROOT/repo/.git" ]]; then
  echo "==> cloning $GRAFFITO_REMOTE into repo/"
  git clone "$GRAFFITO_REMOTE" "$ROOT/repo"
else
  echo "==> repo/ exists; fetching latest"
  git -C "$ROOT/repo" remote set-url origin "$GRAFFITO_REMOTE"
  git -C "$ROOT/repo" fetch origin main
  git -C "$ROOT/repo" checkout main
  git -C "$ROOT/repo" pull --ff-only origin main || true
fi

# ── 6. Initial configure.py to download MWCC + dtk + objdiff ────────────────
echo "==> running configure.py (downloads MWCC, dtk, objdiff if missing)"
(cd "$ROOT/repo" && python3 configure.py --version GMSJ01)

# ── 7. .env check ──────────────────────────────────────────────────────────
if [[ ! -f "$ROOT/.env" ]]; then
  echo "==> writing default .env"
  cat > "$ROOT/.env" <<'EOF'
# graffito-bot environment.
# Either ANTHROPIC_API_KEY (Claude API) or `claude /login` (subscription) is fine.
# ANTHROPIC_API_KEY=
CLAUDE_MODEL=opus
GITHUB_REPO=ryanbevins/graffito
DASHBOARD_PORT=8081
# Optional: token for the dashboard. If unset, only Tailscale network ACL gates access.
# DASHBOARD_TOKEN=
EOF
  chmod 600 "$ROOT/.env"
fi

# ── 8. systemd units ────────────────────────────────────────────────────────
echo "==> installing systemd units"
cp deploy/graffito-daemon.service /etc/systemd/system/
cp deploy/graffito-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable graffito-daemon.service graffito-dashboard.service

# ── 9. graffito init (creates DB + stub files; idempotent) ──────────────────
echo "==> running graffito init"
.venv/bin/graffito init

echo
echo "==> install complete."
echo "    start:      systemctl start graffito-daemon graffito-dashboard"
echo "    status:     systemctl status graffito-daemon graffito-dashboard"
echo "    journal:    journalctl -u graffito-daemon -f"
echo "    dashboard:  http://$(tailscale ip -4 2>/dev/null | head -1):8081"
