"""inspection approval workflow: status, submitter, assigned reviewer, decision

Revision ID: k1f2a3b4c5d6
Revises: j0e1f2a3b4c5

Columns are added only when missing. Existing inspections become 'draft' (they behave exactly as before: editable,
deletable); nothing is locked retroactively.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'k1f2a3b4c5d6'
down_revision: Union[str, None] = 'j0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ('approval_status', sa.Column('approval_status', sa.String(20), nullable=False, server_default='draft')),
    ('submitted_by', sa.Column('submitted_by', sa.String(80))),
    ('submitted_at', sa.Column('submitted_at', sa.DateTime(timezone=True))),
    ('assigned_reviewer', sa.Column('assigned_reviewer', sa.String(80))),
    ('reviewed_by', sa.Column('reviewed_by', sa.String(80))),
    ('reviewed_at', sa.Column('reviewed_at', sa.DateTime(timezone=True))),
    ('submit_comment', sa.Column('submit_comment', sa.Text())),
    ('review_comment', sa.Column('review_comment', sa.Text())),
]


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    have = {c['name'] for c in insp.get_columns('inspection_records')}
    for name, column in COLUMNS:
        if name not in have:
            op.add_column('inspection_records', column)
    if 'ix_inspection_records_approval_status' not in {i['name'] for i in insp.get_indexes('inspection_records')}:
        op.create_index('ix_inspection_records_approval_status', 'inspection_records', ['approval_status'])


def downgrade() -> None:
    op.drop_index('ix_inspection_records_approval_status', table_name='inspection_records')
    for name, _ in reversed(COLUMNS):
        op.drop_column('inspection_records', name)
