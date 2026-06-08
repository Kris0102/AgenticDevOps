"""drop_run_state_table

Revision ID: e3f6a9c2b5d8
Revises: a7c1e9f4d6b2
Create Date: 2026-06-08 00:00:00.000000

Removes the custom `run_state` blackboard-snapshot table — superseded by
LangGraph's native PostgresSaver checkpointer (`checkpoints`/`checkpoint_blobs`/
`checkpoint_writes`/`checkpoint_migrations`, created by `PostgresSaver.setup()`).
See docs/decisions/0005-bootstrap-outside-checkpointed-graph.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f6a9c2b5d8'
down_revision: Union[str, Sequence[str], None] = 'a7c1e9f4d6b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('run_state')


def downgrade() -> None:
    op.create_table(
        'run_state',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=True),
        sa.Column('state_json', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('run_id'),
    )
