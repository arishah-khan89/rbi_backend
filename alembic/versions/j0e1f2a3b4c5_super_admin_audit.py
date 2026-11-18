"""super admin support: users.must_change_password and the audit_log table

Revision ID: j0e1f2a3b4c5
Revises: i9d0e1f2a3b4

Both are added only when missing and change nothing that exists: existing users keep their password and role.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'j0e1f2a3b4c5'
down_revision: Union[str, None] = 'i9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if 'must_change_password' not in {c['name'] for c in insp.get_columns('users')}:
        op.add_column('users', sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()))
    if 'audit_log' not in insp.get_table_names():
        op.create_table(
            'audit_log',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('actor', sa.String(80), nullable=False),
            sa.Column('action', sa.String(60), nullable=False),
            sa.Column('target', sa.String(200)),
            sa.Column('detail', sa.Text()),
        )
        op.create_index('ix_audit_log_at', 'audit_log', ['at'])
        op.create_index('ix_audit_log_action', 'audit_log', ['action'])


def downgrade() -> None:
    op.drop_index('ix_audit_log_action', table_name='audit_log')
    op.drop_index('ix_audit_log_at', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_column('users', 'must_change_password')
