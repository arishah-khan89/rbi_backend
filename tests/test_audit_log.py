"""A: the inspector is the signed-in person. B: every edit of an inspection is written down (old -> new).
C: the audit log can be searched and exported (super admin only). Own temporary database, login switched on.

Run from backend/:  python -m unittest tests.test_audit_log -v
"""
import io
import os
import secrets
import sys
import unittest
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.endpoints import auth as auth_endpoints  # noqa: E402
from app.core import security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
import app.models.equipment, app.models.inspection, app.models.cml, app.models.user, app.models.audit  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

BASE_URL = make_url(settings.DATABASE_URL)
TEST_DB = "ams_test_" + uuid.uuid4().hex[:8]
engine = create_engine(BASE_URL.set(database=TEST_DB), pool_pre_ping=True)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
USERS = {"eng": ("engineer", "Ahmed Khan"), "adm1": ("admin", "Admin One"), "sup": ("super_admin", "Super Person")}
PW = {u: secrets.token_urlsafe(14) for u in USERS}
_auth_before = settings.AUTH_REQUIRED
API = "/api/v1/inspection"


def setUpModule():
    settings.AUTH_REQUIRED = True
    admin = create_engine(BASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()
    Base.metadata.create_all(bind=engine)
    db = Session()
    for u, (role, full) in USERS.items():
        db.add(User(username=u, full_name=full, role=role, password_hash=security.hash_password(PW[u])))
    db.commit()
    db.close()

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = override


def tearDownModule():
    settings.AUTH_REQUIRED = _auth_before
    app.dependency_overrides.clear()
    engine.dispose()
    admin = create_engine(BASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
    admin.dispose()


class Base_(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.h = {}
        for u in USERS:
            r = cls.client.post("/api/v1/auth/login", json={"username": u, "password": PW[u]})
            cls.h[u] = {"Authorization": f"Bearer {r.json()['access_token']}"}

    def setUp(self):
        auth_endpoints._failures.clear()
        db = Session()
        for t in ("audit_log", "inspection_reading", "inspection_tml", "inspection_records", "equipment"):
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        db.close()
        r = self.client.post("/api/v1/equipment", headers=self.h["eng"], json={
            "equipment_id": "V-1", "equipment_name": "t", "equipment_type": "Pressure Vessel", "design_pressure": 10,
            "design_temperature": 150, "diameter": 1000, "material": "SA-516", "material_grade": "70",
            "min_required_thickness": 8.0, "nominal_thickness": 12})
        self.eq = r.json()["id"]

    def zone(self, code, vals):
        v = list(vals) + [None] * (4 - len(vals))
        return {"tml_code": code, "tml_name": code, "tml_type": "Shell", "direction_labels": "N,S,E,W",
                "reading_1": v[0], "reading_2": v[1], "reading_3": v[2], "reading_4": v[3]}

    def body(self, zones=None, date="2026-03-01T00:00:00", inspector="Typed By Hand"):
        return {"equipment_id": self.eq, "equipment_tag": "V-1", "inspection_date": date, "inspector_name": inspector,
                "tml_readings": zones or [self.zone("S-1", [10.0, 10.1])]}

    def create(self, user="eng", **kw):
        r = self.client.post(API, headers=self.h[user], json=self.body(**kw))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def history(self, rid, user="adm1"):
        return self.client.get(f"{API}/{rid}/history", headers=self.h[user]).json()


class InspectorFromLogin(Base_):
    def test_the_signed_in_person_is_the_inspector(self):
        rec = self.create(inspector="Someone Else")
        self.assertEqual(rec["inspector_name"], "Ahmed Khan")             # not what the form sent
        self.assertEqual(rec["created_by"], "eng")

    def test_a_person_without_a_full_name_appears_by_username(self):
        db = Session()
        db.add(User(username="noname", role="engineer", password_hash=security.hash_password("a-long-enough-pass")))
        db.commit()
        db.close()
        tok = self.client.post("/api/v1/auth/login", json={"username": "noname", "password": "a-long-enough-pass"}).json()["access_token"]
        r = self.client.post(API, headers={"Authorization": f"Bearer {tok}"}, json=self.body(inspector="x"))
        self.assertEqual(r.json()["inspector_name"], "noname")

    def test_an_edit_does_not_change_who_the_inspector_is(self):
        rec = self.create()
        r = self.client.put(f"{API}/{rec['id']}", headers=self.h["eng"], json=self.body(zones=[self.zone("S-1", [10.0, 9.9])], inspector="Forged"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["inspector_name"], "Ahmed Khan")        # the name sent in the form is ignored; it stays the inspector who entered it

    def test_an_import_keeps_the_inspector_column_of_the_sheet(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inspection Data"
        ws.append(["Equipment Tag", "Inspection Date", "Inspector Name", "TML Code", "TML Name", "TML Type", "Direction Labels",
                   "Reading 1 (mm)", "Reading 2 (mm)", "Reading 3 (mm)", "Reading 4 (mm)", "Min Required Thickness (mm)", "Remarks"])
        ws.append(["V-1", datetime(2026, 3, 1), "Field Inspector Ali", "S-1", "S-1", "Shell", "N, S", 10.0, 10.1, None, None, None, None])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.client.post(f"{API}/import-excel", headers=self.h["eng"], data={"reviewer": "adm1"}, files={
            "file": ("t.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        self.assertEqual(r.json()["status"], "success", r.text)
        rec = self.client.get(f"{API}?equipment_id={self.eq}", headers=self.h["adm1"]).json()["data"][0]
        self.assertEqual(rec["inspector_name"], "Field Inspector Ali")     # the people who took the readings, as written in the file
        lines = self.history(rec["id"])
        self.assertEqual(lines[0]["action"], "inspection.create")
        self.assertEqual(lines[0]["actor"], "eng")                          # the person who uploaded it

    def test_with_sign_in_off_the_typed_name_is_kept(self):
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.post(API, json=self.body(inspector="Typed By Hand"))
            self.assertEqual(r.json()["inspector_name"], "Typed By Hand")
        finally:
            settings.AUTH_REQUIRED = True


class ChangeLog(Base_):
    def test_creating_is_written_down(self):
        rec = self.create()
        lines = self.history(rec["id"])
        self.assertEqual([(x["action"], x["actor"]) for x in lines], [("inspection.create", "eng")])
        self.assertIn("1 zone(s)", lines[0]["detail"])

    def test_every_moved_reading_is_written_old_to_new(self):
        rec = self.create()
        r = self.client.put(f"{API}/{rec['id']}", headers=self.h["eng"], json=self.body(
            zones=[self.zone("S-1", [10.0, 9.8, 9.7])]))
        self.assertEqual(r.status_code, 200, r.text)
        edit = [x for x in self.history(rec["id"]) if x["action"] == "inspection.edit"]
        self.assertEqual(len(edit), 1)
        self.assertEqual(edit[0]["actor"], "eng")
        self.assertIn("S-1 reading 2: 10.1 -> 9.8", edit[0]["detail"])
        self.assertIn("S-1 reading 3: empty -> 9.7", edit[0]["detail"])
        self.assertNotIn("reading 1", edit[0]["detail"])                    # 10.0 did not move

    def test_a_save_that_changes_nothing_writes_nothing(self):
        rec = self.create()
        self.client.put(f"{API}/{rec['id']}", headers=self.h["eng"], json=self.body())
        self.assertEqual([x["action"] for x in self.history(rec["id"])], ["inspection.create"])

    def test_a_new_zone_a_removed_value_and_a_new_date_are_written_down(self):
        rec = self.create()
        self.client.put(f"{API}/{rec['id']}", headers=self.h["eng"], json=self.body(
            zones=[self.zone("S-1", [10.0, None]), self.zone("S-2", [9.0, 9.1])], date="2026-03-05T00:00:00"))
        detail = [x for x in self.history(rec["id"]) if x["action"] == "inspection.edit"][0]["detail"]
        self.assertIn("date 2026-03-01 -> 2026-03-05", detail)
        self.assertIn("S-2 added: 9, 9.1", detail)
        self.assertIn("S-1 reading 2: 10.1 -> empty", detail)

    def test_adding_zones_to_an_inspection_of_the_same_date_is_written_down(self):
        rec = self.create()
        r = self.client.post(API, headers=self.h["eng"], json=self.body(zones=[self.zone("S-2", [9.0])]))
        self.assertEqual(r.status_code, 201, r.text)
        lines = self.history(rec["id"])
        self.assertEqual([x["action"] for x in lines], ["inspection.create", "inspection.add_zones"])
        self.assertIn("S-2", lines[1]["detail"])

    def test_a_deleted_inspection_stays_in_the_audit_log(self):
        rec = self.create()
        self.assertEqual(self.client.delete(f"{API}/{rec['id']}", headers=self.h["adm1"]).status_code, 200)
        rows = self.client.get("/api/v1/auth/audit/search?action=inspection.delete", headers=self.h["sup"]).json()
        self.assertEqual(rows["total"], 1)
        self.assertEqual((rows["items"][0]["actor"], rows["items"][0]["target"]), ("adm1", rec["id"]))
        self.assertIn("V-1 2026-03-01", rows["items"][0]["detail"])


class AuditLogSearch(Base_):
    def seed(self):
        a = self.create()
        self.client.put(f"{API}/{a['id']}", headers=self.h["eng"], json=self.body(zones=[self.zone("S-1", [9.0, 9.1])]))
        self.client.post(f"{API}/{a['id']}/submit", headers=self.h["eng"], json={"reviewer": "adm1", "comment": "please look"})
        self.client.post(f"{API}/{a['id']}/approve", headers=self.h["adm1"], json={})
        return a

    def search(self, user="sup", **params):
        r = self.client.get("/api/v1/auth/audit/search", headers=self.h[user], params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_filters_and_paging(self):
        self.seed()
        everything = self.search()
        self.assertGreaterEqual(everything["total"], 4)
        self.assertEqual({x["actor"] for x in self.search(actor="adm1")["items"]}, {"adm1"})
        self.assertTrue(all(x["action"].startswith("inspection.s") for x in self.search(action="inspection.s")["items"]))
        self.assertEqual(self.search(q="please look")["total"], 1)                  # text inside the detail
        self.assertEqual(self.search(q="no-such-text-anywhere")["total"], 0)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.search(date_from=today, date_to=today)["total"], everything["total"])
        self.assertEqual(self.search(date_from="2000-01-01", date_to="2000-01-02")["total"], 0)
        page = self.search(limit=2, offset=0)
        self.assertEqual((len(page["items"]), page["total"]), (2, everything["total"]))
        self.assertNotEqual(page["items"], self.search(limit=2, offset=2)["items"])
        newest_first = [x["at"] for x in everything["items"]]
        self.assertEqual(newest_first, sorted(newest_first, reverse=True))

    def test_a_bad_date_is_refused(self):
        r = self.client.get("/api/v1/auth/audit/search?date_from=31-12-2026", headers=self.h["sup"])
        self.assertEqual(r.status_code, 400)

    def test_only_a_super_admin_reads_the_audit_log(self):
        for u in ("eng", "adm1"):
            self.assertEqual(self.client.get("/api/v1/auth/audit/search", headers=self.h[u]).status_code, 403, u)
            self.assertEqual(self.client.get("/api/v1/auth/audit/export", headers=self.h[u]).status_code, 403, u)

    def test_the_csv_export_has_the_filtered_lines_and_is_itself_logged(self):
        self.seed()
        r = self.client.get("/api/v1/auth/audit/export?action=inspection.", headers=self.h["sup"])
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        text_ = r.content.decode("utf-8-sig")
        lines = text_.strip().splitlines()
        self.assertEqual(lines[0], "When,By,Action,Target,Detail")
        self.assertTrue(all("inspection." in l for l in lines[1:]))
        self.assertGreaterEqual(len(lines) - 1, 4)
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"))                      # Excel opens it with the right encoding
        logged = self.search(action="audit.export")
        self.assertEqual((logged["total"], logged["items"][0]["actor"]), (1, "sup"))

    def test_a_cell_that_looks_like_a_formula_is_defused(self):
        for bad in ("=1+1", "+cmd", "-2", "@x"):
            self.assertEqual(auth_endpoints.csv_safe(bad), "'" + bad)
        self.assertEqual(auth_endpoints.csv_safe("normal"), "normal")
        self.assertEqual(auth_endpoints.csv_safe(None), "")


if __name__ == "__main__":
    unittest.main()
