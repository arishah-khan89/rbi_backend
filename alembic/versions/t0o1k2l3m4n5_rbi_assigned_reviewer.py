"""Risk assessment (RBI): the admin an assessment is sent to

Revision ID: t0o1k2l3m4n5
Revises: s9n0i1j2k3l4

Adds two nullable columns to rbi_assessments (each only when it is missing); nothing that exists is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 't0o1k2l3m4n5'
down_revision: Union[str, None] = 's9n0i1j2k3l4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('rbi_assessments')}


def upgrade() -> None:
    have = _columns()
    if 'assigned_reviewer' not in have:
        op.add_column('rbi_assessments', sa.Column('assigned_reviewer', sa.String(80)))
    if 'submit_comment' not in have:
        op.add_column('rbi_assessments', sa.Column('submit_comment', sa.Text()))


def downgrade() -> None:
    have = _columns()
    if 'submit_comment' in have:
        op.drop_column('rbi_assessments', 'submit_comment')
    if 'assigned_reviewer' in have:
        op.drop_column('rbi_assessments', 'assigned_reviewer')
