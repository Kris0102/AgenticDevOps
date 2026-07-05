"""add app_settings table + runs.llm_provider/llm_model (settings hub)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("runs", sa.Column("llm_provider", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("llm_model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "llm_model")
    op.drop_column("runs", "llm_provider")
    op.drop_table("app_settings")
