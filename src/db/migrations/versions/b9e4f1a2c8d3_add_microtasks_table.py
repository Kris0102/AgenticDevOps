"""add_microtasks_table

Revision ID: b9e4f1a2c8d3
Revises: c7aa814787cc
Create Date: 2026-06-06 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b9e4f1a2c8d3'
down_revision: Union[str, Sequence[str], None] = 'c7aa814787cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'microtasks',
        sa.Column('microtask_id', sa.String(), nullable=False),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('idx', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='PENDING'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('acceptance_criteria', sa.JSON(), nullable=True),
        sa.Column('dependencies', sa.JSON(), nullable=True),
        sa.Column('files_touched', sa.JSON(), nullable=True),
        sa.Column('test_strategy', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('microtask_id'),
    )
    op.create_index(op.f('ix_microtasks_run_id'), 'microtasks', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_microtasks_run_id'), table_name='microtasks')
    op.drop_table('microtasks')
