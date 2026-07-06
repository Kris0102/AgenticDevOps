"""add repos table + runs.repo_id (repo registry, GH #41) + canonical-URL backfill

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Strip a trailing `.git` and/or `/` — lessons/code_chunks are keyed by repo_url
# verbatim (ADR 0018), so pre-registry spelling variants split a repo's memory.
_CANONICALIZE = r"regexp_replace({col}, '(\.git)?/?$', '')"


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("repo_id", sa.String(), primary_key=True),
        sa.Column("repo_url", sa.String(), nullable=False, unique=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=True),
        sa.Column("default_branch", sa.String(), nullable=True),
        sa.Column("push_branch_override", sa.String(), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("runs", sa.Column("repo_id", sa.String(), nullable=True))
    for table in ("runs", "lessons", "code_chunks"):
        op.execute(f"UPDATE {table} SET repo_url = {_CANONICALIZE.format(col='repo_url')}")


def downgrade() -> None:
    # Backfill is not reversed — canonical URLs are valid pre-registry values too.
    op.drop_column("runs", "repo_id")
    op.drop_table("repos")
