"""inspection_records.locked_snapshot: the t_min of each zone when the inspection was locked

Revision ID: m3h4c5d6e7f8
Revises: l2g3b4c5d6e7

Adds one nullable column (only when it is missing). Existing inspections keep working: a locked one without a
snapshot gets it the first time the equipment data is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'm3h4c5d6e7f8'
down_revision: Union[str, None] = 'l2g3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('inspection_records')}
    if 'locked_snapshot' not in cols:
        op.add_column('inspection_records', sa.Column('locked_snapshot', sa.Text()))


def downgrade() -> None:
    op.drop_column('inspection_records', 'locked_snapshot')
