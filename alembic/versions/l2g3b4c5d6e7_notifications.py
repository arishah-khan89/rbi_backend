"""notifications: a per-person list of what happened to their inspections

Revision ID: l2g3b4c5d6e7
Revises: k1f2a3b4c5d6

Adds only the new table (when it is missing); nothing that exists is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'l2g3b4c5d6e7'
down_revision: Union[str, None] = 'k1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'notifications' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'notifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recipient', sa.String(80), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('detail', sa.Text()),
        sa.Column('record_id', postgresql.UUID(as_uuid=True)),
        sa.Column('equipment_id', postgresql.UUID(as_uuid=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_notifications_recipient', 'notifications', ['recipient'])
    op.create_index('ix_notifications_created_at', 'notifications', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_notifications_created_at', table_name='notifications')
    op.drop_index('ix_notifications_recipient', table_name='notifications')
    op.drop_table('notifications')
