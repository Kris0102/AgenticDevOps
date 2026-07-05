"""add artifacts.microtask_id — artifact→microtask context for the dashboard

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-05
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("microtask_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifacts", "microtask_id")
