#!/usr/bin/env bash
# Create a named snapshot of the DB via the running Postgres Docker container.
# Stores a plain SQL dump in scripts/snapshots/.
#
# The snapshot is SECRET-FREE by construction (safe to hand to someone else):
#   * app_settings      — data excluded entirely. It is one JSON blob holding provider
#                         api_keys + the GitHub token (Fernet `enc:`). The app re-seeds
#                         the row from the local .env on first read after restore.
#   * checkpoints/_blobs/_writes — data excluded. The checkpointed blackboard carries the
#                         resolved RunConfig incl. RAW provider api_keys, hex-encoded
#                         inside bytea — unstrippable by text post-processing. Cost:
#                         restored runs are not resumable (audit data — runs, events,
#                         artifacts, microtasks, lessons, code_chunks — is all kept).
#                         checkpoint_migrations data IS kept (schema version only).
#   * repos.token       — per-repo PATs (`enc:` strings) nulled by the sed pass below.
#   * safety net        — any plaintext GitHub PAT / sk-… key shape anywhere in the dump
#                         is replaced with [REDACTED].
#
# Usage:
#   ./snapshot_db.sh                  # auto-name: snapshot_<timestamp>.sql
#   ./snapshot_db.sh my_label         # snapshot_my_label.sql

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOT_DIR="$SCRIPT_DIR/snapshots"
mkdir -p "$SNAPSHOT_DIR"

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

echo "Creating secret-free snapshot → $OUTPUT"

# Sanitize pass (see header):
#   1. `enc:` Fernet tokens (only repos.token can still carry one after the data
#      exclusions) → \N, i.e. SQL NULL in COPY format.
#   2. Plaintext token shapes (legacy unencrypted values, or anything that leaked
#      into a payload) → [REDACTED].
PGPASSWORD="$POSTGRES_PASSWORD" docker exec -i "$CONTAINER" \
    pg_dump \
    --username="$POSTGRES_USER" \
    --no-owner \
    --no-privileges \
    --format=plain \
    --exclude-table-data=app_settings \
    --exclude-table-data=checkpoints \
    --exclude-table-data=checkpoint_blobs \
    --exclude-table-data=checkpoint_writes \
    "$POSTGRES_DB" \
    | sed -E \
        -e 's/enc:[A-Za-z0-9_=-]+/\\N/g' \
        -e 's/gh[pousr]_[A-Za-z0-9]{20,}/[REDACTED]/g' \
        -e 's/github_pat_[A-Za-z0-9_]{20,}/[REDACTED]/g' \
        -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED]/g' \
    > "$OUTPUT"

# Verify: no secret material survived
if grep -qE 'enc:[A-Za-z0-9_=-]+|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}' "$OUTPUT"; then
    echo "ERROR: secret-shaped values survived sanitization — snapshot deleted." >&2
    rm -f "$OUTPUT"
    exit 1
fi

echo "Snapshot saved: $OUTPUT ($(du -sh "$OUTPUT" | cut -f1))"
echo "Note: app_settings + run checkpoints are excluded — after restore, settings"
echo "re-seed from the local .env and old runs are view-only (not resumable)."
