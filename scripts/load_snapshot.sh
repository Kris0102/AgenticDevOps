#!/usr/bin/env bash
# Restore DB from a snapshot created by snapshot_db.sh.
# WARNING: drops and recreates all tables — existing data is lost.
#
# Usage:
#   ./load_snapshot.sh scripts/snapshots/snapshot_20260606_120000.sql

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

SNAPSHOT="${1:-}"
if [[ -z "$SNAPSHOT" ]]; then
    echo "Usage: $0 <path-to-snapshot.sql>"
    echo ""
    echo "Available snapshots:"
    ls -lt "$SCRIPT_DIR/snapshots/"*.sql 2>/dev/null | awk '{print "  " $NF}' || echo "  (none)"
    exit 1
fi

if [[ ! -f "$SNAPSHOT" ]]; then
    echo "ERROR: File not found: $SNAPSHOT"
    exit 1
fi

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

_psql() {
    PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
        psql --username="$POSTGRES_USER" "$POSTGRES_DB" "$@"
}

echo ""
echo "This will REPLACE all DB data with: $SNAPSHOT"
echo ""
read -rp "Type 'yes' to confirm: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# Drop all tables then replay the dump
_psql <<'SQL'
DO $$ DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;
SQL

_psql --file="/dev/stdin" < "$SNAPSHOT"

echo ""
echo "Loaded: $SNAPSHOT"
