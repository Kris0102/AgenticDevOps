"""add runs.deploy_mode (per-run deploy label, ADR 0036)

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # DeployMode value (dry_run/push/pr/local_only). Nullable — pre-existing runs
    # simply show no label; no backfill (the old skip_deploy/auto_create_pr booleans
    # lived only in checkpoints, not on the row).
    op.add_column("runs", sa.Column("deploy_mode", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "deploy_mode")
