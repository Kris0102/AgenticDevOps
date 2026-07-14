"""lesson store: nullable run_id + microtask_id + updated_at (ADR 0034)

Human-curated lessons (operator rules) have no originating run, so `run_id` drops its
NOT NULL. `microtask_id` promotes a fact that was only ever encoded inside the
`lesson_id` string (`lesson_{run}_code_{microtask}`) to a real, linkable column, and
`updated_at` records a human edit — the two facts the dashboard's "why was this lesson
added" narrative cannot derive from the existing columns.

Revision ID: d7e8f9a0b1c2
Revises: 708069ce37b4
Create Date: 2026-07-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = '708069ce37b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("lessons", "run_id", existing_type=sa.String(), nullable=True)
    op.add_column("lessons", sa.Column("microtask_id", sa.String(), nullable=True))
    op.add_column("lessons", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    # Recover the microtask link already present (but unqueryable) in existing code
    # lessons' ids — `lesson_<run_id>_code_<microtask_id>`, written by
    # LessonStore.record_code_lessons.
    op.execute(
        r"""
        UPDATE lessons
           SET microtask_id = substring(lesson_id from '_code_(.*)$')
         WHERE scope = 'code'
           AND lesson_id LIKE '%\_code\_%'
        """
    )


def downgrade() -> None:
    op.drop_column("lessons", "updated_at")
    op.drop_column("lessons", "microtask_id")
    # Human-authored lessons have no run to attribute — they cannot survive the
    # NOT NULL coming back.
    op.execute("DELETE FROM lessons WHERE run_id IS NULL")
    op.alter_column("lessons", "run_id", existing_type=sa.String(), nullable=False)
