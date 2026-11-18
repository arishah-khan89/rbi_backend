"""Inspection reports (tab "Inspection Report"): create / change / list, tasks, files, approval, notifications, roles.
Through the API with sign-in on, on a TEMPORARY database. Run from backend/:  python -m unittest tests.test_inspection_reports -v
"""
import os
import secrets
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.endpoints import auth as auth_endpoints  # noqa: E402
from app.api.endpoints import inspection_reports as report_endpoints  # noqa: E402
from app.core import security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
import app.models.equipment, app.models.inspection, app.models.cml, app.models.user, app.models.audit  # noqa: E402,F401
import app.models.inspection_task, app.models.inspection_report, app.models.notification  # noqa: E402,F401
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


R = "/api/v1/inspection-reports"
T = "/api/v1/inspection-tasks"
N = "/api/v1/notifications"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class Reports(unittest.TestCase):
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
        for t in ("audit_log", "notifications", "inspection_report_files", "inspection_report_recipients", "inspection_report_tasks",
                  "inspection_reports", "inspection_tasks", "inspection_reading", "inspection_tml", "inspection_records", "equipment"):
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        db.close()
        self.eq = self.make_equipment("V-1")
        self.eq2 = self.make_equipment("E-2")
        self.t1 = self.make_task(self.eq, "Inspection Task-1", "Internal")
        self.t2 = self.make_task(self.eq, "Inspection Task-2", "External")
        self.other_task = self.make_task(self.eq2, "Other equipment task", "UTG")

    # ---------------------------------------------------------------- helpers
    def make_equipment(self, tag):
        r = self.client.post("/api/v1/equipment", headers=self.h["eng"], json={
            "equipment_id": tag, "equipment_name": f"{tag} name", "equipment_type": "Pressure Vessel", "design_pressure": 10,
            "design_temperature": 150, "diameter": 1000, "material": "SA-516", "material_grade": "70",
            "min_required_thickness": 8.0, "nominal_thickness": 12})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def make_task(self, equipment_id, name, kind):
        r = self.client.post(T, headers=self.h["eng"], json={"equipment_id": equipment_id, "name": name, "task_type": kind,
                                                            "rbi_interval_years": 7, "last_date": "2024-12-15", "next_date": "2031-12-15"})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def body(self, **kw):
        base = {"equipment_id": self.eq, "report_no": "1212", "inspection_date": "2026-12-15", "title": "Baseline",
                "reason": "First inspection", "summary": "All good", "task_ids": [self.t1], "recipients": ["adm2"]}
        base.update(kw)
        return base

    def make(self, user="eng", **kw):
        r = self.client.post(R, headers=self.h[user], json=self.body(**kw))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def post(self, user, rid, action, body=None):
        return self.client.post(f"{R}/{rid}/{action}", headers=self.h[user], json=body or {})

    def send(self, rid, user="eng", approver="adm1", **kw):
        r = self.post(user, rid, "submit", {"approver": approver, **kw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def get(self, rid, user="adm1"):
        return self.client.get(f"{R}/{rid}", headers=self.h[user]).json()

    def notes(self, user):
        return self.client.get(N, headers=self.h[user]).json()

    def upload(self, rid, user="eng", name="report.pdf", data=PDF):
        return self.client.post(f"{R}/{rid}/files", headers=self.h[user], files={"file": (name, data, "application/pdf")})

    # ---------------------------------------------------------------- create and read
    def test_a_report_keeps_what_the_person_wrote(self):
        r = self.make()
        self.assertEqual((r["report_no"], r["title"], r["reason"], r["summary"], r["inspection_date"]),
                         ("1212", "Baseline", "First inspection", "All good", "2026-12-15"))
        self.assertEqual((r["approval_status"], r["equipment_tag"], r["created_by"]), ("draft", "V-1", "eng"))
        self.assertEqual([t["name"] for t in r["tasks"]], ["Inspection Task-1"])
        self.assertEqual([x["username"] for x in r["recipients"]], ["adm2"])
        self.assertEqual((r["task_count"], r["file_count"]), (1, 0))
        got = self.get(r["id"], "view")
        self.assertEqual((got["report_no"], got["tasks"][0]["next_date"]), ("1212", "2031-12-15"))     # the task's own dates are not touched

    def test_what_is_refused_when_writing_a_report(self):
        post = lambda **kw: self.client.post(R, headers=self.h["eng"], json=self.body(**kw))
        self.assertEqual(post(report_no="  ").status_code, 422)
        self.assertEqual(post(title="Visit").status_code, 422)                                  # only the listed titles
        self.assertEqual(post(equipment_id=str(uuid.uuid4())).status_code, 400)
        wrong = post(task_ids=[self.other_task])                                                # a task of ANOTHER equipment
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("own equipment", wrong.json()["detail"])
        self.assertEqual(post(task_ids=[str(uuid.uuid4())]).status_code, 400)
        self.assertEqual(post(recipients=["nobody"]).status_code, 400)
        self.assertEqual(post(recipients=["view", "adm2"]).status_code, 201)                    # any active person may be told
        self.assertEqual(post(report_no="1212").status_code, 400)                               # the number is not repeated
        dup = post(report_no="1212 ")
        self.assertEqual(dup.status_code, 400)
        self.assertIn("not repeated", dup.json()["detail"])

    def test_a_draft_may_have_no_task_yet_but_cannot_be_sent_without_one(self):
        r = self.make(task_ids=[], recipients=[])
        self.assertEqual(r["task_count"], 0)
        sent = self.post("eng", r["id"], "submit", {"approver": "adm1"})
        self.assertEqual(sent.status_code, 400)
        self.assertIn("at least one inspection task", sent.json()["detail"])

    def test_nobody_is_sent_their_own_report_in_send_to(self):
        r = self.make(recipients=["eng", "adm2", "ADM2"])
        self.assertEqual([x["username"] for x in r["recipients"]], ["adm2"])

    def test_the_list_filters_searches_and_counts(self):
        a = self.make(report_no="1212", title="Baseline", inspection_date="2020-12-15")
        b = self.make(report_no="1215", title="Leak", inspection_date="2024-12-15", summary="found a leak at the nozzle")
        c = self.make(report_no="6853", title="Repair", inspection_date="2026-12-15", equipment_id=self.eq2, task_ids=[self.other_task])
        self.send(b["id"])
        get = lambda q="", user="view": self.client.get(f"{R}{q}", headers=self.h[user]).json()
        nos = lambda r: [x["report_no"] for x in r["data"]]
        everything = get()
        self.assertEqual(nos(everything), ["6853", "1215", "1212"])                              # newest inspection first
        self.assertEqual(everything["counts"], {"draft": 2, "pending": 1, "returned": 0, "approved": 0, "rejected": 0})
        self.assertEqual(nos(get(f"?equipment_id={self.eq2}")), ["6853"])
        self.assertEqual(nos(get("?status=pending")), ["1215"])
        self.assertEqual(get("?status=pending")["counts"]["draft"], 2)                             # the counts ignore the state filter
        self.assertEqual(nos(get("?q=nozzle")), ["1215"])
        self.assertEqual(nos(get("?q=E-2")), ["6853"])
        self.assertEqual(nos(get("?q=leak")), ["1215"])                                            # the title
        self.assertEqual(sorted(nos(get("?mine=true", "eng"))), ["1212", "1215", "6853"])
        self.assertEqual(nos(get("?mine=true", "eng2")), [])
        self.assertEqual(nos(get("?assigned_to_me=true", "adm1")), ["1215"])
        self.assertEqual(nos(get("?assigned_to_me=true", "adm2")), [])
        page2 = get("?page=2&page_size=2")
        self.assertEqual((page2["total"], page2["total_pages"], nos(page2)), (3, 2, ["1212"]))
        self.assertEqual(self.client.get(R).status_code, 401)

    def test_summary_and_the_people_that_can_be_chosen(self):
        a = self.make(report_no="A1")
        b = self.make(report_no="B1")
        self.send(a["id"])
        self.send(b["id"])
        self.post("adm1", b["id"], "return", {"comment": "fix the summary"})
        self.make(report_no="C1")
        s = lambda user: self.client.get(f"{R}/summary", headers=self.h[user]).json()
        self.assertEqual(s("eng"), {"waiting_for_me": 0, "returned_to_me": 1, "my_drafts": 1})
        self.assertEqual(s("adm1"), {"waiting_for_me": 1, "returned_to_me": 0, "my_drafts": 0})
        self.assertEqual(s("view"), {"waiting_for_me": 0, "returned_to_me": 0, "my_drafts": 0})
        names = {x["username"] for x in self.client.get(f"{R}/recipients", headers=self.h["eng"]).json()}
        self.assertEqual(names, set(USERS))

    # ---------------------------------------------------------------- change and delete
    def test_only_the_owner_changes_a_draft_and_every_change_is_audited(self):
        r = self.make()
        put = lambda user, **kw: self.client.put(f"{R}/{r['id']}", headers=self.h[user], json=kw)
        for who in ("eng2", "adm1"):
            denied = put(who, summary="changed by somebody else")
            self.assertEqual(denied.status_code, 403, who)
            self.assertIn("belongs to eng", denied.json()["detail"])
        self.assertEqual(put("sup", summary="x").status_code, 403)
        self.assertEqual(put("view", summary="x").status_code, 403)
        ok = put("eng", title="Leak", summary="found a leak", task_ids=[self.t1, self.t2], recipients=["adm2", "view"])
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["title"], ok.json()["task_count"], sorted(x["username"] for x in ok.json()["recipients"])),
                         ("Leak", 2, ["adm2", "view"]))
        self.assertEqual(put("eng", report_no="").status_code, 400)
        other = self.make(report_no="9999")
        self.assertEqual(self.client.put(f"{R}/{other['id']}", headers=self.h["eng"], json={"report_no": "1212"}).status_code, 400)
        self.assertEqual(put("eng", equipment_id=self.eq2).json()["equipment_tag"], "V-1")           # the equipment cannot be changed
        lines = self.client.get(f"{R}/{r['id']}/history", headers=self.h["view"]).json()
        self.assertEqual([x["action"] for x in lines], ["report.create", "report.edit"])
        self.assertIn("title: Baseline -> Leak", lines[1]["detail"])
        self.assertIn("summary changed", lines[1]["detail"])
        self.assertIn("tasks: Inspection Task-1 -> Inspection Task-1, Inspection Task-2", lines[1]["detail"])
        self.assertIn("send to: adm2 -> adm2, view", lines[1]["detail"])
        self.assertEqual(self.client.put(f"{R}/{uuid.uuid4()}", headers=self.h["eng"], json={"summary": "x"}).status_code, 404)

    def test_an_admin_deletes_a_report_that_is_not_locked(self):
        r = self.make()
        self.upload(r["id"])
        self.assertEqual(self.client.delete(f"{R}/{r['id']}", headers=self.h["eng"]).status_code, 403)        # an admin's job
        self.assertEqual(self.client.delete(f"{R}/{r['id']}", headers=self.h["sup"]).status_code, 403)
        self.assertEqual(self.client.delete(f"{R}/{r['id']}", headers=self.h["adm1"]).status_code, 204)
        self.assertEqual(self.client.get(f"{R}/{r['id']}", headers=self.h["adm1"]).status_code, 404)
        db = Session()
        self.assertEqual(db.execute(text("SELECT count(*) FROM inspection_report_files")).scalar(), 0)       # its files went with it
        self.assertEqual(db.execute(text("SELECT count(*) FROM inspection_tasks")).scalar(), 3)               # the tasks stay
        db.close()
        pending = self.make(report_no="P1")
        self.send(pending["id"])
        locked = self.client.delete(f"{R}/{pending['id']}", headers=self.h["adm1"])
        self.assertEqual(locked.status_code, 409)
        self.assertIn("locked", locked.json()["detail"])

    def test_a_task_that_a_report_covers_cannot_be_deleted(self):
        r = self.make()
        refused = self.client.delete(f"{T}/{self.t1}", headers=self.h["adm1"])
        self.assertEqual(refused.status_code, 400)
        self.assertIn("covered by report 1212", refused.json()["detail"])
        self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"task_ids": [self.t2]})            # take it off the report
        self.assertEqual(self.client.delete(f"{T}/{self.t1}", headers=self.h["adm1"]).status_code, 204)

    # ---------------------------------------------------------------- files
    def test_files_go_up_and_come_back_unchanged_only_for_signed_in_people(self):
        r = self.make()
        up = self.upload(r["id"], name="My Report (final).pdf")
        self.assertEqual(up.status_code, 201, up.text)
        f = up.json()
        self.assertEqual((f["filename"], f["size_bytes"], f["uploaded_by"]), ("My_Report_final.pdf", len(PDF), "eng"))   # a safe name
        self.assertEqual(self.get(r["id"])["file_count"], 1)
        down = self.client.get(f"{R}/{r['id']}/files/{f['id']}", headers=self.h["view"])
        self.assertEqual((down.status_code, down.content), (200, PDF))
        self.assertIn("attachment", down.headers["content-disposition"])
        self.assertEqual(down.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get(f"{R}/{r['id']}/files/{f['id']}").status_code, 401)                # never a public link
        self.assertEqual(self.client.get(f"{R}/{r['id']}/files/{uuid.uuid4()}", headers=self.h["view"]).status_code, 404)

    def test_what_may_not_be_uploaded(self):
        r = self.make()
        self.assertEqual(self.upload(r["id"], name="virus.exe", data=b"MZ\x90\x00").status_code, 400)       # not an allowed type
        liar = self.upload(r["id"], name="fake.pdf", data=b"this is not a pdf")
        self.assertEqual(liar.status_code, 400)                                                             # the content must match
        self.assertEqual(self.upload(r["id"], name="empty.pdf", data=b"").status_code, 400)
        real = report_endpoints.MAX_DOCUMENT_BYTES
        report_endpoints.MAX_DOCUMENT_BYTES = 100
        try:
            self.assertEqual(self.upload(r["id"], name="big.pdf", data=PDF + b"x" * 200).status_code, 413)
        finally:
            report_endpoints.MAX_DOCUMENT_BYTES = real
        self.assertEqual(self.get(r["id"])["file_count"], 0)
        for i in range(20):
            self.assertEqual(self.upload(r["id"], name=f"f{i}.pdf").status_code, 201)
        self.assertEqual(self.upload(r["id"], name="one-too-many.pdf").status_code, 400)                    # at most 20 files

    def test_who_may_add_or_remove_a_file(self):
        r = self.make()
        for who, expected in (("eng2", 403), ("adm1", 403), ("view", 403), ("sup", 403)):
            self.assertEqual(self.upload(r["id"], user=who).status_code, expected, who)
        fid = self.upload(r["id"]).json()["id"]
        self.assertEqual(self.client.delete(f"{R}/{r['id']}/files/{fid}", headers=self.h["eng2"]).status_code, 403)
        self.assertEqual(self.client.delete(f"{R}/{r['id']}/files/{fid}", headers=self.h["eng"]).status_code, 204)   # an inspector, on his own
        self.assertEqual(self.get(r["id"])["file_count"], 0)
        actions = [x["action"] for x in self.client.get(f"{R}/{r['id']}/history", headers=self.h["view"]).json()]
        self.assertEqual(actions, ["report.create", "report.file_add", "report.file_remove"])

    # ---------------------------------------------------------------- approval
    def test_the_full_round_send_return_correct_send_approve(self):
        r = self.make()
        fid = self.upload(r["id"]).json()["id"]
        sent = self.send(r["id"], comment="please check")
        self.assertEqual((sent["approval_status"], sent["assigned_reviewer"], sent["submitted_by"], sent["submit_comment"]),
                         ("pending", "adm1", "eng", "please check"))
        self.assertEqual(self.post("adm1", r["id"], "return", {"comment": "the summary is empty"}).json()["approval_status"], "returned")
        back = self.get(r["id"])
        self.assertEqual((back["review_comment"], back["reviewed_by"]), ("the summary is empty", "adm1"))
        fixed = self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"summary": "now written"})      # a returned report can be changed
        self.assertEqual(fixed.status_code, 200)
        self.send(r["id"], approver="adm2")
        done = self.post("adm2", r["id"], "approve", {"comment": "good"})
        self.assertEqual((done.status_code, done.json()["approval_status"], done.json()["reviewed_by"]), (200, "approved", "adm2"))
        lines = [x["action"] for x in self.client.get(f"{R}/{r['id']}/history", headers=self.h["view"]).json()]
        self.assertEqual(lines, ["report.create", "report.file_add", "report.submit", "report.return", "report.edit", "report.submit", "report.approve"])
        self.assertEqual(self.client.get(f"{R}/{r['id']}/files/{fid}", headers=self.h["view"]).status_code, 200)

    def test_pending_and_approved_are_locked(self):
        r = self.make()
        fid = self.upload(r["id"]).json()["id"]
        self.send(r["id"])
        for stage in ("pending", "approved"):
            if stage == "approved":
                self.assertEqual(self.post("adm1", r["id"], "approve").status_code, 200)
            put = self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"summary": "changed"})
            self.assertEqual(put.status_code, 409, stage)
            self.assertIn("locked", put.json()["detail"])
            self.assertEqual(self.upload(r["id"]).status_code, 409, stage)
            self.assertEqual(self.client.delete(f"{R}/{r['id']}/files/{fid}", headers=self.h["eng"]).status_code, 409, stage)
            self.assertEqual(self.client.delete(f"{R}/{r['id']}", headers=self.h["adm1"]).status_code, 409, stage)
        self.assertEqual(self.get(r["id"])["summary"], "All good")

    def test_who_may_send_and_who_may_decide(self):
        r = self.make()
        for who in ("eng2", "adm2", "view", "sup"):                                              # not the owner / not allowed to write
            self.assertIn(self.post(who, r["id"], "submit", {"approver": "adm1"}).status_code, (403,), who)
        for approver, why in (("eng2", "an inspector"), ("view", "a viewer"), ("sup", "the super admin"), ("nobody", "unknown"), ("eng", "yourself")):
            self.assertEqual(self.post("eng", r["id"], "submit", {"approver": approver}).status_code, 400, why)
        self.send(r["id"])
        self.assertEqual(self.post("adm2", r["id"], "approve").status_code, 403)                  # another admin: not the one it was sent to
        self.assertEqual(self.post("adm2", r["id"], "return", {"comment": "no thanks"}).status_code, 403)
        for who in ("eng", "eng2", "view", "sup"):
            self.assertEqual(self.post(who, r["id"], "approve").status_code, 403, who)
        self.assertEqual(self.post("adm1", r["id"], "return", {"comment": "x"}).status_code, 422)  # a reason of at least 3 letters
        self.assertEqual(self.get(r["id"])["approval_status"], "pending")
        self.assertEqual(self.post("adm1", r["id"], "approve").status_code, 200)

    def test_an_admin_cannot_decide_on_their_own_report(self):
        r = self.make(user="adm1")
        self.assertEqual(self.post("adm1", r["id"], "submit", {"approver": "adm1"}).status_code, 400)
        self.send(r["id"], user="adm1", approver="adm2")
        self.assertEqual(self.post("adm1", r["id"], "approve").status_code, 403)
        self.assertEqual(self.post("adm2", r["id"], "approve").status_code, 200)

    def test_withdraw_and_reopen(self):
        r = self.make()
        self.send(r["id"])
        for who in ("eng2", "adm1"):
            self.assertEqual(self.post(who, r["id"], "withdraw").status_code, 403, who)             # only the sender (or the super admin)
        back = self.post("eng", r["id"], "withdraw")
        self.assertEqual((back.status_code, back.json()["approval_status"], back.json()["assigned_reviewer"]), (200, "draft", None))
        self.assertEqual(self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"summary": "again"}).status_code, 200)
        self.send(r["id"])
        self.assertEqual(self.post("sup", r["id"], "withdraw").status_code, 200)                    # the super admin watches over
        self.send(r["id"])
        self.post("adm1", r["id"], "approve")
        self.assertEqual(self.post("adm1", r["id"], "withdraw").status_code, 400)                   # an approved report is not withdrawn
        for who in ("eng", "adm2", "view"):
            self.assertEqual(self.post(who, r["id"], "reopen", {"reason": "wrong date"}).status_code, 403, who)
        self.assertEqual(self.post("adm1", r["id"], "reopen", {"reason": "x"}).status_code, 422)
        reopened = self.post("adm1", r["id"], "reopen", {"reason": "wrong date of inspection"})
        self.assertEqual((reopened.status_code, reopened.json()["approval_status"]), (200, "returned"))
        self.assertEqual(self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"inspection_date": "2026-12-16"}).status_code, 200)
        self.send(r["id"])
        self.post("adm1", r["id"], "approve")
        self.assertEqual(self.post("sup", r["id"], "reopen", {"reason": "audit finding"}).status_code, 200)   # the super admin too

    def test_reject_is_a_final_no_and_needs_a_reason(self):
        r = self.make()
        self.assertEqual(self.post("adm1", r["id"], "reject", {"reason": "not our equipment"}).status_code, 400)    # not sent yet
        self.send(r["id"])
        for who in ("eng", "eng2", "view", "sup"):
            self.assertEqual(self.post(who, r["id"], "reject", {"reason": "no thanks"}).status_code, 403, who)
        self.assertEqual(self.post("adm2", r["id"], "reject", {"reason": "no thanks"}).status_code, 403)         # not the admin it was sent to
        self.assertEqual(self.post("adm1", r["id"], "reject", {"reason": "x"}).status_code, 422)                 # a reason of a few words
        self.assertEqual(self.post("adm1", r["id"], "reject", {}).status_code, 422)
        self.assertEqual(self.get(r["id"])["approval_status"], "pending")
        done = self.post("adm1", r["id"], "reject", {"reason": "This is a report of another vessel"})
        self.assertEqual((done.status_code, done.json()["approval_status"], done.json()["reviewed_by"], done.json()["review_comment"]),
                         (200, "rejected", "adm1", "This is a report of another vessel"))
        # closed: nobody changes it, sends it again, withdraws, approves or reopens it
        self.assertEqual(self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"summary": "again"}).status_code, 409)
        self.assertIn("rejected and is closed", self.client.put(f"{R}/{r['id']}", headers=self.h["eng"], json={"summary": "again"}).json()["detail"])
        self.assertEqual(self.upload(r["id"]).status_code, 409)
        self.assertEqual(self.post("eng", r["id"], "submit", {"approver": "adm1"}).status_code, 400)
        self.assertEqual(self.post("eng", r["id"], "withdraw").status_code, 400)
        self.assertEqual(self.post("adm1", r["id"], "approve").status_code, 400)
        self.assertEqual(self.post("adm1", r["id"], "reopen", {"reason": "changed my mind"}).status_code, 400)
        # it is counted, listed, told to the sender and written to the audit trail
        lst = self.client.get(f"{R}?status=rejected", headers=self.h["view"]).json()
        self.assertEqual([x["report_no"] for x in lst["data"]], ["1212"])
        self.assertEqual(lst["counts"], {"draft": 0, "pending": 0, "returned": 0, "approved": 0, "rejected": 1})
        n = self.notes("eng")["items"][0]
        self.assertEqual((n["kind"], n["detail"]), ("report_rejected", "This is a report of another vessel"))
        self.assertIn("adm1 rejected report 1212", n["message"])
        self.assertEqual([x["action"] for x in self.client.get(f"{R}/{r['id']}/history", headers=self.h["view"]).json()][-1], "report.reject")
        # an admin may clear it away; the number can then be used again
        self.assertEqual(self.client.delete(f"{R}/{r['id']}", headers=self.h["adm1"]).status_code, 204)
        self.make()

    def test_returning_a_report_is_the_meridium_re_refer_and_can_be_sent_again(self):
        r = self.make()
        self.send(r["id"])
        self.post("adm1", r["id"], "return", {"comment": "add the photos"})
        self.assertIn("adm1 re-referred report 1212", self.notes("eng")["items"][0]["message"])
        self.send(r["id"])
        self.assertEqual(self.post("adm1", r["id"], "approve").status_code, 200)

    def test_approving_a_report_does_not_touch_the_dates_of_its_tasks(self):
        r = self.make(task_ids=[self.t1, self.t2])
        self.send(r["id"])
        self.post("adm1", r["id"], "approve")
        for tid in (self.t1, self.t2):
            t = self.client.get(f"{T}/{tid}", headers=self.h["view"]).json()
            self.assertEqual((t["last_date"], t["next_date"]), ("2024-12-15", "2031-12-15"))

    # ---------------------------------------------------------------- notifications
    def test_everybody_the_report_concerns_is_told(self):
        r = self.make(recipients=["adm2", "view"])
        self.send(r["id"], comment="for the 2026 shutdown")
        a = self.notes("adm1")["items"][0]
        self.assertEqual((a["kind"], a["detail"], a["record_id"], a["equipment_id"]), ("report_assigned", "for the 2026 shutdown", r["id"], self.eq))
        self.assertIn("eng sent you report 1212 (V-1) for approval", a["message"])
        for who in ("adm2", "view"):                                                        # "Send to": told, but they do not decide
            self.assertEqual(self.notes(who)["items"][0]["kind"], "report_shared", who)
        self.assertEqual(self.notes("eng")["unread"], 0)                                      # nobody is told what they did themselves
        self.assertEqual(self.notes("eng2")["unread"], 0)

        self.post("adm1", r["id"], "return", {"comment": "add the photos"})
        e = self.notes("eng")["items"][0]
        self.assertEqual((e["kind"], e["detail"]), ("report_returned", "add the photos"))
        self.send(r["id"])
        self.post("adm1", r["id"], "approve", {"comment": "ok"})
        self.assertEqual(self.notes("eng")["items"][0]["kind"], "report_approved")
        self.assertEqual(self.notes("view")["items"][0]["kind"], "report_approved")             # the people it was sent to see the outcome
        self.post("adm1", r["id"], "reopen", {"reason": "wrong zone"})
        self.assertEqual(self.notes("eng")["items"][0]["kind"], "report_reopened")
        self.send(r["id"])
        self.post("eng", r["id"], "withdraw")
        self.assertEqual(self.notes("adm1")["items"][0]["kind"], "report_withdrawn")

    # ---------------------------------------------------------------- roles and the rest
    def test_roles(self):
        for who, expected in (("eng", 201), ("adm1", 201), ("view", 403), ("sup", 403)):
            self.assertEqual(self.client.post(R, headers=self.h[who], json=self.body(report_no=f"no-{who}")).status_code, expected, who)
        for who in USERS:
            self.assertEqual(self.client.get(R, headers=self.h[who]).status_code, 200, who)           # everybody reads

    def test_without_sign_in_it_works_as_before_but_approval_needs_it(self):
        settings.AUTH_REQUIRED = False
        try:
            r = self.client.post(R, json=self.body(report_no="free"))
            self.assertEqual((r.status_code, r.json()["created_by"]), (201, "System"))
            self.assertEqual(self.client.put(f"{R}/{r.json()['id']}", json={"summary": "edited"}).status_code, 200)
            sent = self.client.post(f"{R}/{r.json()['id']}/submit", json={"approver": "adm1"})
            self.assertEqual(sent.status_code, 400)
            self.assertIn("sign-in", sent.json()["detail"])
            self.assertEqual(self.client.get(f"{R}/summary").json(), {"waiting_for_me": 0, "returned_to_me": 0, "my_drafts": 0})
        finally:
            settings.AUTH_REQUIRED = True

    def test_reports_of_removed_equipment_stay_readable_by_id_but_none_can_be_added_to_it(self):
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{self.eq2}", headers=self.h["adm1"]).status_code, 200)
        r = self.client.post(R, headers=self.h["eng"], json=self.body(report_no="late", equipment_id=self.eq2, task_ids=[]))
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
