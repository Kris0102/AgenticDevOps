"""normalize microtasks.acceptance_criteria to structured AcceptanceCriterion shape

Data-only migration (ADR 0016). `acceptance_criteria` changed from `List[str]` to
`List[AcceptanceCriterion]` = `[{"text": str, "examples": [...]}, ...]`. The column is
already JSON, so there is no DDL change — this backfills existing rows by wrapping any
bare-string criterion as `{"text": <str>, "examples": []}`. Idempotent: rows already in
object form are left untouched.

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-24
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite(rows, transform):
    bind = op.get_bind()
    for microtask_id, criteria in rows:
        if not criteria:
            continue
        new, changed = [], False
        for item in criteria:
            converted = transform(item)
            new.append(converted)
            changed = changed or (converted is not item)
        if changed:
            bind.execute(
                sa.text(
                    "UPDATE microtasks SET acceptance_criteria = CAST(:ac AS JSON) "
                    "WHERE microtask_id = :mid"
                ),
                {"ac": json.dumps(new), "mid": microtask_id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT microtask_id, acceptance_criteria FROM microtasks")
    ).fetchall()

    def to_object(item):
        if isinstance(item, str):
            return {"text": item, "examples": []}
        return item  # already structured — leave as-is

    _rewrite(rows, to_object)


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT microtask_id, acceptance_criteria FROM microtasks")
    ).fetchall()

    def to_string(item):
        if isinstance(item, dict) and "text" in item:
            return item["text"]  # drop examples — lossy, but restores List[str]
        return item

    _rewrite(rows, to_string)
