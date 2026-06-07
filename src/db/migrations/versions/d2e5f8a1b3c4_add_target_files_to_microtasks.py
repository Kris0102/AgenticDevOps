"""add_target_files_to_microtasks

Revision ID: d2e5f8a1b3c4
Revises: b9e4f1a2c8d3
Create Date: 2026-06-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2e5f8a1b3c4'
down_revision: Union[str, Sequence[str], None] = 'b9e4f1a2c8d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('microtasks', sa.Column('target_files', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('microtasks', 'target_files')
