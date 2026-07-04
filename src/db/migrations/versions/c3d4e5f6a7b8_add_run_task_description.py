"""add runs.task_description (dashboard "what was this run about")

Nullable — old runs are lazily backfilled from their checkpoint on first dashboard read.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("task_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "task_description")
