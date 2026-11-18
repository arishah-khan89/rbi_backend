"""Risk assessment (RBI) of the client's workbook: fluid list, mechanism library, components, assessments

Revision ID: s9n0i1j2k3l4
Revises: r8m9h0i1j2k3

Adds four new tables (each only when it is missing); nothing that exists is changed. The older RBI tables (rbi_fluids,
rbi_damage_mechanisms, rbi_analyses) are left as they are.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 's9n0i1j2k3l4'
down_revision: Union[str, None] = 'r8m9h0i1j2k3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _have(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    uuid_pk = lambda: sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True)          # noqa: E731
    stamps = lambda: [sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),  # noqa: E731
                      sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now())]
    if not _have('rbi_fluid_list'):
        op.create_table(
            'rbi_fluid_list', uuid_pk(),
            sa.Column('toxic_name', sa.String(200)), sa.Column('flammable_name', sa.String(200)),
            sa.Column('cas_number', sa.String(80)), sa.Column('toxicity_concentration', sa.Numeric(14, 4)),
            sa.Column('toxicity_unit', sa.String(30)), sa.Column('flammability', sa.Integer()),
            sa.Column('flash_point', sa.String(40)), sa.Column('note', sa.String(200)), *stamps())
        op.create_index('uq_rbi_fluid_list_toxic_lower', 'rbi_fluid_list', [sa.text('lower(toxic_name)')], unique=True,
                        postgresql_where=sa.text('toxic_name IS NOT NULL'))
        op.create_index('uq_rbi_fluid_list_flammable_lower', 'rbi_fluid_list', [sa.text('lower(flammable_name)')], unique=True,
                        postgresql_where=sa.text('flammable_name IS NOT NULL'))
    if not _have('rbi_mechanism_library'):
        op.create_table(
            'rbi_mechanism_library', uuid_pk(),
            sa.Column('code', sa.String(20), nullable=False), sa.Column('name', sa.String(200), nullable=False),
            sa.Column('location', sa.String(20)), sa.Column('damage_class', sa.String(60)), sa.Column('rate_based', sa.String(10)),
            sa.Column('typical_drivers', sa.Text()), sa.Column('primary_nde', sa.Text()), sa.Column('secondary_nde', sa.Text()),
            sa.Column('coverage_timing', sa.Text()), sa.Column('mitigation', sa.Text()), sa.Column('definition', sa.Text()),
            sa.Column('applicable_materials', sa.Text()), sa.Column('min_temperature_c', sa.Numeric(8, 2)),
            sa.Column('max_temperature_c', sa.Numeric(8, 2)), sa.Column('temperature_note', sa.String(200)),
            sa.Column('process_streams', sa.Text()), sa.Column('damage_type', sa.String(200)), sa.Column('recommended_nde', sa.Text()),
            sa.Column('sort_order', sa.Integer()), *stamps())
        op.create_index('uq_rbi_mechanism_library_name_lower', 'rbi_mechanism_library', [sa.text('lower(name)')], unique=True)
        op.create_index('ix_rbi_mechanism_library_code', 'rbi_mechanism_library', ['code'])
    if not _have('rbi_components'):
        op.create_table(
            'rbi_components', uuid_pk(),
            sa.Column('equipment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('equipment.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(120), nullable=False), sa.Column('component_type', sa.String(40), nullable=False),
            sa.Column('in_service_date', sa.Date()), sa.Column('status', sa.String(20), nullable=False, server_default='Active'),
            sa.Column('description', sa.Text()), sa.Column('comments', sa.Text()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column('created_by', sa.String(200)),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column('updated_by', sa.String(200)),
            sa.UniqueConstraint('equipment_id', 'name', name='uq_rbi_component_name'))
        op.create_index('ix_rbi_components_equipment_id', 'rbi_components', ['equipment_id'])
    if not _have('rbi_assessments'):
        op.create_table(
            'rbi_assessments', uuid_pk(),
            sa.Column('component_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('rbi_components.id', ondelete='CASCADE'), nullable=False),
            sa.Column('revision', sa.Integer(), nullable=False), sa.Column('state', sa.String(20), nullable=False, server_default='draft'),
            sa.Column('assessment_date', sa.Date(), nullable=False),
            sa.Column('inputs', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column('results', postgresql.JSONB()), sa.Column('rules_version', sa.String(30)),
            sa.Column('comments', sa.Text()), sa.Column('return_reason', sa.Text()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column('created_by', sa.String(200)),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column('updated_by', sa.String(200)),
            sa.Column('finalized_at', sa.DateTime(timezone=True)), sa.Column('finalized_by', sa.String(200)),
            sa.Column('approved_at', sa.DateTime(timezone=True)), sa.Column('approved_by', sa.String(200)),
            sa.UniqueConstraint('component_id', 'revision', name='uq_rbi_assessment_revision'))
        op.create_index('ix_rbi_assessments_component_id', 'rbi_assessments', ['component_id'])
        op.create_index('ix_rbi_assessments_state', 'rbi_assessments', ['state'])


def downgrade() -> None:
    for name in ('rbi_assessments', 'rbi_components', 'rbi_mechanism_library', 'rbi_fluid_list'):
        if _have(name):
            op.drop_table(name)
