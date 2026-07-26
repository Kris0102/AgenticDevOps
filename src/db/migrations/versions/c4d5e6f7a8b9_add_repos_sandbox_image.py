"""add repos.sandbox_image (per-repo sandbox image, ADR 0039)

Revision ID: c4d5e6f7a8b9
Revises: e8f9a0b1c2d3
Create Date: 2026-07-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Docker image the sandbox runs in for runs against this repo. Nullable — NULL means
    # the generic `agentic-sandbox:0.1`, which is exactly the behaviour every existing row
    # already had, so no backfill is needed.
    op.add_column("repos", sa.Column("sandbox_image", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("repos", "sandbox_image")
