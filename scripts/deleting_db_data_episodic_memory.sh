#!/usr/bin/env bash
# Wipe all run data from the DB — keeps schema intact (tables stay, data gone).
# Use this for a clean PoC re-run. Does NOT drop tables or touch migrations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env
if [[ -f "$ROOT_DIR/.env" ]]; then
    export $(grep -v '^#' "$ROOT_DIR/.env" | xargs)
fi

POSTGRES_USER="${POSTGRES_USER:-user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}"
POSTGRES_DB="${POSTGRES_DB:-agentic_devops}"
CONTAINER="${POSTGRES_CONTAINER:-deploy-postgres-1}"

echo ""
echo "This will DELETE ALL DATA from:"
echo "  runs, microtasks, events, artifacts, sandboxes, run_state"
echo ""
echo "Schema (tables/columns) is preserved. Migrations are NOT affected."
echo ""
read -rp "Type 'yes' to confirm: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
    psql --username="$POSTGRES_USER" "$POSTGRES_DB" <<'SQL'
TRUNCATE TABLE microtasks    RESTART IDENTITY CASCADE;
TRUNCATE TABLE events        RESTART IDENTITY CASCADE;
TRUNCATE TABLE artifacts     RESTART IDENTITY CASCADE;
TRUNCATE TABLE sandboxes     RESTART IDENTITY CASCADE;
TRUNCATE TABLE run_state     RESTART IDENTITY CASCADE;
TRUNCATE TABLE runs          RESTART IDENTITY CASCADE;
SQL

echo ""
echo "Done — all run data wiped. Schema intact."
