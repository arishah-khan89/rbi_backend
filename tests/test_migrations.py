"""A database built ONLY by the migrations (as on Railway) must work like the development database.

The other tests create their tables from the models; this one runs `alembic upgrade head` on an empty temporary
database, so a column that the code uses but no migration creates is caught here.
Run from backend/:  python -m unittest tests.test_migrations -v
"""
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
import app.models.equipment, app.models.inspection, app.models.cml, app.models.asme, app.models.tank, app.models.user, app.models.notification, app.models.inspection_task, app.models.inspection_report, app.models.rbi_assessment  # noqa: E402,F401
from app.models.equipment import Equipment  # noqa: E402
from app.schemas.inspection import DynamicTMLReadingInput, InspectionRecordCreate  # noqa: E402
from app.crud import inspection as crud  # noqa: E402

BACKEND = os.path.join(os.path.dirname(__file__), "..")


class MigratedDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = make_url(settings.DATABASE_URL)
        cls.name = "ams_mig_" + uuid.uuid4().hex[:8]
        cls.admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with cls.admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{cls.name}"'))
        cls.url = base.set(database=cls.name).render_as_string(hide_password=False)
        run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND,
                             env=dict(os.environ, DATABASE_URL=cls.url), capture_output=True, text=True)
        if run.returncode != 0:
            cls.tearDownClass()
            raise AssertionError("alembic upgrade head failed: " + run.stderr[-600:])
        cls.engine = create_engine(cls.url)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "engine", None) is not None:
            cls.engine.dispose()
        with cls.admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))
        cls.admin.dispose()

    def test_every_column_of_the_code_exists_after_the_migrations(self):
        insp = inspect(self.engine)
        have_tables = set(insp.get_table_names())
        for table in Base.metadata.sorted_tables:
            self.assertIn(table.name, have_tables, f"table {table.name} is not created by the migrations")
            have = {c["name"] for c in insp.get_columns(table.name)}
            missing = sorted({c.name for c in table.columns} - have)
            self.assertEqual(missing, [], f"{table.name}: columns used by the code but not created by any migration")

    def test_saving_inspections_works_on_a_migrated_database(self):
        db = sessionmaker(bind=self.engine)()
        try:
            eq = Equipment(equipment_id="MIG-1", equipment_name="migrated", equipment_type="Pressure Vessel", plant="P", unit="U",
                           min_required_thickness=D("8.0"), nominal_thickness=D("12"))
            db.add(eq)
            db.commit()
            db.refresh(eq)
            for date, t in ((datetime(2024, 1, 1), D("11.9")), (datetime(2026, 1, 1), D("11.2"))):    # the 2nd save reads the 1st
                rec = crud.create_inspection_record(db, InspectionRecordCreate(
                    equipment_id=eq.id, equipment_tag="MIG-1", inspection_date=date, inspector_name="t",
                    tml_readings=[DynamicTMLReadingInput(tml_code="S-1", tml_type="Shell", reading_1=t, reading_2=t, reading_3=t, reading_4=t)]))
                self.assertIsNotNone(rec.id)
            db.refresh(eq)
            self.assertIsNotNone(eq.remaining_life)
        finally:
            db.close()


class SampleTemplateRoute(unittest.TestCase):
    def test_the_sample_excel_can_be_downloaded(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(settings, "AUTH_REQUIRED", False):          # whatever AUTH_REQUIRED is in backend/.env
            res = TestClient(app).get("/api/v1/inspection/sample-template")
        self.assertEqual(res.status_code, 200, res.text[:200])      # it was taken for a record id (422) when declared after /{record_id}
        self.assertIn("spreadsheetml", res.headers["content-type"])
        self.assertTrue(res.content.startswith(b"PK"))              # an .xlsx file is a zip


class ErrorTextOutsideDevelopment(unittest.TestCase):
    def test_database_errors_are_not_shown_to_the_user_in_production(self):
        from app.crud.inspection import _error_detail
        old = settings.APP_ENV
        try:
            settings.APP_ENV = "production"
            self.assertEqual(_error_detail(Exception('column "x" does not exist')), "")
            settings.APP_ENV = "development"
            self.assertIn("does not exist", _error_detail(Exception('column "x" does not exist')))
        finally:
            settings.APP_ENV = old


if __name__ == "__main__":
    unittest.main()


class EquipmentDocumentsMoveIntoTheDatabase(unittest.TestCase):
    """The migration that puts equipment documents into the database moves the files that are on disk today."""

    def test_existing_files_are_moved_and_the_old_public_address_is_replaced(self):
        base = make_url(settings.DATABASE_URL)
        name = "ams_mig_docs_" + uuid.uuid4().hex[:8]
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        folder = os.path.join(BACKEND, "uploads", "documents")
        os.makedirs(folder, exist_ok=True)
        stored = "doc_deadbeef_migration_test.pdf"
        path = os.path.join(folder, stored)
        engine = None
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            url = base.set(database=name).render_as_string(hide_password=False)
            env = dict(os.environ, DATABASE_URL=url)
            alembic = lambda target: subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=BACKEND,
                                                    env=env, capture_output=True, text=True)
            self.assertEqual(alembic("o5j6e7f8g9h0").returncode, 0)          # the state before this change
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 old file on disk")
            engine = create_engine(url)
            with engine.begin() as c:
                for tag, doc in (("DOC-1", "/uploads/documents/" + stored), ("DOC-2", "/uploads/documents/" + stored),
                                 ("DOC-3", "/uploads/documents/doc_00000000_gone.pdf"), ("DOC-4", None)):
                    c.execute(text("INSERT INTO equipment (id, equipment_id, equipment_name, equipment_type, plant, unit, document_url, document_name) "
                                   "VALUES (gen_random_uuid(), :t, 'x', 'Pressure Vessel', 'P', 'U', :d, 'Datasheet.pdf')"), {"t": tag, "d": doc})
            run = alembic("head")
            self.assertEqual(run.returncode, 0, run.stderr[-600:])
            with engine.connect() as c:
                urls = dict(c.execute(text("SELECT equipment_id, document_url FROM equipment")).fetchall())
                docs = c.execute(text("SELECT filename, size_bytes, content FROM equipment_documents")).fetchall()
            self.assertEqual(len(docs), 1)                                    # two equipment shared one file: stored once
            self.assertEqual(docs[0][0], "Datasheet.pdf")
            self.assertEqual(bytes(docs[0][2]), b"%PDF-1.4 old file on disk")
            self.assertTrue(urls["DOC-1"].startswith("/api/v1/equipment/documents/"))
            self.assertEqual(urls["DOC-1"], urls["DOC-2"])
            self.assertEqual(urls["DOC-3"], "/uploads/documents/doc_00000000_gone.pdf")   # missing on disk: left as it was
            self.assertIsNone(urls["DOC-4"])
        finally:
            if engine is not None:
                engine.dispose()
            if os.path.exists(path):
                os.remove(path)
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()
