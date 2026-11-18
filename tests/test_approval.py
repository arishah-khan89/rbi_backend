"""Approval workflow of inspections, through the API with login switched on (own temporary database).

draft -> pending (ONE admin) -> approved (locked) | returned; reopen needs a reason; every step is in the audit trail.
Passwords are random per run. Run from backend/:  python -m unittest tests.test_approval -v
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
USERS = {"eng": "engineer", "eng2": "engineer", "adm1": "admin", "adm2": "admin", "sup": "super_admin", "view": "viewer"}
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


API = "/api/v1/inspection"


class Approval(unittest.TestCase):
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
        for t in ("audit_log", "notifications", "inspection_reading", "inspection_tml", "inspection_records", "equipment"):
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        db.close()
        r = self.client.post("/api/v1/equipment", headers=self.h["eng"], json={
            "equipment_id": "V-1", "equipment_name": "t", "equipment_type": "Pressure Vessel", "design_pressure": 10,
            "design_temperature": 150, "diameter": 1000, "material": "SA-516", "material_grade": "70",
            "min_required_thickness": 8.0, "nominal_thickness": 12})
        self.assertEqual(r.status_code, 201, r.text)
        self.eq = r.json()["id"]

    # ---------------------------------------------------------------- helpers
    def zone(self, code, vals):
        v = list(vals) + [None] * (4 - len(vals))
        return {"tml_code": code, "tml_name": code, "tml_type": "Shell", "direction_labels": "N,S,E,W",
                "reading_1": v[0], "reading_2": v[1], "reading_3": v[2], "reading_4": v[3]}

    def body(self, date="2026-03-01T00:00:00", zones=None):
        return {"equipment_id": self.eq, "equipment_tag": "V-1", "inspection_date": date, "inspector_name": "Ahmed",
                "tml_readings": zones or [self.zone("S-1", [10.0, 10.1])]}

    def create(self, user="eng", **kw):
        r = self.client.post(API, headers=self.h[user], json=self.body(**kw))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def post(self, user, rid, action, body=None):
        return self.client.post(f"{API}/{rid}/{action}", headers=self.h[user], json=body or {})

    def status(self, rid):
        return self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()["approval_status"]

    def pending_to(self, reviewer="adm1", user="eng"):
        rec = self.create(user)
        r = self.post(user, rec["id"], "submit", {"reviewer": reviewer})
        self.assertEqual(r.status_code, 200, r.text)
        return rec["id"]

    # ---------------------------------------------------------------- the happy path
    def test_full_flow_draft_pending_approved(self):
        rec = self.create()
        self.assertEqual(rec["approval_status"], "draft")
        r = self.post("eng", rec["id"], "submit", {"reviewer": "adm1", "comment": "please check S-1"})
        self.assertEqual((r.status_code, r.json()["approval_status"], r.json()["assigned_reviewer"], r.json()["submitted_by"]),
                         (200, "pending", "adm1", "eng"))
        self.assertEqual(r.json()["submit_comment"], "please check S-1")       # the reviewer reads the sender's note
        r = self.post("adm1", rec["id"], "approve", {"comment": "looks right"})
        self.assertEqual((r.status_code, r.json()["approval_status"], r.json()["reviewed_by"]), (200, "approved", "adm1"))
        lines = self.client.get(f"{API}/{rec['id']}/history", headers=self.h["eng"]).json()
        self.assertEqual([x["action"] for x in lines], ["inspection.create", "inspection.submit", "inspection.approve"])
        self.assertEqual({x["actor"] for x in lines}, {"eng", "adm1"})

    def test_a_returned_inspection_is_corrected_and_sent_again(self):
        rid = self.pending_to("adm1")
        self.assertEqual(self.post("adm1", rid, "return", {"comment": "S-1 reading 2 looks wrong"}).json()["approval_status"], "returned")
        rec = self.client.get(f"{API}/{rid}", headers=self.h["eng"]).json()
        self.assertEqual(rec["review_comment"], "S-1 reading 2 looks wrong")
        # it can be edited again and sent to ANOTHER admin
        r = self.client.put(f"{API}/{rid}", headers=self.h["eng"], json=self.body(zones=[self.zone("S-1", [10.0, 10.05])]))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.post("eng", rid, "submit", {"reviewer": "adm2"}).json()["assigned_reviewer"], "adm2")
        self.assertEqual(self.post("adm2", rid, "approve").json()["approval_status"], "approved")

    # ---------------------------------------------------------------- who may do what
    def test_only_the_assigned_admin_decides(self):
        rid = self.pending_to("adm1")
        self.assertEqual(self.post("adm2", rid, "approve").status_code, 403)            # another admin: no
        self.assertEqual(self.post("adm2", rid, "return", {"comment": "no thanks"}).status_code, 403)
        self.assertEqual(self.post("sup", rid, "approve").status_code, 403)             # not even a super admin
        self.assertEqual(self.post("eng", rid, "approve").status_code, 403)             # an engineer: role
        self.assertEqual(self.post("view", rid, "approve").status_code, 403)
        self.assertEqual(self.status(rid), "pending")
        self.assertEqual(self.post("adm1", rid, "approve").status_code, 200)

    def test_every_admin_sees_every_waiting_inspection_but_engineers_do_not(self):
        rid = self.pending_to("adm1")
        for u in ("adm1", "adm2", "sup"):
            ids = [r["id"] for r in self.client.get(f"{API}/approvals", headers=self.h[u]).json()]
            self.assertIn(rid, ids, u)
        self.assertEqual(self.client.get(f"{API}/approvals", headers=self.h["eng"]).status_code, 403)
        mine = [r["id"] for r in self.client.get(f"{API}/approvals?mine=true", headers=self.h["adm2"]).json()]
        self.assertNotIn(rid, mine)
        # the waiting list carries the readings the admin has to look at
        first = self.client.get(f"{API}/approvals", headers=self.h["adm1"]).json()[0]
        self.assertEqual(first["tml_readings"][0]["tml_code"], "S-1")

    def test_summary_counts_for_the_bell(self):
        rid = self.pending_to("adm1")
        s = lambda u: self.client.get(f"{API}/approvals/summary", headers=self.h[u]).json()
        self.assertEqual((s("adm1")["assigned_to_me"], s("adm2")["assigned_to_me"], s("adm2")["pending_total"]), (1, 0, 1))
        self.post("adm1", rid, "return", {"comment": "fix it please"})
        self.assertEqual((s("eng")["returned_to_me"], s("eng2")["returned_to_me"], s("adm1")["assigned_to_me"]), (1, 0, 0))

    def test_the_reviewer_must_be_an_active_admin_other_than_yourself(self):
        rec = self.create("adm1")
        for who, why in (("eng2", "an engineer"), ("view", "a viewer"), ("nobody", "unknown")):
            self.assertEqual(self.post("adm1", rec["id"], "submit", {"reviewer": who}).status_code, 400, why)
        self.assertEqual(self.post("adm1", rec["id"], "submit", {"reviewer": "adm1"}).status_code, 400)   # not yourself
        self.assertEqual(self.post("adm1", rec["id"], "submit", {"reviewer": "adm2"}).status_code, 200)
        self.assertEqual(self.post("view", rec["id"], "submit", {"reviewer": "adm2"}).status_code, 403)  # a viewer cannot submit at all

    def test_the_reviewer_list_has_only_admins(self):
        names = {r["username"] for r in self.client.get("/api/v1/auth/reviewers", headers=self.h["eng"]).json()}
        self.assertEqual(names, {"adm1", "adm2"})                                       # no inspector, no viewer, no super admin
        rec = self.create()
        self.assertEqual(self.post("eng", rec["id"], "submit", {"reviewer": "sup"}).status_code, 400)   # the super admin is not a reviewer

    def test_the_admin_chain_inspector_to_admin_under_one_super_admin(self):
        # the client's structure: inspectors enter, several admins review, the super admin is above them
        for admin, inspector, date in (("adm1", "eng", "2026-03-01T00:00:00"), ("adm2", "eng2", "2026-04-01T00:00:00")):
            rid = self.create(inspector, date=date)["id"]
            self.assertEqual(self.post(inspector, rid, "submit", {"reviewer": admin}).status_code, 200)
            self.assertEqual(self.post(admin, rid, "approve").status_code, 200, admin)
            self.assertEqual(self.client.get("/api/v1/auth/users", headers=self.h[admin]).status_code, 403)   # admins do not manage users
        self.assertEqual(self.client.get("/api/v1/auth/users", headers=self.h["sup"]).status_code, 200)

    def test_an_admin_with_waiting_inspections_cannot_be_deleted_until_they_decide(self):
        # an admin of its own, so the other tests keep theirs
        pw = secrets.token_urlsafe(14)
        db = Session()
        db.add(User(username="adm.temp", role="admin", password_hash=security.hash_password(pw)))
        db.commit()
        db.close()
        self.h["adm.temp"] = {"Authorization": "Bearer " + self.client.post(
            "/api/v1/auth/login", json={"username": "adm.temp", "password": pw}).json()["access_token"]}
        rid = self.pending_to("adm.temp")
        r = self.client.delete("/api/v1/auth/users/adm.temp", headers=self.h["sup"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("waiting for approval", r.json()["detail"])
        self.assertEqual(self.post("adm.temp", rid, "approve").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/auth/users/adm.temp", headers=self.h["sup"]).status_code, 204)
        rec = self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()
        self.assertEqual((rec["approval_status"], rec["reviewed_by"]), ("approved", "adm.temp"))   # the name stays on the record

    def test_the_inspector_gets_a_list_of_what_was_returned_with_the_reason(self):
        rid = self.pending_to("adm1", user="eng")
        returned = lambda user: self.client.get(f"{API}/returned-to-me", headers=self.h[user])
        self.assertEqual(returned("eng").json(), [])                                     # nothing returned yet
        self.post("adm1", rid, "return", {"comment": "S-1 reading 2 looks wrong"})
        rows = returned("eng").json()
        self.assertEqual([(r["id"], r["approval_status"], r["review_comment"], r["reviewed_by"]) for r in rows],
                         [(rid, "returned", "S-1 reading 2 looks wrong", "adm1")])
        self.assertEqual(rows[0]["tml_readings"][0]["tml_code"], "S-1")                  # the readings come along, ready to edit
        self.assertEqual(returned("eng2").json(), [])                                    # only the sender's own list
        self.assertEqual(returned("adm1").json(), [])
        self.assertEqual(self.client.get(f"{API}/approvals/summary", headers=self.h["eng"]).json()["returned_to_me"], 1)
        # sent again -> it leaves the list
        self.assertEqual(self.post("eng", rid, "submit", {"reviewer": "adm2"}).status_code, 200)
        self.assertEqual(returned("eng").json(), [])

    # ---------------------------------------------------------------- t_min: locked inspections are frozen, changes are audited
    EQ = "/api/v1/equipment"

    def two_readings(self):
        """A (2025) 10.0 mm and B (2026) 9.0 mm: corrosion rate 1 mm/yr, remaining life (9.0 - t_min) / 1."""
        a = self.create("eng", date="2025-03-01T00:00:00", zones=[self.zone("S-1", [10.0, 10.0, 10.0, 10.0])])["id"]
        b = self.create("eng", date="2026-03-01T00:00:00", zones=[self.zone("S-1", [9.0, 9.0, 9.0, 9.0])])["id"]
        return a, b

    def rec(self, rid):
        return self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()

    def equipment(self):
        return self.client.get(f"{self.EQ}/{self.eq}", headers=self.h["adm1"]).json()

    def set_t_min(self, value, reason="re-assessed", user="adm1"):
        body = {"min_required_thickness": value}
        if reason is not None:
            body["change_reason"] = reason
        return self.client.put(f"{self.EQ}/{self.eq}", headers=self.h[user], json=body)

    def test_an_approved_inspection_keeps_its_figures_when_the_equipment_t_min_changes(self):
        a, b = self.two_readings()
        self.assertEqual(float(self.rec(b)["remaining_life"]), 1.0)                         # (9.0 - 8.0) / 1.0
        self.post("eng", b, "submit", {"reviewer": "adm1"})
        self.post("adm1", b, "approve")
        life_then = float(self.equipment()["remaining_life"])
        self.assertEqual(life_then, 1.0)

        self.assertEqual(self.set_t_min(8.5).status_code, 200)

        frozen = self.rec(b)
        self.assertEqual(float(frozen["remaining_life"]), 1.0)                              # the approved figure did not move
        self.assertEqual(float(frozen["corrosion_rate"]), 1.0)
        self.assertEqual(float(frozen["tml_readings"][0]["min_required_thickness"]), 8.0)   # it shows the t_min it was approved with
        self.assertIn("S-1: 8 mm then, 8.5 mm now", frozen["t_min_note"])
        self.assertEqual(float(self.equipment()["remaining_life"]), 0.5)                    # the equipment's CURRENT life uses 8.5

        # reopened: it follows the equipment data again
        self.post("adm1", b, "reopen", {"reason": "re-check with the new t_min"})
        reopened = self.rec(b)
        self.assertEqual((reopened["approval_status"], float(reopened["remaining_life"]), reopened["t_min_note"]), ("returned", 0.5, None))
        self.assertEqual(float(reopened["tml_readings"][0]["min_required_thickness"]), 8.5)

    def test_an_inspection_waiting_for_approval_is_frozen_too_and_a_draft_follows_the_data(self):
        a, b = self.two_readings()                                                          # both drafts
        self.post("eng", b, "submit", {"reviewer": "adm1"})                                 # b locked, a still a draft
        self.set_t_min(8.5)
        self.assertEqual(float(self.rec(b)["remaining_life"]), 1.0)                         # pending: frozen
        self.assertIsNotNone(self.rec(b)["t_min_note"])
        self.assertIsNone(self.rec(a)["remaining_life"])                                    # (a is the baseline: no life yet)
        c = self.create("eng", date="2026-09-01T00:00:00", zones=[self.zone("S-1", [8.0, 8.0, 8.0, 8.0])])["id"]
        self.assertEqual(float(self.rec(c)["tml_readings"][0]["min_required_thickness"]), 8.5)   # a new one uses today's t_min

    def test_an_old_locked_inspection_without_a_snapshot_is_protected_the_first_time_the_data_changes(self):
        a, b = self.two_readings()
        self.post("eng", b, "submit", {"reviewer": "adm1"})
        self.post("adm1", b, "approve")
        db = Session()                                                                       # as it was before snapshots existed
        db.execute(text("UPDATE inspection_records SET locked_snapshot = NULL WHERE id = :i"), {"i": b})
        db.commit()
        db.close()
        self.set_t_min(8.5)
        frozen = self.rec(b)
        self.assertEqual((float(frozen["remaining_life"]), float(frozen["tml_readings"][0]["min_required_thickness"])), (1.0, 8.0))

    def test_changing_a_t_min_needs_a_reason_and_everything_is_in_the_audit_trail(self):
        no_reason = self.set_t_min(8.5, reason=None)
        self.assertEqual(no_reason.status_code, 400)
        self.assertIn("reason", no_reason.json()["detail"])
        self.assertEqual(self.set_t_min(8.5, reason="ab").status_code, 400)                  # a real sentence, not two letters
        self.assertEqual(float(self.equipment()["min_required_thickness"]), 8.0)              # nothing was saved
        self.assertEqual(self.set_t_min(8.5, reason="re-assessed after the 2026 survey").status_code, 200)
        # a change that is not a t_min needs no reason, and is audited as well
        self.assertEqual(self.client.put(f"{self.EQ}/{self.eq}", headers=self.h["eng"], json={"plant": "Plant 9"}).status_code, 200)
        # saving the same values again changes nothing, so nothing is written
        self.assertEqual(self.client.put(f"{self.EQ}/{self.eq}", headers=self.h["eng"], json={"plant": "Plant 9", "min_required_thickness": 8.5}).status_code, 200)

        lines = self.client.get(f"{self.EQ}/{self.eq}/history", headers=self.h["view"]).json()
        self.assertEqual([x["action"] for x in lines], ["equipment.edit", "equipment.edit", "equipment.create"])
        tmin, plant, created = lines[1], lines[0], lines[2]
        self.assertEqual((tmin["actor"], "min_required_thickness: 8 -> 8.5" in tmin["detail"]), ("adm1", True))
        self.assertIn("reason: re-assessed after the 2026 survey", tmin["detail"])
        self.assertIn("plant:", plant["detail"])
        self.assertNotIn("remaining_life", tmin["detail"] + plant["detail"])               # calculated figures are not "changes"
        self.assertEqual((created["actor"], "V-1" in created["detail"]), ("eng", True))
        # the super admin reads them in the audit log
        rows = self.client.get("/api/v1/auth/audit/search?action=equipment.", headers=self.h["sup"]).json()
        self.assertEqual(rows["total"], 3)

    def test_without_sign_in_a_t_min_change_needs_no_reason(self):
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.put(f"{self.EQ}/{self.eq}", json={"min_required_thickness": 8.5})
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            settings.AUTH_REQUIRED = True

    def test_removing_equipment_is_audited(self):
        self.assertEqual(self.client.delete(f"{self.EQ}/{self.eq}", headers=self.h["adm1"]).status_code, 200)
        actions = [x["action"] for x in self.client.get(f"{self.EQ}/{self.eq}/history", headers=self.h["adm1"]).json()]
        self.assertEqual(actions, ["equipment.delete", "equipment.create"])

    # ---------------------------------------------------------------- Notifications
    NOTE = "/api/v1/notifications"

    def notes(self, user, q=""):
        r = self.client.get(f"{self.NOTE}{q}", headers=self.h[user])
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_every_step_of_the_workflow_tells_the_person_it_concerns(self):
        rid = self.create("eng")["id"]
        self.assertEqual(self.notes("adm1")["unread"], 0)
        self.post("eng", rid, "submit", {"reviewer": "adm1", "comment": "please check S-1"})
        n = self.notes("adm1")
        self.assertEqual((n["unread"], n["items"][0]["kind"], n["items"][0]["detail"]), (1, "assigned", "please check S-1"))
        self.assertIn("eng sent you the inspection of V-1", n["items"][0]["message"])
        self.assertEqual(n["items"][0]["record_id"], rid)
        self.assertEqual(self.notes("eng")["unread"], 0)                                   # nobody is told what they did themselves
        self.assertEqual(self.notes("adm2")["unread"], 0)

        self.post("adm1", rid, "return", {"comment": "reading 2 looks wrong"})
        e = self.notes("eng")["items"][0]
        self.assertEqual((e["kind"], e["detail"]), ("returned", "reading 2 looks wrong"))
        self.post("eng", rid, "submit", {"reviewer": "adm1"})
        self.post("adm1", rid, "approve", {"comment": "ok now"})
        self.assertEqual([(x["kind"], x["detail"]) for x in self.notes("eng")["items"][:2]], [("approved", "ok now"), ("returned", "reading 2 looks wrong")])
        self.post("adm1", rid, "reopen", {"reason": "wrong zone"})
        self.assertEqual(self.notes("eng")["items"][0]["kind"], "reopened")
        self.assertEqual(self.notes("eng")["items"][0]["detail"], "wrong zone")

    def test_a_withdrawn_inspection_tells_the_reviewer(self):
        rid = self.pending_to("adm1")
        self.post("eng", rid, "withdraw")
        kinds = [x["kind"] for x in self.notes("adm1")["items"]]
        self.assertEqual(kinds, ["withdrawn", "assigned"])

    def test_notifications_are_read_one_by_one_or_all_at_once_and_only_by_their_owner(self):
        for d in ("2026-01-01T00:00:00", "2026-02-01T00:00:00", "2026-03-01T00:00:00"):
            self.pending_to_date("adm1", d)
        n = self.notes("adm1")
        self.assertEqual((n["unread"], len(n["items"])), (3, 3))
        first = n["items"][0]["id"]
        self.assertEqual(self.client.post(f"{self.NOTE}/{first}/read", headers=self.h["adm2"]).status_code, 404)   # not theirs
        self.assertEqual(self.notes("adm1")["unread"], 3)
        r = self.client.post(f"{self.NOTE}/{first}/read", headers=self.h["adm1"])
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["read_at"])
        self.assertEqual(self.notes("adm1")["unread"], 2)
        self.assertEqual(len(self.notes("adm1", "?unread_only=true")["items"]), 2)
        self.assertEqual(self.client.post(f"{self.NOTE}/read-all", headers=self.h["adm1"]).json()["marked"], 2)
        n = self.notes("adm1")
        self.assertEqual((n["unread"], len(n["items"])), (0, 3))                           # read ones stay in the list

    def pending_to_date(self, reviewer, date, user="eng"):
        rid = self.create(user, date=date)["id"]
        self.assertEqual(self.post(user, rid, "submit", {"reviewer": reviewer}).status_code, 200)
        return rid

    def test_every_role_has_its_own_list_and_can_mark_it_read(self):
        for who in ("view", "sup", "eng2", "adm2"):                                         # a viewer and the super admin too
            self.assertEqual(self.notes(who)["unread"], 0, who)
            self.assertEqual(self.client.post(f"{self.NOTE}/read-all", headers=self.h[who]).status_code, 200, who)
        self.assertEqual(self.client.get(self.NOTE).status_code, 401)                       # not without signing in

    def test_old_notifications_are_removed(self):
        self.pending_to("adm1")
        db = Session()
        db.execute(text("UPDATE notifications SET created_at = now() - interval '40 days' WHERE recipient = 'adm1'"))
        db.commit()
        db.close()
        self.assertEqual(self.notes("adm1"), {"unread": 0, "items": []})

    def test_an_import_sends_the_reviewer_one_message_for_the_whole_file(self):
        rows = [self.row(datetime(2025, 3, 1)), self.row(datetime(2026, 3, 1), vals=(9.9, 10.0)), self.row(datetime(2026, 4, 1), vals=(9.8, 9.9))]
        self.assertEqual(self.do_import("eng", rows, reviewer="adm1").json()["sent_for_approval"], 3)
        n = self.notes("adm1")
        self.assertEqual((n["unread"], n["items"][0]["kind"]), (1, "assigned"))
        self.assertIn("3 imported inspection(s) from t.xlsx", n["items"][0]["message"])

    def test_without_sign_in_there_are_no_notifications(self):
        settings.AUTH_REQUIRED = False
        try:
            self.assertEqual(self.client.get(self.NOTE).json(), {"unread": 0, "items": []})
        finally:
            settings.AUTH_REQUIRED = True

    # ---------------------------------------------------------------- My tasks
    def test_my_tasks_shows_each_person_what_waits_for_them(self):
        draft = self.create("eng", date="2026-01-01T00:00:00")["id"]
        sent = self.create("eng", date="2026-02-01T00:00:00")["id"]
        back = self.create("eng", date="2026-03-01T00:00:00")["id"]
        done = self.create("eng", date="2026-04-01T00:00:00")["id"]
        for rid in (sent, back, done):
            self.assertEqual(self.post("eng", rid, "submit", {"reviewer": "adm1"}).status_code, 200)
        self.post("adm1", back, "return", {"comment": "fix it"})
        self.post("adm1", done, "approve")
        tasks = lambda user: self.client.get(f"{API}/my-tasks", headers=self.h[user]).json()
        ids = lambda items: [x["id"] for x in items]

        t = tasks("eng")
        self.assertEqual((ids(t["drafts"]), ids(t["waiting_for_approval"]), ids(t["returned"]), ids(t["recently_approved"])),
                         ([draft], [sent], [back], [done]))
        self.assertEqual((t["drafts_total"], t["waiting_for_approval_total"], t["returned_total"], t["awaiting_my_decision_total"]), (1, 1, 1, 0))
        self.assertEqual(t["returned"][0]["review_comment"], "fix it")
        self.assertEqual(t["waiting_for_approval"][0]["assigned_reviewer"], "adm1")
        self.assertEqual(t["pending_in_system"], 1)

        a = tasks("adm1")                                                                  # the admin it was sent to
        self.assertEqual((ids(a["awaiting_my_decision"]), a["awaiting_my_decision_total"]), ([sent], 1))
        self.assertEqual((a["drafts"], a["returned"], a["waiting_for_approval"]), ([], [], []))   # nothing of his own
        self.assertEqual(tasks("adm2")["awaiting_my_decision"], [])                         # another admin: not his
        for other in ("eng2", "view"):                                                      # nothing is theirs
            o = tasks(other)
            self.assertEqual((o["drafts"], o["returned"], o["waiting_for_approval"], o["recently_approved"], o["awaiting_my_decision"]),
                             ([], [], [], [], []), other)
        s = tasks("sup")                                                                    # the super admin overview
        self.assertEqual((s["pending_in_system"], s["awaiting_my_decision"], s["drafts"]), (1, [], []))

    def test_my_tasks_follow_the_work(self):
        rid = self.create("eng")["id"]
        tasks = lambda: self.client.get(f"{API}/my-tasks", headers=self.h["eng"]).json()
        self.assertEqual(tasks()["drafts_total"], 1)
        self.post("eng", rid, "submit", {"reviewer": "adm1"})
        self.assertEqual((tasks()["drafts_total"], tasks()["waiting_for_approval_total"]), (0, 1))
        self.post("adm1", rid, "return", {"comment": "again"})
        self.assertEqual((tasks()["waiting_for_approval_total"], tasks()["returned_total"]), (0, 1))
        self.post("eng", rid, "submit", {"reviewer": "adm1"})
        self.post("adm1", rid, "approve")
        t = tasks()
        self.assertEqual((t["returned_total"], len(t["recently_approved"])), (0, 1))
        self.post("adm1", rid, "reopen", {"reason": "wrong zone"})                          # reopened -> back with the owner
        self.assertEqual((tasks()["returned_total"], len(tasks()["recently_approved"])), (1, 0))

    # ---------------------------------------------------------------- whose inspection it is
    def test_only_the_owner_changes_or_sends_a_draft(self):
        rec = self.create("eng")                                                          # eng's draft
        mine = {"eng": 200}
        for who in ("eng2", "adm1"):                                               # not another inspector and not an admin
            put = self.client.put(f"{API}/{rec['id']}", headers=self.h[who], json=self.body(zones=[self.zone("S-1", [9.0])]))
            self.assertEqual(put.status_code, 403, who)
            self.assertIn("belongs to eng", put.json()["detail"])
            self.assertEqual(self.post(who, rec["id"], "submit", {"reviewer": "adm2"}).status_code, 403, who)
        # the same date as a new inspection (a zone added to the draft) is refused too
        other = self.client.post(API, headers=self.h["eng2"], json=self.body(zones=[self.zone("S-2", [9.0, 9.1])]))
        self.assertEqual(other.status_code, 403, other.text)
        self.assertEqual(self.status(rec["id"]), "draft")
        # the owner can
        self.assertEqual(self.client.put(f"{API}/{rec['id']}", headers=self.h["eng"], json=self.body(zones=[self.zone("S-1", [9.5, 9.6])])).status_code, mine["eng"])
        self.assertEqual(self.post("eng", rec["id"], "submit", {"reviewer": "adm1"}).status_code, 200)

    def test_a_returned_inspection_stays_with_the_person_who_sent_it(self):
        rid = self.pending_to("adm1", user="eng")
        self.post("adm1", rid, "return", {"comment": "fix reading 2"})
        self.assertEqual(self.client.put(f"{API}/{rid}", headers=self.h["eng2"], json=self.body()).status_code, 403)
        self.assertEqual(self.post("eng2", rid, "submit", {"reviewer": "adm2"}).status_code, 403)
        self.assertEqual(self.client.get(f"{API}/returned-to-me", headers=self.h["eng"]).json()[0]["id"], rid)   # still in eng's box
        self.assertEqual(self.post("eng", rid, "submit", {"reviewer": "adm2"}).status_code, 200)

    def test_a_record_without_an_owner_can_be_worked_on_by_anybody_who_may_write(self):
        rec = self.create("eng")
        db = Session()                                                                    # an old record: no owner on file
        db.execute(text("UPDATE inspection_records SET created_by = 'System', submitted_by = NULL WHERE id = :i"), {"i": rec["id"]})
        db.commit()
        db.close()
        self.assertEqual(self.client.put(f"{API}/{rec['id']}", headers=self.h["eng2"], json=self.body()).status_code, 200)
        self.assertEqual(self.post("eng2", rec["id"], "submit", {"reviewer": "adm1"}).status_code, 200)

    def test_an_import_does_not_replace_a_draft_of_somebody_else(self):
        draft = self.create("eng")                                                        # eng's draft on 2026-03-01
        r = self.do_import("eng2", [self.row(datetime(2026, 3, 1), vals=(9.5, 9.6))], reviewer="adm1")
        self.assertEqual(r.json()["status"], "rejected")
        self.assertTrue(any("belongs to eng" in e for e in r.json()["errors"]), r.json()["errors"])
        self.assertEqual(self.status(draft["id"]), "draft")
        mine = self.do_import("eng", [self.row(datetime(2026, 3, 1), vals=(9.5, 9.6))], reviewer="adm1")     # the owner may
        self.assertEqual(mine.json()["status"], "success")

    def test_an_imported_inspection_belongs_to_the_person_who_imported_it(self):
        r = self.do_import("eng", [self.row(datetime(2026, 5, 1))], reviewer="adm1")
        self.assertEqual(r.json()["status"], "success")
        rec = self.all_records()[0]
        self.post("adm1", rec["id"], "return", {"comment": "again"})
        self.assertEqual(self.client.put(f"{API}/{rec['id']}", headers=self.h["eng2"], json=self.body(date="2026-05-01T00:00:00")).status_code, 403)
        self.assertEqual(rec["created_by"], "eng")

    # ---------------------------------------------------------------- what an admin decided
    def test_an_admin_keeps_a_list_of_what_they_approved_and_returned(self):
        a = self.create("eng", date="2026-03-01T00:00:00")["id"]
        b = self.create("eng", date="2026-04-01T00:00:00")["id"]
        for rid in (a, b):
            self.assertEqual(self.post("eng", rid, "submit", {"reviewer": "adm1"}).status_code, 200)
        self.post("adm1", a, "approve", {"comment": "looks right"})
        self.post("adm1", b, "return", {"comment": "S-1 reading 2 is wrong"})
        decided = lambda user, q="": self.client.get(f"{API}/approvals/decided{q}", headers=self.h[user])

        rows = decided("adm1").json()
        self.assertEqual({(r["decision"], r["comment"], r["equipment_tag"], r["current_status"]) for r in rows},
                         {("approved", "looks right", "V-1", "approved"), ("returned", "S-1 reading 2 is wrong", "V-1", "returned")})
        self.assertEqual({r["decided_by"] for r in rows}, {"adm1"})
        self.assertEqual(rows[0]["inspector_name"], "Eng")                          # the name comes from the sign-in
        self.assertEqual([r["decision"] for r in decided("adm1", "?decision=approved").json()], ["approved"])
        self.assertEqual([r["decision"] for r in decided("adm1", "?decision=returned").json()], ["returned"])
        self.assertEqual(decided("adm1", "?decision=maybe").status_code, 400)
        self.assertEqual(decided("adm1", "?date_from=01-01-2026").status_code, 400)
        self.assertEqual(decided("adm1", "?date_from=2999-01-01").json(), [])

        self.assertEqual(decided("adm2").json(), [])                                     # another admin sees only their own
        self.assertEqual(len(decided("sup").json()), 2)                                  # the super admin oversees all admins
        self.assertEqual(decided("eng").status_code, 403)
        self.assertEqual(decided("view").status_code, 403)

        # the list keeps the decision even when the inspection moves on (here: reopened)
        self.assertEqual(self.post("adm1", a, "reopen", {"reason": "wrong zone"}).status_code, 200)
        statuses = {r["decision"]: r["current_status"] for r in decided("adm1").json()}
        self.assertEqual(statuses["approved"], "returned")

    # ---------------------------------------------------------------- the lock
    def test_pending_and_approved_are_locked(self):
        rid = self.pending_to("adm1")
        for stage in ("pending", "approved"):
            if stage == "approved":
                self.assertEqual(self.post("adm1", rid, "approve").status_code, 200)
            put = self.client.put(f"{API}/{rid}", headers=self.h["eng"], json=self.body(zones=[self.zone("S-1", [9.0])]))
            self.assertEqual(put.status_code, 400, stage)
            self.assertIn("locked", put.json()["detail"])
            self.assertEqual(self.client.delete(f"{API}/{rid}", headers=self.h["adm1"]).status_code, 400, stage)       # not even an admin
            self.assertEqual(self.client.delete(f"{API}/{rid}", headers=self.h["sup"]).status_code, 403, stage)       # the super admin does not delete data at all
            add = self.client.post(API, headers=self.h["eng"], json=self.body(zones=[self.zone("S-2", [9.0])]))      # a new zone, same date
            self.assertEqual(add.status_code, 400, stage)
            self.assertIn("locked", add.json()["detail"])
            tml = self.client.get(f"/api/v1/cml/equipment/{self.eq}", headers=self.h["eng"]).json()[0]["id"]            # the direct CML route
            direct = self.client.post(f"/api/v1/cml/tml/{tml}/reading", headers=self.h["eng"], json={
                "inspection_date": "2026-03-01T00:00:00", "reading_1": 5.0})
            self.assertEqual(direct.status_code, 409, stage)
        # nothing changed
        rec = self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()
        self.assertEqual(rec["min_measured_thickness"], "10.0000")

    def test_withdraw_makes_it_editable_again(self):
        rid = self.pending_to("adm1")
        self.assertEqual(self.post("eng2", rid, "withdraw").status_code, 403)       # not the sender
        self.assertEqual(self.post("eng", rid, "withdraw").json()["approval_status"], "draft")
        self.assertEqual(self.client.put(f"{API}/{rid}", headers=self.h["eng"], json=self.body()).status_code, 200)
        self.assertEqual(self.post("adm1", rid, "approve").status_code, 400)        # nothing waiting any more

    # ---------------------------------------------------------------- reopen
    def test_reopen_needs_a_reason_and_the_right_person(self):
        rid = self.pending_to("adm1")
        self.post("adm1", rid, "approve")
        self.assertEqual(self.post("eng", rid, "reopen", {"reason": "typo found"}).status_code, 403)
        self.assertEqual(self.post("adm2", rid, "reopen", {"reason": "typo found"}).status_code, 403)    # another admin: no
        self.assertEqual(self.post("adm1", rid, "reopen", {}).status_code, 422)                          # no reason
        self.assertEqual(self.post("adm1", rid, "reopen", {"reason": "ab"}).status_code, 422)            # too short
        r = self.post("adm1", rid, "reopen", {"reason": "reading 2 was a typing mistake"})
        self.assertEqual((r.status_code, r.json()["approval_status"]), (200, "returned"))
        self.assertIn("Reopened: reading 2", r.json()["review_comment"])
        self.assertEqual(self.client.put(f"{API}/{rid}", headers=self.h["eng"], json=self.body()).status_code, 200)   # editable again
        actions = [x["action"] for x in self.client.get(f"{API}/{rid}/history", headers=self.h["eng"]).json()]
        self.assertIn("inspection.reopen", actions)

    def test_a_super_admin_can_reopen_any_approved_inspection(self):
        rid = self.pending_to("adm1")
        self.post("adm1", rid, "approve")
        self.assertEqual(self.post("sup", rid, "reopen", {"reason": "audit finding"}).json()["approval_status"], "returned")

    def test_only_an_approved_inspection_can_be_reopened(self):
        rid = self.pending_to("adm1")
        self.assertEqual(self.post("adm1", rid, "reopen", {"reason": "not approved yet"}).status_code, 400)

    # ---------------------------------------------------------------- the rest
    def test_return_needs_a_comment(self):
        rid = self.pending_to("adm1")
        self.assertEqual(self.post("adm1", rid, "return", {}).status_code, 422)
        self.assertEqual(self.post("adm1", rid, "return", {"comment": ""}).status_code, 422)

    def test_a_draft_cannot_be_approved_and_cannot_be_submitted_twice(self):
        rec = self.create()
        self.assertEqual(self.post("adm1", rec["id"], "approve").status_code, 400)
        self.assertEqual(self.post("eng", rec["id"], "submit", {"reviewer": "adm1"}).status_code, 200)
        self.assertEqual(self.post("eng", rec["id"], "submit", {"reviewer": "adm2"}).status_code, 400)

    def test_an_import_cannot_replace_a_locked_inspection(self):
        rid = self.pending_to("adm1")
        self.post("adm1", rid, "approve")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inspection Data"
        ws.append(["Equipment Tag", "Inspection Date", "Inspector Name", "TML Code", "TML Name", "TML Type", "Direction Labels",
                   "Reading 1 (mm)", "Reading 2 (mm)", "Reading 3 (mm)", "Reading 4 (mm)", "Min Required Thickness (mm)", "Remarks"])
        ws.append(["V-1", datetime(2026, 3, 1), "x", "S-1", "S-1", "Shell", "N, S", 9.0, 9.1, None, None, None, None])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.client.post(f"{API}/import-excel", headers=self.h["eng"], data={"reviewer": "adm2"}, files={
            "file": ("t.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertTrue(any("approved and locked" in e for e in r.json()["errors"]), r.json()["errors"])
        self.assertEqual(self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()["min_measured_thickness"], "10.0000")

    def test_approvals_need_sign_in(self):
        rid = self.pending_to("adm1")
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.post(f"{API}/{rid}/approve", json={})
            self.assertEqual(r.status_code, 400)
            self.assertIn("sign-in", r.json()["detail"])
        finally:
            settings.AUTH_REQUIRED = True

    # ---------------------------------------------------------------- import -> approval
    HEADER = ["Equipment Tag", "Inspection Date", "Inspector Name", "TML Code", "TML Name", "TML Type", "Direction Labels",
              "Reading 1 (mm)", "Reading 2 (mm)", "Reading 3 (mm)", "Reading 4 (mm)", "Min Required Thickness (mm)", "Remarks"]

    def workbook(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inspection Data"
        ws.append(self.HEADER)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def do_import(self, user, rows, reviewer=None):
        data = {"reviewer": reviewer} if reviewer is not None else {}
        return self.client.post(f"{API}/import-excel", headers=self.h[user], data=data, files={
            "file": ("t.xlsx", self.workbook(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})

    def row(self, date, code="S-1", vals=(10.0, 10.1)):
        v = list(vals) + [None] * (4 - len(vals))
        return ["V-1", date, "Ahmed", code, code, "Shell", "N, S, E, W", *v, None, None]

    def all_records(self):
        return self.client.get(f"{API}?equipment_id={self.eq}&page_size=50", headers=self.h["adm1"]).json()["data"]

    def test_an_import_is_sent_to_the_chosen_reviewer(self):
        r = self.do_import("eng", [self.row(datetime(2025, 3, 1)), self.row(datetime(2026, 3, 1), vals=(9.9, 10.0))], reviewer="adm1")
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()
        self.assertEqual((res["status"], res["records_created"], res["sent_for_approval"], res["reviewer"]), ("success", 2, 2, "adm1"))
        recs = self.all_records()
        self.assertEqual({x["approval_status"] for x in recs}, {"pending"})
        self.assertEqual({(x["assigned_reviewer"], x["submitted_by"]) for x in recs}, {("adm1", "eng")})
        # locked from the start, visible to the reviewer, and the history says where it came from
        self.assertEqual(self.client.put(f"{API}/{recs[0]['id']}", headers=self.h["eng"], json=self.body()).status_code, 400)
        waiting = self.client.get(f"{API}/approvals?mine=true", headers=self.h["adm1"]).json()
        self.assertEqual(len(waiting), 2)
        lines = self.client.get(f"{API}/{recs[0]['id']}/history", headers=self.h["adm1"]).json()
        self.assertEqual(lines[0]["action"], "inspection.create")
        submit = next(x for x in lines if x["action"] == "inspection.submit")
        self.assertIn("Imported from t.xlsx", submit["detail"])
        self.assertEqual(self.post("adm1", recs[0]["id"], "approve").status_code, 200)

    def test_an_import_needs_a_valid_reviewer_and_otherwise_does_nothing(self):
        rows = [self.row(datetime(2026, 3, 1))]
        for who, why in ((None, "none chosen"), ("eng2", "an engineer"), ("nobody", "unknown"), ("view", "a viewer")):
            r = self.do_import("eng", rows, reviewer=who)
            self.assertEqual(r.status_code, 400, why)
            self.assertIn("review this import", r.json()["detail"])
        r = self.do_import("adm1", rows, reviewer="adm1")                      # an admin cannot review their own import
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.all_records(), [])                                 # nothing was imported by any of them

    def test_an_import_that_is_rejected_sends_nothing(self):
        bad = self.row(datetime(2026, 3, 1))
        bad[5] = "Pipe"                                                          # not a zone type
        r = self.do_import("eng", [bad], reviewer="adm1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json().get("sent_for_approval")), ("rejected", 0))
        self.assertEqual(self.all_records(), [])

    def test_an_import_over_a_returned_inspection_replaces_it_and_sends_it_again(self):
        rid = self.pending_to("adm1")                                            # 2026-03-01, S-1 10.0 / 10.1
        self.post("adm1", rid, "return", {"comment": "reading is wrong"})
        r = self.do_import("eng", [self.row(datetime(2026, 3, 1), vals=(9.5, 9.6))], reviewer="adm2")
        self.assertEqual((r.json()["status"], r.json()["records_updated"], r.json()["sent_for_approval"]), ("success", 1, 1))
        rec = self.client.get(f"{API}/{rid}", headers=self.h["adm1"]).json()
        self.assertEqual((rec["approval_status"], rec["assigned_reviewer"], rec["min_measured_thickness"]), ("pending", "adm2", "9.5000"))
        self.assertEqual(self.post("adm1", rid, "approve").status_code, 403)    # it now belongs to adm2
        self.assertEqual(self.post("adm2", rid, "approve").status_code, 200)

    def test_without_sign_in_an_import_works_as_before(self):
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.post(f"{API}/import-excel", files={
                "file": ("t.xlsx", self.workbook([self.row(datetime(2026, 3, 1))]),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            self.assertEqual((r.status_code, r.json()["status"], "sent_for_approval" in r.json()), (200, "success", False))
        finally:
            settings.AUTH_REQUIRED = True


if __name__ == "__main__":
    unittest.main()
