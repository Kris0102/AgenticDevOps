"""add runs.sandbox_image (which image the run's agents ran in, ADR 0039)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Denormalized off the checkpointed SandboxSpec so the run list stays one SQL query.
    # Nullable and NOT backfilled: every existing run used the generic agentic-sandbox:0.1,
    # which is exactly what NULL renders as, so a backfill would add rows of no information.
    op.add_column("runs", sa.Column("sandbox_image", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "sandbox_image")
