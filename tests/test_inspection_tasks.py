"""Inspection tasks (Inspection Reports dashboard, tab "Inspection Task"), through the API with sign-in on, on a TEMPORARY
database. Everything in a task is written by a person; the only thing the software works out is the badge
(overdue / due soon / ok). Run from backend/:  python -m unittest tests.test_inspection_tasks -v
"""
import os
import secrets
import sys
import unittest
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.endpoints import auth as auth_endpoints  # noqa: E402
from app.core import security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
import app.models.equipment, app.models.inspection, app.models.cml, app.models.user, app.models.audit  # noqa: E402,F401
import app.models.inspection_task  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

BASE_URL = make_url(settings.DATABASE_URL)
TEST_DB = "ams_test_" + uuid.uuid4().hex[:8]
engine = create_engine(BASE_URL.set(database=TEST_DB), pool_pre_ping=True)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
USERS = {"eng": "engineer", "adm": "admin", "sup": "super_admin", "view": "viewer"}
PW = {u: secrets.token_urlsafe(14) for u in USERS}
_auth_before = settings.AUTH_REQUIRED


def setUpModule():
    settings.AUTH_REQUIRED = True
    admin = create_engine(BASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()
    Base.metadata.create_all(bind=engine)
    db = Session()
    for u, role in USERS.items():
        db.add(User(username=u, full_name=u.title(), role=role, password_hash=security.hash_password(PW[u])))
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


API = "/api/v1/inspection-tasks"
TODAY = date.today()
iso = lambda d: d.isoformat()


class Tasks(unittest.TestCase):
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
        for t in ("audit_log", "inspection_tasks", "inspection_reading", "inspection_tml", "inspection_records", "equipment"):
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        db.close()
        self.eq = self.make_equipment("V-1")
        self.eq2 = self.make_equipment("E-2")

    def make_equipment(self, tag):
        r = self.client.post("/api/v1/equipment", headers=self.h["eng"], json={
            "equipment_id": tag, "equipment_name": f"{tag} name", "equipment_type": "Pressure Vessel", "design_pressure": 10,
            "design_temperature": 150, "diameter": 1000, "material": "SA-516", "material_grade": "70",
            "min_required_thickness": 8.0, "nominal_thickness": 12})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def body(self, **kw):
        base = {"equipment_id": self.eq, "name": "Inspection Task-1", "task_type": "Internal", "details": "Check the shell",
                "duration_days": 30, "rbi_interval_years": 7, "last_date": "2024-12-15", "next_date": "2031-12-15"}
        base.update(kw)
        return base

    def make(self, user="eng", **kw):
        r = self.client.post(API, headers=self.h[user], json=self.body(**kw))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # ---------------------------------------------------------------- create / read
    def test_a_task_keeps_exactly_what_the_person_wrote(self):
        t = self.make()
        self.assertEqual((t["name"], t["task_type"], t["details"], t["duration_days"]), ("Inspection Task-1", "Internal", "Check the shell", 30))
        self.assertEqual((float(t["rbi_interval_years"]), t["last_date"], t["next_date"]), (7.0, "2024-12-15", "2031-12-15"))
        self.assertEqual((t["equipment_tag"], t["equipment_name"], t["created_by"]), ("V-1", "V-1 name", "eng"))
        got = self.client.get(f"{API}/{t['id']}", headers=self.h["view"]).json()
        self.assertEqual((got["next_date"], got["rbi_interval_years"]), ("2031-12-15", "7.00"))

    def test_nothing_is_calculated_from_the_interval_or_the_dates(self):
        t = self.make(rbi_interval_years=7, last_date="2024-12-15", next_date="2030-01-01")       # does NOT add up on purpose
        self.assertEqual((t["last_date"], t["next_date"]), ("2024-12-15", "2030-01-01"))           # the software leaves it alone
        t = self.make(name="Inspection Task-2", task_type="External", rbi_interval_years=None, last_date=None, next_date=None)
        self.assertEqual((t["rbi_interval_years"], t["last_date"], t["next_date"], t["status"]), (None, None, None, "none"))

    def test_the_next_date_cannot_be_later_than_the_last_date_plus_the_rbi_interval(self):
        # 2 years = 730.5 days, counted as 731: the latest next date after 2025-01-01 is 2027-01-02
        post = lambda **kw: self.client.post(API, headers=self.h["eng"], json=self.body(**kw))
        late = post(name="late", rbi_interval_years=2, last_date="2025-01-01", next_date="2027-01-03")
        self.assertEqual(late.status_code, 400)
        self.assertIn("cannot be later than the RBI date, 2027-01-02", late.json()["detail"])
        self.assertEqual(post(name="on the day", rbi_interval_years=2, last_date="2025-01-01", next_date="2027-01-02").status_code, 201)
        self.assertEqual(post(name="earlier", rbi_interval_years=2, last_date="2025-01-01", next_date="2026-06-01").status_code, 201)
        self.assertEqual(post(name="no interval", rbi_interval_years=None, last_date="2025-01-01", next_date="2040-01-01").status_code, 201)   # no interval: no limit
        self.assertEqual(post(name="no last date", rbi_interval_years=2, last_date=None, next_date="2040-01-01").status_code, 201)
        # changing the dates is checked; another field of an older task with a later date is not
        t = self.client.post(API, headers=self.h["eng"], json=self.body(name="old", rbi_interval_years=None, last_date="2025-01-01", next_date="2040-01-01")).json()
        self.assertEqual(self.client.put(f"{API}/{t['id']}", headers=self.h["eng"], json={"duration_days": 9}).status_code, 200)
        self.assertEqual(self.client.put(f"{API}/{t['id']}", headers=self.h["eng"], json={"rbi_interval_years": 2}).status_code, 400)    # the later date no longer fits
        ok = self.client.put(f"{API}/{t['id']}", headers=self.h["eng"], json={"rbi_interval_years": 2, "next_date": "2027-01-02"})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_the_four_types_and_only_those(self):
        for i, kind in enumerate(("Internal", "External", "UTG", "NDE")):
            self.make(name=f"T-{i}", task_type=kind)
        r = self.client.post(API, headers=self.h["eng"], json=self.body(name="T-x", task_type="Visual"))
        self.assertEqual(r.status_code, 422)

    # ---------------------------------------------------------------- the rules
    def test_what_is_refused(self):
        post = lambda **kw: self.client.post(API, headers=self.h["eng"], json=self.body(**kw))
        self.assertEqual(post(name="  ").status_code, 422)                                         # a name is needed
        self.assertEqual(post(duration_days=-1).status_code, 422)
        self.assertEqual(post(rbi_interval_years=0).status_code, 422)
        self.assertEqual(post(rbi_interval_years=51).status_code, 422)
        self.assertEqual(post(equipment_id=str(uuid.uuid4())).status_code, 400)                    # an equipment that does not exist
        bad = post(last_date="2026-01-01", next_date="2025-01-01")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("next date cannot be before", bad.json()["detail"])
        self.make()
        dup = post(name="inspection task-1")                                                      # the same name on the same equipment
        self.assertEqual(dup.status_code, 400)
        self.assertIn("already has a task called", dup.json()["detail"])
        self.assertEqual(post(equipment_id=self.eq2).status_code, 201)                              # another equipment may reuse the name

    def test_roles(self):
        t = self.make()
        body = self.body(name="By someone else")
        for who, expected in (("eng", 201), ("adm", 201), ("view", 403), ("sup", 403)):
            self.assertEqual(self.client.post(API, headers=self.h[who], json=dict(body, name=f"n-{who}")).status_code, expected, who)
        for who in ("eng", "adm", "view", "sup"):                                                  # everybody reads
            self.assertEqual(self.client.get(API, headers=self.h[who]).status_code, 200, who)
            self.assertEqual(self.client.get(f"{API}/{t['id']}", headers=self.h[who]).status_code, 200, who)
        for who, expected in (("view", 403), ("sup", 403)):
            self.assertEqual(self.client.put(f"{API}/{t['id']}", headers=self.h[who], json={"details": "x"}).status_code, expected, who)
            self.assertEqual(self.client.delete(f"{API}/{t['id']}", headers=self.h[who]).status_code, 403, who)
        self.assertEqual(self.client.delete(f"{API}/{t['id']}", headers=self.h["eng"]).status_code, 403)   # deleting is an admin's job
        self.assertEqual(self.client.get(API).status_code, 401)                                            # not without signing in

    # ---------------------------------------------------------------- edit / delete / history
    def test_edit_changes_only_what_is_sent_and_is_audited(self):
        t = self.make()
        r = self.client.put(f"{API}/{t['id']}", headers=self.h["adm"], json={"rbi_interval_years": 5, "next_date": "2029-12-15"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["details"], float(r.json()["rbi_interval_years"]), r.json()["next_date"], r.json()["updated_by"]),
                         ("Check the shell", 5.0, "2029-12-15", "adm"))
        # the same values again change nothing and write nothing
        self.client.put(f"{API}/{t['id']}", headers=self.h["adm"], json={"rbi_interval_years": 5})
        lines = self.client.get(f"{API}/{t['id']}/history", headers=self.h["view"]).json()
        self.assertEqual([x["action"] for x in lines], ["task.edit", "task.create"])
        self.assertIn("rbi_interval_years: 7 -> 5", lines[0]["detail"])
        self.assertIn("next_date: 2031-12-15 -> 2029-12-15", lines[0]["detail"])
        self.assertEqual((lines[0]["actor"], "V-1" in lines[0]["detail"]), ("adm", True))
        # clearing a value is a change too
        r = self.client.put(f"{API}/{t['id']}", headers=self.h["eng"], json={"next_date": None})
        self.assertEqual((r.json()["next_date"], r.json()["status"]), (None, "none"))
        # a rename that clashes, and dates in the wrong order, are refused
        other = self.make(name="Second")
        self.assertEqual(self.client.put(f"{API}/{other['id']}", headers=self.h["eng"], json={"name": "Inspection Task-1"}).status_code, 400)
        self.assertEqual(self.client.put(f"{API}/{other['id']}", headers=self.h["eng"], json={"last_date": "2040-01-01"}).status_code, 400)
        self.assertEqual(self.client.put(f"{API}/{uuid.uuid4()}", headers=self.h["eng"], json={"details": "x"}).status_code, 404)

    def test_an_admin_deletes_and_it_is_audited(self):
        t = self.make()
        self.assertEqual(self.client.delete(f"{API}/{t['id']}", headers=self.h["adm"]).status_code, 204)
        self.assertEqual(self.client.get(f"{API}/{t['id']}", headers=self.h["adm"]).status_code, 404)
        self.assertEqual(self.client.delete(f"{API}/{t['id']}", headers=self.h["adm"]).status_code, 404)
        actions = [x["action"] for x in self.client.get(f"{API}/{t['id']}/history", headers=self.h["adm"]).json()]
        self.assertEqual(actions, ["task.delete", "task.create"])

    # ---------------------------------------------------------------- the badge, the list, the filters
    def test_the_badge_says_where_the_persons_own_next_date_stands(self):
        make = lambda name, d: self.make(name=name, next_date=None if d is None else iso(TODAY + timedelta(days=d)), last_date=None)
        self.assertEqual(make("late", -1)["status"], "overdue")
        self.assertEqual(make("today", 0)["status"], "due_soon")
        self.assertEqual(make("edge", 90)["status"], "due_soon")
        self.assertEqual(make("later", 91)["status"], "ok")
        self.assertEqual(make("none", None)["status"], "none")

    def test_the_list_filters_searches_and_counts(self):
        mk = lambda **kw: self.make(last_date=None, **kw)
        mk(name="A late internal", task_type="Internal", next_date=iso(TODAY - timedelta(days=5)))
        mk(name="B soon external", task_type="External", next_date=iso(TODAY + timedelta(days=30)))
        mk(name="C far UTG", task_type="UTG", next_date=iso(TODAY + timedelta(days=900)), details="thickness gauge")
        mk(name="D no date", task_type="NDE", next_date=None)
        mk(name="E other equipment", equipment_id=self.eq2, task_type="Internal", next_date=iso(TODAY + timedelta(days=500)))
        get = lambda q="": self.client.get(f"{API}{q}", headers=self.h["view"]).json()
        names = lambda r: [x["name"] for x in r["data"]]

        everything = get()
        self.assertEqual(everything["total"], 5)
        self.assertEqual(names(everything), ["A late internal", "B soon external", "E other equipment", "C far UTG", "D no date"])   # soonest first, no date last
        self.assertEqual(everything["counts"], {"overdue": 1, "due_soon": 1, "ok": 2, "none": 1})
        self.assertEqual(names(get(f"?equipment_id={self.eq2}")), ["E other equipment"])
        self.assertEqual(names(get("?task_type=UTG")), ["C far UTG"])
        self.assertEqual(names(get("?status=overdue")), ["A late internal"])
        self.assertEqual(get("?status=overdue")["counts"], {"overdue": 1, "due_soon": 1, "ok": 2, "none": 1})   # the badges ignore the status filter
        self.assertEqual(names(get("?q=gauge")), ["C far UTG"])                                       # in the details
        self.assertEqual(names(get("?q=E-2")), ["E other equipment"])                                  # by equipment tag
        page2 = get("?page=2&page_size=2")
        self.assertEqual((page2["total"], page2["total_pages"], names(page2)), (5, 3, ["E other equipment", "C far UTG"]))

    def test_tasks_of_removed_equipment_are_not_listed(self):
        self.make()
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{self.eq}", headers=self.h["adm"]).status_code, 200)
        self.assertEqual(self.client.get(API, headers=self.h["adm"]).json()["total"], 0)
        r = self.client.post(API, headers=self.h["eng"], json=self.body(name="after removal"))
        self.assertEqual(r.status_code, 400)                                                           # and none can be added to it

    def test_without_sign_in_it_works_as_before(self):
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.post(API, json=self.body())
            self.assertEqual((r.status_code, r.json()["created_by"]), (201, "System"))
            self.assertEqual(self.client.get(API).json()["total"], 1)
        finally:
            settings.AUTH_REQUIRED = True


if __name__ == "__main__":
    unittest.main()
