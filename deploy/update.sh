#!/usr/bin/env bash
# Pull latest graffito-bot code + restart services.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "==> updating $ROOT"

git pull --ff-only origin main
.venv/bin/pip install -q -e .

systemctl restart graffito-daemon graffito-dashboard
systemctl status graffito-daemon graffito-dashboard --no-pager
