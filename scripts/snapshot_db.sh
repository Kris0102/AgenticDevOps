#!/usr/bin/env bash
# Create a named snapshot of the DB via the running Postgres Docker container.
# Stores a plain SQL dump in scripts/snapshots/.
#
# Usage:
#   ./snapshot_db.sh                  # auto-name: snapshot_<timestamp>.sql
#   ./snapshot_db.sh my_label         # snapshot_my_label.sql

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOT_DIR="$SCRIPT_DIR/snapshots"
mkdir -p "$SNAPSHOT_DIR"

# Load .env
if [[ -f "$ROOT_DIR/.env" ]]; then
    export $(grep -v '^#' "$ROOT_DIR/.env" | xargs)
fi

POSTGRES_USER="${POSTGRES_USER:-user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}"
POSTGRES_DB="${POSTGRES_DB:-agentic_devops}"
CONTAINER="${POSTGRES_CONTAINER:-deploy-postgres-1}"

LABEL="${1:-$(date +%Y%m%d_%H%M%S)}"
OUTPUT="$SNAPSHOT_DIR/snapshot_${LABEL}.sql"

echo "Creating snapshot → $OUTPUT"

PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
    pg_dump \
    --username="$POSTGRES_USER" \
    --no-owner \
    --no-privileges \
    --format=plain \
    "$POSTGRES_DB" > "$OUTPUT"

echo "Snapshot saved: $OUTPUT ($(du -sh "$OUTPUT" | cut -f1))"
