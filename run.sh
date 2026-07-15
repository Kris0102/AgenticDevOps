#!/usr/bin/env bash
# Start AgenticDevOps — API (uvicorn :8000) + dashboard dev server.
# Verifies ./setup.sh has been run first by checking the artifacts it produces
# (no stamp file — the real state is the check).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---------------------------------------------------------------- setup check
MISSING=()
[[ -f .env ]]                        || MISSING+=(".env (created by setup)")
[[ -x venv/bin/uvicorn ]]            || MISSING+=("venv + Python deps (venv/bin/uvicorn)")
[[ -d dashboard/node_modules ]]      || MISSING+=("dashboard/node_modules (npm install)")

if (( ${#MISSING[@]} > 0 )); then
    echo "Setup incomplete — missing:"
    for m in "${MISSING[@]}"; do echo "  - $m"; done
    echo ""
    echo "Run ./setup.sh first (optionally with a DB snapshot):"
    echo "  ./setup.sh latest"
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo "Docker is not running. Please start Docker and try again."
    exit 1
fi

if docker compose version > /dev/null 2>&1; then
    DC=(docker compose)
else
    DC=(docker-compose)
fi

# Idempotent — brings Postgres/pgAdmin/Grafana back up after a reboot, no-op otherwise.
"${DC[@]}" -f deploy/docker-compose.yml up -d

# ---------------------------------------------------------------- start
npx concurrently \
  --shell "bash" \
  --names "API,NPM" \
  --prefix-colors "blue,green" \
  "source venv/bin/activate && export PYTHONPATH=src && uvicorn src.api.app:app --port 8000" \
  "cd dashboard && npm run dev"
