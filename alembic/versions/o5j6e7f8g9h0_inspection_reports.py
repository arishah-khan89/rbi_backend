"""inspection reports: reports, the tasks they cover, who they are sent to, and their files

Revision ID: o5j6e7f8g9h0
Revises: n4i5d6e7f8g9

Adds only new tables (each when it is missing); nothing that exists is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'o5j6e7f8g9h0'
down_revision: Union[str, None] = 'n4i5d6e7f8g9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    uuid_t = postgresql.UUID(as_uuid=True)
    if 'inspection_reports' not in have:
        op.create_table(
            'inspection_reports',
            sa.Column('id', uuid_t, primary_key=True),
            sa.Column('equipment_id', uuid_t, sa.ForeignKey('equipment.id', ondelete='CASCADE'), nullable=False),
            sa.Column('equipment_tag', sa.String(50), nullable=False),
            sa.Column('report_no', sa.String(50), nullable=False),
            sa.Column('inspection_date', sa.Date(), nullable=False),
            sa.Column('title', sa.String(40), nullable=False),
            sa.Column('reason', sa.Text()),
            sa.Column('summary', sa.Text()),
            sa.Column('approval_status', sa.String(20), nullable=False, server_default='draft'),
            sa.Column('submitted_by', sa.String(80)),
            sa.Column('submitted_at', sa.DateTime(timezone=True)),
            sa.Column('assigned_reviewer', sa.String(80)),
            sa.Column('reviewed_by', sa.String(80)),
            sa.Column('reviewed_at', sa.DateTime(timezone=True)),
            sa.Column('submit_comment', sa.Text()),
            sa.Column('review_comment', sa.Text()),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('created_by', sa.String(200)),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_by', sa.String(200)),
        )
        op.create_index('ix_inspection_reports_equipment_id', 'inspection_reports', ['equipment_id'])
        op.create_index('ix_inspection_reports_equipment_tag', 'inspection_reports', ['equipment_tag'])
        op.create_index('ix_inspection_reports_inspection_date', 'inspection_reports', ['inspection_date'])
        op.create_index('ix_inspection_reports_approval_status', 'inspection_reports', ['approval_status'])
    if 'inspection_report_tasks' not in have:
        op.create_table(
            'inspection_report_tasks',
            sa.Column('report_id', uuid_t, sa.ForeignKey('inspection_reports.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('task_id', uuid_t, sa.ForeignKey('inspection_tasks.id', ondelete='RESTRICT'), primary_key=True),
        )
        op.create_index('ix_inspection_report_tasks_task_id', 'inspection_report_tasks', ['task_id'])
    if 'inspection_report_recipients' not in have:
        op.create_table(
            'inspection_report_recipients',
            sa.Column('report_id', uuid_t, sa.ForeignKey('inspection_reports.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('username', sa.String(80), primary_key=True),
        )
    if 'inspection_report_files' not in have:
        op.create_table(
            'inspection_report_files',
            sa.Column('id', uuid_t, primary_key=True),
            sa.Column('report_id', uuid_t, sa.ForeignKey('inspection_reports.id', ondelete='CASCADE'), nullable=False),
            sa.Column('filename', sa.String(200), nullable=False),
            sa.Column('content_type', sa.String(100)),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            sa.Column('content', sa.LargeBinary(), nullable=False),
            sa.Column('uploaded_by', sa.String(200)),
            sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_inspection_report_files_report_id', 'inspection_report_files', ['report_id'])


def downgrade() -> None:
    op.drop_table('inspection_report_files')
    op.drop_table('inspection_report_recipients')
    op.drop_table('inspection_report_tasks')
    op.drop_table('inspection_reports')
