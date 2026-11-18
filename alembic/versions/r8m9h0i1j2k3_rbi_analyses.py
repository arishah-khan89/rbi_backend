"""RBI analyses: one per component of an equipment

Revision ID: r8m9h0i1j2k3
Revises: q7l8g9h0i1j2

Adds one new table (only when it is missing); nothing that exists is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'r8m9h0i1j2k3'
down_revision: Union[str, None] = 'q7l8g9h0i1j2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'rbi_analyses' in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        'rbi_analyses',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('equipment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('equipment.id', ondelete='CASCADE'), nullable=False),
        sa.Column('component', sa.String(20), nullable=False),
        sa.Column('inputs', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('created_by', sa.String(200)),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_by', sa.String(200)),
        sa.UniqueConstraint('equipment_id', 'component', name='uq_rbi_analysis_component'),
    )
    op.create_index('ix_rbi_analyses_equipment_id', 'rbi_analyses', ['equipment_id'])


def downgrade() -> None:
    if 'rbi_analyses' in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table('rbi_analyses')
