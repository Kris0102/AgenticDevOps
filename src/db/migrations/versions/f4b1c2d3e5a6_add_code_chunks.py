"""add_code_chunks (Context Engine / semantic memory)

Enables the pgvector `vector` extension and creates the `code_chunks` table that
backs the Context Engine's semantic search. Embedding dimension 384 matches
all-MiniLM-L6-v2 (db/embedder.py / models.EMBEDDING_DIM).

Revision ID: f4b1c2d3e5a6
Revises: e3f6a9c2b5d8
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = 'f4b1c2d3e5a6'
down_revision: Union[str, Sequence[str], None] = 'e3f6a9c2b5d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    # Requires the pgvector/pgvector:pg15 Postgres image (see deploy/docker-compose.yml).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "code_chunks",
        sa.Column("chunk_id", sa.String(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("git_sha", sa.String(), nullable=True),
        sa.Column("filepath", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("chunk_type", sa.String(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_index("ix_code_chunks_namespace", "code_chunks", ["namespace"])
    op.create_index("ix_code_chunks_run_id", "code_chunks", ["run_id"])
    op.create_index("ix_code_chunks_repo_url", "code_chunks", ["repo_url"])
    # Composite filter for scoped lookups (baseline by repo+sha, workspace by run).
    op.create_index(
        "ix_code_chunks_scope",
        "code_chunks",
        ["namespace", "run_id", "repo_url", "git_sha"],
    )
    # Approximate-NN index for cosine similarity ordering.
    op.execute(
        "CREATE INDEX ix_code_chunks_embedding_hnsw ON code_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_code_chunks_embedding_hnsw")
    op.drop_index("ix_code_chunks_scope", table_name="code_chunks")
    op.drop_index("ix_code_chunks_repo_url", table_name="code_chunks")
    op.drop_index("ix_code_chunks_run_id", table_name="code_chunks")
    op.drop_index("ix_code_chunks_namespace", table_name="code_chunks")
    op.drop_table("code_chunks")
    # Leave the `vector` extension installed — other objects may rely on it.
