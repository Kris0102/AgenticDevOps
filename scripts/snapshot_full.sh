#!/usr/bin/env bash
# FULL DB snapshot — INCLUDES SECRETS. For personal backup/restore only.
#
# Unlike snapshot_db.sh (sanitized, shareable), this dumps EVERYTHING:
# app_settings (provider api_keys + GitHub token, Fernet `enc:`), repos.token,
# and the LangGraph checkpoints (which carry RAW resolved provider keys in bytea).
# It also copies the Fernet key (.settings_key) next to the dump — without it a
# restored DB's `enc:` secrets are undecryptable on another machine.
#
# Output goes to scripts/snapshot_secrets/ which is .gitignored.
# NEVER commit or share these files.
#
# Usage:
#   ./snapshot_full.sh                # auto-name: snapshot_<timestamp>.sql
#   ./snapshot_full.sh my_label       # snapshot_my_label.sql
#
# Restore (full):
#   cp scripts/snapshot_secrets/snapshot_<label>.settings_key .settings_key
#   ./scripts/load_snapshot.sh scripts/snapshot_secrets/snapshot_<label>.sql

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOT_DIR="$SCRIPT_DIR/snapshot_secrets"
mkdir -p "$SNAPSHOT_DIR"
chmod 700 "$SNAPSHOT_DIR"

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

LABEL="${1:-$(date +%Y%m%d_%H%M%S)}"
OUTPUT="$SNAPSHOT_DIR/snapshot_${LABEL}.sql"

echo "Creating FULL snapshot (includes secrets) → $OUTPUT"

PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
    pg_dump \
    --username="$POSTGRES_USER" \
    --no-owner \
    --no-privileges \
    --format=plain \
    "$POSTGRES_DB" > "$OUTPUT"
chmod 600 "$OUTPUT"

# Bundle the Fernet key — the DB's `enc:` secrets are useless without it.
if [[ -f "$ROOT_DIR/.settings_key" ]]; then
    cp "$ROOT_DIR/.settings_key" "$SNAPSHOT_DIR/snapshot_${LABEL}.settings_key"
    chmod 600 "$SNAPSHOT_DIR/snapshot_${LABEL}.settings_key"
    KEY_NOTE="+ snapshot_${LABEL}.settings_key (Fernet key)"
else
    KEY_NOTE="(no .settings_key file — secrets encrypted via SETTINGS_ENCRYPTION_KEY env; back that up yourself)"
fi

echo "Snapshot saved: $OUTPUT ($(du -sh "$OUTPUT" | cut -f1)) $KEY_NOTE"
echo "WARNING: contains API keys/tokens — never commit or share. Shareable dumps: ./snapshot_db.sh"
