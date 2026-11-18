"""equipment documents in the database

Revision ID: p6k7f8g9h0i1
Revises: o5j6e7f8g9h0

Equipment datasheets used to be files in a public folder (anyone with the link could open them, and they were lost on a
redeploy). They now live in the database and are given only to signed-in people. This adds the table and moves the files
that already exist into it, pointing each equipment at the new address. A file that is missing on disk is left as it was.
"""
import mimetypes
import os
import re
import uuid as uuid_lib
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'p6k7f8g9h0i1'
down_revision: Union[str, None] = 'o5j6e7f8g9h0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_PREFIX = '/uploads/documents/'
NEW_PREFIX = '/api/v1/equipment/documents/'
_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'uploads', 'documents')


def upgrade() -> None:
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())
    if 'equipment_documents' not in have:
        op.create_table(
            'equipment_documents',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('filename', sa.String(300), nullable=False),
            sa.Column('content_type', sa.String(100)),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            sa.Column('content', sa.LargeBinary(), nullable=False),
            sa.Column('uploaded_by', sa.String(200)),
            sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    docs = sa.table('equipment_documents', sa.column('id', postgresql.UUID(as_uuid=True)), sa.column('filename', sa.String),
                    sa.column('content_type', sa.String), sa.column('size_bytes', sa.Integer),
                    sa.column('content', sa.LargeBinary), sa.column('uploaded_by', sa.String))
    rows = bind.execute(sa.text("SELECT id, document_url, document_name FROM equipment WHERE document_url LIKE :p"),
                        {'p': OLD_PREFIX + '%'}).fetchall()
    moved = {}                                                     # one stored file shared by several equipment is stored once
    for row_id, url, name in rows:
        stored = os.path.basename(url)
        path = os.path.join(_FOLDER, stored)
        if not os.path.isfile(path):
            continue                                               # nothing to move: leave the old address
        if stored not in moved:
            with open(path, 'rb') as f:
                content = f.read()
            doc_id = uuid_lib.uuid4()
            shown = name or re.sub(r'^doc_[0-9a-f]{8}_', '', stored)
            bind.execute(docs.insert().values(id=doc_id, filename=shown, size_bytes=len(content), content=content,
                                              content_type=mimetypes.guess_type(stored)[0], uploaded_by='migration'))
            moved[stored] = doc_id
        bind.execute(sa.text("UPDATE equipment SET document_url = :u WHERE id = :i"),
                     {'u': NEW_PREFIX + str(moved[stored]), 'i': row_id})


def downgrade() -> None:
    # the files stay in the table; the equipment keeps pointing at /api/v1/equipment/documents/..., so nothing is lost
    pass
