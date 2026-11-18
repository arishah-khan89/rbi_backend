"""inspection_tasks: planned inspection work per equipment (type, RBI interval, last date, next date)

Revision ID: n4i5d6e7f8g9
Revises: m3h4c5d6e7f8

Adds only the new table (when it is missing); nothing that exists is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'n4i5d6e7f8g9'
down_revision: Union[str, None] = 'm3h4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'inspection_tasks' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'inspection_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('equipment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('equipment.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('task_type', sa.String(20), nullable=False),
        sa.Column('details', sa.Text()),
        sa.Column('duration_days', sa.Integer()),
        sa.Column('rbi_interval_years', sa.Numeric(6, 2)),
        sa.Column('last_date', sa.Date()),
        sa.Column('next_date', sa.Date()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('created_by', sa.String(200)),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(200)),
    )
    op.create_index('ix_inspection_tasks_equipment_id', 'inspection_tasks', ['equipment_id'])
    op.create_index('ix_inspection_tasks_next_date', 'inspection_tasks', ['next_date'])


def downgrade() -> None:
    op.drop_index('ix_inspection_tasks_next_date', table_name='inspection_tasks')
    op.drop_index('ix_inspection_tasks_equipment_id', table_name='inspection_tasks')
    op.drop_table('inspection_tasks')
