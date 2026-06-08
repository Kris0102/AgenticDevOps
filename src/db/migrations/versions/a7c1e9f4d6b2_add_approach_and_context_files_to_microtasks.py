"""add_approach_and_context_files_to_microtasks

Revision ID: a7c1e9f4d6b2
Revises: d2e5f8a1b3c4
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c1e9f4d6b2'
down_revision: Union[str, Sequence[str], None] = 'd2e5f8a1b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('microtasks', sa.Column('approach', sa.Text(), nullable=True))
    op.add_column('microtasks', sa.Column('context_files', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('microtasks', 'context_files')
    op.drop_column('microtasks', 'approach')
