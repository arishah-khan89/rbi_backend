"""Risk assessment (RBI) -> inspection tasks: where a task comes from, and whether an assessment adds its plan to the tasks

Revision ID: u1p2m3n4o5p6
Revises: t0o1k2l3m4n5

Adds nullable / defaulted columns (each only when it is missing); nothing that exists is changed.
  rbi_assessments.reflect_to_tasks      the person's answer when sending the risk for approval: add its inspection plan to the tasks
  inspection_tasks.source               'manual' (written by a person) or 'rbi' (made from an approved risk assessment)
  inspection_tasks.rbi_component_id     the component an 'rbi' task belongs to
  inspection_tasks.rbi_assessment_id    the assessment (revision) it was last taken from
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'u1p2m3n4o5p6'
down_revision: Union[str, None] = 't0o1k2l3m4n5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'reflect_to_tasks' not in _columns('rbi_assessments'):
        op.add_column('rbi_assessments', sa.Column('reflect_to_tasks', sa.Boolean(), nullable=False, server_default=sa.false()))
    have = _columns('inspection_tasks')
    if 'source' not in have:
        op.add_column('inspection_tasks', sa.Column('source', sa.String(10), nullable=False, server_default='manual'))
    if 'rbi_component_id' not in have:
        op.add_column('inspection_tasks', sa.Column('rbi_component_id', postgresql.UUID(as_uuid=True), nullable=True))
        op.create_index('ix_inspection_tasks_rbi_component_id', 'inspection_tasks', ['rbi_component_id'])
        op.create_foreign_key('fk_inspection_tasks_rbi_component', 'inspection_tasks', 'rbi_components', ['rbi_component_id'], ['id'], ondelete='SET NULL')
    if 'rbi_assessment_id' not in have:
        op.add_column('inspection_tasks', sa.Column('rbi_assessment_id', postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key('fk_inspection_tasks_rbi_assessment', 'inspection_tasks', 'rbi_assessments', ['rbi_assessment_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    have = _columns('inspection_tasks')
    if 'rbi_assessment_id' in have:
        op.drop_constraint('fk_inspection_tasks_rbi_assessment', 'inspection_tasks', type_='foreignkey')
        op.drop_column('inspection_tasks', 'rbi_assessment_id')
    if 'rbi_component_id' in have:
        op.drop_constraint('fk_inspection_tasks_rbi_component', 'inspection_tasks', type_='foreignkey')
        op.drop_index('ix_inspection_tasks_rbi_component_id', table_name='inspection_tasks')
        op.drop_column('inspection_tasks', 'rbi_component_id')
    if 'source' in have:
        op.drop_column('inspection_tasks', 'source')
    if 'reflect_to_tasks' in _columns('rbi_assessments'):
        op.drop_column('rbi_assessments', 'reflect_to_tasks')
