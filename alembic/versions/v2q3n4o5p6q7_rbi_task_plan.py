"""Risk assessment (RBI) -> inspection tasks: the choice per side (Internal / External) instead of one yes / no

Revision ID: v2q3n4o5p6q7
Revises: u1p2m3n4o5p6

rbi_assessments.task_plan   what the person chose for the Internal and the External inspection plan:
                            {"internal": {"action": "new" | "link" | "none", "task_id": ...}, "external": {...}}
                            and, once approved, which task it made or updated.
The one yes / no column of the previous revision (reflect_to_tasks, never in use outside development) is removed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'v2q3n4o5p6q7'
down_revision: Union[str, None] = 'u1p2m3n4o5p6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('rbi_assessments')}


def upgrade() -> None:
    have = _columns()
    if 'task_plan' not in have:
        op.add_column('rbi_assessments', sa.Column('task_plan', postgresql.JSONB(), nullable=True))
    if 'reflect_to_tasks' in have:
        op.drop_column('rbi_assessments', 'reflect_to_tasks')


def downgrade() -> None:
    have = _columns()
    if 'reflect_to_tasks' not in have:
        op.add_column('rbi_assessments', sa.Column('reflect_to_tasks', sa.Boolean(), nullable=False, server_default=sa.false()))
    if 'task_plan' in have:
        op.drop_column('rbi_assessments', 'task_plan')
