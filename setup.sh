#!/usr/bin/env bash
# One-shot environment setup for AgenticDevOps on a fresh machine.
# Does NOT start the app — run ./run.sh afterwards.
#
# Usage:
#   ./setup.sh                          # deps + .env + DB left as-is (migrations only)
#   ./setup.sh latest                   # ... + restore the newest scripts/snapshots/*.sql
#   ./setup.sh scripts/snapshots/snapshot_<label>.sql   # ... + restore that snapshot
#
# Steps:
#   1. .env       — created with working defaults if missing (no secrets; enter API
#                   keys / GitHub token in the dashboard Settings page after start)
#   2. Python     — venv created if missing + requirements installed
#   3. Dashboard  — npm install
#   4. Docker     — Postgres/pgAdmin/Grafana up + sandbox base image build
#   5. DB         — optional snapshot restore (asks before overwriting existing data),
#                   then Alembic migrations

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

SNAPSHOT_ARG="${1:-}"

step() { echo ""; echo "==> $*"; }

# ---------------------------------------------------------------- 0. preflight
if ! docker info > /dev/null 2>&1; then
    echo "Docker is not running. Please start Docker and try again."
    exit 1
fi

if docker compose version > /dev/null 2>&1; then
    DC=(docker compose)
else
    DC=(docker-compose)
fi

# ---------------------------------------------------------------- 1. .env
step ".env"
if [[ -f .env ]]; then
    echo ".env already exists — keeping it."
else
    cat > .env <<'ENV'
# Postgres — docker-compose initializes the DB from these; the app (db.py) and
# Alembic build the connection URL from them. Single source of truth.
# NOTE: Postgres only applies creds on FIRST init of an empty data volume.
POSTGRES_USER=user
POSTGRES_PASSWORD=password
POSTGRES_DB=agentic_devops
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# --- Secrets (leave empty here) -------------------------------------------
# Provider API keys and the GitHub token are managed in the dashboard
# Settings page (encrypted at rest). Values below only SEED settings on
# first boot of an empty settings table.
CLUSTER_API_KEY=
GITHUB_TOKEN=

# --- LLM seed defaults (UI-managed after first boot, ADR 0020) -------------
LLM_MODEL=google/gemma-4-31B-it
USE_TOOLS=true

# --- Logging ----------------------------------------------------------------
LOG_LEVEL=INFO
LOG_JSON=false
LOG_COLORS=auto
ENV
    echo "Created .env with defaults. Enter API keys in the dashboard Settings page."
fi

# Load it for the DB steps below
set -a
# shellcheck disable=SC1091
source .env
set +a
POSTGRES_USER="${POSTGRES_USER:-user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}"
POSTGRES_DB="${POSTGRES_DB:-agentic_devops}"
CONTAINER="${POSTGRES_CONTAINER:-deploy-postgres-1}"

_psql() {
    PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
        psql --username="$POSTGRES_USER" "$POSTGRES_DB" "$@"
}

# ---------------------------------------------------------------- 2. python deps
step "Python venv + dependencies"
if [[ ! -d venv ]]; then
    python3 -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
echo "Python dependencies installed."

# ---------------------------------------------------------------- 3. dashboard deps
step "Dashboard dependencies"
(cd dashboard && npm install --no-audit --no-fund)

# ---------------------------------------------------------------- 4. docker services
step "Docker services (Postgres, pgAdmin, Grafana)"
"${DC[@]}" -f deploy/docker-compose.yml up -d

step "Sandbox base image"
"${DC[@]}" -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder

step "Waiting for Postgres"
for i in $(seq 1 30); do
    if docker exec "$CONTAINER" pg_isready --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" > /dev/null 2>&1; then
        break
    fi
    if [[ "$i" == 30 ]]; then
        echo "Postgres did not become ready in time." >&2
        exit 1
    fi
    sleep 1
done
echo "Postgres is ready."

# ---------------------------------------------------------------- 5. database
step "Database"
if [[ -n "$SNAPSHOT_ARG" ]]; then
    if [[ "$SNAPSHOT_ARG" == "latest" ]]; then
        SNAPSHOT="$(ls -t scripts/snapshots/snapshot_*.sql 2>/dev/null | head -1 || true)"
        if [[ -z "$SNAPSHOT" ]]; then
            echo "No snapshots found in scripts/snapshots/." >&2
            exit 1
        fi
    else
        SNAPSHOT="$SNAPSHOT_ARG"
        if [[ ! -f "$SNAPSHOT" ]]; then
            echo "Snapshot not found: $SNAPSHOT" >&2
            exit 1
        fi
    fi

    # Existing data? Ask before overwriting.
    RUN_COUNT="$(_psql -tAc "SELECT (SELECT count(*) FROM runs) + (SELECT count(*) FROM events)" 2>/dev/null | tr -d '[:space:]' || true)"
    RUN_COUNT="${RUN_COUNT:-0}"
    RESTORE=yes
    if [[ "$RUN_COUNT" != "0" ]]; then
        echo "The database already contains data ($RUN_COUNT run/event rows)."
        read -rp "Overwrite it with $SNAPSHOT? [y/N] " ANSWER
        if [[ "${ANSWER,,}" != "y" && "${ANSWER,,}" != "yes" ]]; then
            RESTORE=no
            echo "Keeping existing data — snapshot NOT restored."
        fi
    fi
    if [[ "$RESTORE" == "yes" ]]; then
        echo yes | ./scripts/load_snapshot.sh "$SNAPSHOT"
    fi
else
    echo "No snapshot given — leaving the database as-is (migrations only)."
fi

echo "Running Alembic migrations..."
venv/bin/alembic upgrade head

step "Setup complete"
echo "Start the app with: ./run.sh"
