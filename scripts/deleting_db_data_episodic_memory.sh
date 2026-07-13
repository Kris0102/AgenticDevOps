#!/usr/bin/env bash
# Wipe all run data from the DB — keeps schema intact (tables stay, data gone).
# Use this for a clean PoC re-run. Does NOT drop tables or touch migrations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env (source so inline `# comments` and quoted values are handled correctly)
if [[ -f "$ROOT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "$ROOT_DIR/.env"
    set +a
fi

POSTGRES_USER="${POSTGRES_USER:-user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}"
POSTGRES_DB="${POSTGRES_DB:-agentic_devops}"
CONTAINER="${POSTGRES_CONTAINER:-deploy-postgres-1}"

echo ""
echo "This will DELETE ALL DATA from:"
echo "  runs, microtasks, events, artifacts, sandboxes, code_chunks, lessons, checkpoints"
echo ""
echo "KEPT: repos (registry) + app_settings (provider/GitHub config) — so you don't"
echo "      re-enter repos or secrets. Schema/migrations are NOT affected."
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
TRUNCATE TABLE code_chunks   RESTART IDENTITY CASCADE;
-- Cross-run failure memory (ADR 0018): its run_id references would dangle after a run
-- wipe, so clear it too for a true clean slate. repos + app_settings are intentionally
-- left intact (registry + provider/GitHub config).
TRUNCATE TABLE lessons       RESTART IDENTITY CASCADE;
TRUNCATE TABLE checkpoints   RESTART IDENTITY CASCADE;
TRUNCATE TABLE checkpoint_writes RESTART IDENTITY CASCADE;
TRUNCATE TABLE checkpoint_blobs      RESTART IDENTITY CASCADE;
TRUNCATE TABLE runs          RESTART IDENTITY CASCADE;
SQL

echo ""
echo "Done — all run data wiped. Schema intact."
