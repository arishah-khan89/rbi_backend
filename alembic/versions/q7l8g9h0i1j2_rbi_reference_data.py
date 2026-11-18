"""RBI reference data: fluids and damage mechanisms

Revision ID: q7l8g9h0i1j2
Revises: p6k7f8g9h0i1

Adds two new tables (each only when it is missing); nothing that exists is changed. They are filled from the client's RBI sheet
with `python -m app.seed.import_rbi_reference`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'q7l8g9h0i1j2'
down_revision: Union[str, None] = 'p6k7f8g9h0i1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    uuid_t = postgresql.UUID(as_uuid=True)
    now = sa.func.now()
    if 'rbi_fluids' not in have:
        op.create_table(
            'rbi_fluids',
            sa.Column('id', uuid_t, primary_key=True),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('cas_number', sa.String(80)),
            sa.Column('min_concentration', sa.String(80)),
            sa.Column('meridium_name', sa.String(120)),
            sa.Column('toxicity_number', sa.Integer()),
            sa.Column('flammability_number', sa.Integer()),
            sa.Column('ratings_confirmed', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=now),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=now),
        )
        op.create_index('uq_rbi_fluids_name_lower', 'rbi_fluids', [sa.text('lower(name)')], unique=True)
    if 'rbi_damage_mechanisms' not in have:
        op.create_table(
            'rbi_damage_mechanisms',
            sa.Column('id', uuid_t, primary_key=True),
            sa.Column('name', sa.String(200), nullable=False),
            sa.Column('definition', sa.Text()),
            sa.Column('applicable_materials', sa.Text()),
            sa.Column('min_temperature_c', sa.Numeric(8, 2)),
            sa.Column('max_temperature_c', sa.Numeric(8, 2)),
            sa.Column('temperature_note', sa.String(200)),
            sa.Column('process_streams', sa.Text()),
            sa.Column('damage_type', sa.String(200)),
            sa.Column('recommended_nde', sa.Text()),
            sa.Column('sort_order', sa.Integer()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=now),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=now),
        )
        op.create_index('uq_rbi_damage_mechanisms_name_lower', 'rbi_damage_mechanisms', [sa.text('lower(name)')], unique=True)


def downgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ('rbi_damage_mechanisms', 'rbi_fluids'):
        if table in have:
            op.drop_table(table)
