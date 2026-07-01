"""add lessons table (per-repo failure memory, ADR 0018)

Durable, embedded, per-repo lessons learned from confirmed failures (reviewer plan
rejections that were addressed; reflector diagnoses that led to a pass). pgvector for
semantic retrieval — the `vector` extension already exists (created with code_chunks).

Revision ID: b2c3d4e5f6a7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "lessons",
        sa.Column("lesson_id", sa.String(), primary_key=True),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("failure_type", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("guidance", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lessons_repo_url", "lessons", ["repo_url"])
    op.create_index("ix_lessons_run_id", "lessons", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_lessons_run_id", table_name="lessons")
    op.drop_index("ix_lessons_repo_url", table_name="lessons")
    op.drop_table("lessons")
