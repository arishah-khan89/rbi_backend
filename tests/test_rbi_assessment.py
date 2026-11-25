"""Risk assessment (RBI) after the client's workbook: reference lists, components, the life of an assessment (draft -> finalized ->
approved), what the software fills in, the rights, the audit trail. On a TEMPORARY database, with sign-in on.
Run from backend/:  python -m unittest tests.test_rbi_assessment -v
"""
import os
import secrets
import sys
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

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
import app.models.rbi_assessment  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cml import InspectionReading, InspectionTML  # noqa: E402
from app.models.equipment import Equipment  # noqa: E402
from app.models.rbi_assessment import RbiAssessment, RbiFluidEntry, RbiMechanismEntry  # noqa: E402
from app.models.user import User  # noqa: E402
from app.rbi_engine.engine import load_rules  # noqa: E402
from app.seed.import_rbi_lists import import_lists  # noqa: E402
from tests.rbi_sheet_helpers import HAVE_SHEET, SHEET_FILE  # noqa: E402

BASE_URL = make_url(settings.DATABASE_URL)
TEST_DB = "ams_test_" + uuid.uuid4().hex[:8]
engine = create_engine(BASE_URL.set(database=TEST_DB), pool_pre_ping=True)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
USERS = {"view": "viewer", "eng": "engineer", "eng2": "engineer", "adm": "admin", "adm2": "admin", "sup": "super_admin"}
PW = {u: secrets.token_urlsafe(14) for u in USERS}
_auth_before = settings.AUTH_REQUIRED
API = "/api/v1/risk-assessment"
IMPORT_RESULT = {}


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
    if HAVE_SHEET:
        IMPORT_RESULT.update(import_lists(db, SHEET_FILE, apply=True))
    else:
        db.add_all([RbiFluidEntry(toxic_name="Furfural", flammable_name="Furfural", toxicity_concentration=Decimal(100), toxicity_unit="PPM", flash_point="60"),
                    RbiFluidEntry(flammable_name="EE", flammability=3, flash_point="44"),
                    RbiMechanismEntry(code="DM01", name="Internal Corrosion", location="Internal", damage_class="Thinning", rate_based="Yes",
                                      primary_nde="UT", secondary_nde="RT", sort_order=1),
                    RbiMechanismEntry(code="DM02", name="External corrosion", location="External", damage_class="Thinning", rate_based="Yes",
                                      primary_nde="UT", secondary_nde="RT", sort_order=2)])
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


def make_vessel(tag, with_readings=True):
    """A vessel with a shell location (thinnest reading 10 mm, minimum required 8 mm) and a head."""
    db = Session()
    eq = Equipment(equipment_id=tag, equipment_name="Test vessel " + tag, equipment_type="Pressure Vessel", plant="P1", unit="U1",
                   min_required_thickness=Decimal("8.0"), dish_min_thickness=Decimal("7.0"), nominal_thickness=Decimal("12"),
                   commissioning_date=datetime(2016, 1, 1, tzinfo=timezone.utc), expected_service_life=25, has_insulation="Yes",
                   fluid="ammonia", operating_temperature=Decimal("50"), operating_pressure=Decimal("12.5"))
    db.add(eq)
    db.flush()
    if with_readings:
        for code, name, kind, readings, cr in (("S-1", "Shell course 1", "Shell", (10.0, 11.0, 12.0, 10.5), Decimal("0.30")),
                                               ("D-1", "Upper dish", "Dish", (9.0, 9.5, 10.0, 9.8), Decimal("0.10"))):
            tml = InspectionTML(equipment_id=eq.id, equipment_tag=tag, tml_code=code, tml_name=name, tml_type=kind)
            db.add(tml)
            db.flush()
            db.add(InspectionReading(tml_id=tml.id, inspection_date=datetime(2020, 1, 1, tzinfo=timezone.utc), reading_1=Decimal("12")))
            db.add(InspectionReading(tml_id=tml.id, inspection_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                                     reading_1=Decimal(str(readings[0])), reading_2=Decimal(str(readings[1])),
                                     reading_3=Decimal(str(readings[2])), reading_4=Decimal(str(readings[3])),
                                     controlling_cr=cr, short_term_cr_csv="0.20,0.25", long_term_cr_csv="0.10"))
    db.commit()
    eid = str(eq.id)
    db.close()
    return eid


NUMBERS = {"design_life_years": 20, "t_nominal": 12, "external_corrosion_rate": 0.05, "internal_corrosion_rate": 0.1, "downtime_days": 10,
           "inventory_kg": 1600, "cost_per_kg": 1, "repair_cost": 300000, "toxic_percentage": 0, "detection_time": 30,
           "isolation_time": 60, "ignition_distance": 15, "operating_temperature": 95, "operating_pressure": 10, "workforce_persons": 1,
           "cui_temperature": 100, "chloride_ppm": 25, "area_humidity": 80, "cooling_tower_distance": 30, "external_inspections": 3,
           "internal_inspections": 2}
DATES = {"in_service_date": "2020-09-03", "last_internal_inspection": "2021-01-01", "last_external_inspection": "2025-01-01"}
MECHANISMS = [{"name": "Internal Corrosion", "inspection_effectiveness": "B – Usually effective"},
              {"name": "External corrosion", "inspection_effectiveness": "C – Fairly effective"}]


class Base_(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        auth_endpoints._failures.clear()
        cls.tokens = {}
        cls.form = None

    def h(self, user):
        if user not in self.tokens:
            r = self.client.post("/api/v1/auth/login", json={"username": user, "password": PW[user]})
            self.tokens[user] = {"Authorization": "Bearer " + r.json()["access_token"]}
        return self.tokens[user]

    def full_inputs(self):
        """Every input of the form answered (the choices are the first option of each dropdown)."""
        form = self.client.get(f"{API}/form", headers=self.h("view")).json()
        inputs = {**NUMBERS, **DATES}
        for i in form["inputs"]:
            if i["kind"] in ("select", "yesno") and "options" in i:
                inputs[i["key"]] = i["options"][0]
        inputs.update({"internal_corrosion_rate_kind": "Expected", "toxic_fluid": "Furfural", "representative_fluid": "EE", "mechanisms": MECHANISMS})
        return inputs

    def component(self, tag, user="eng", name=None, kind="Shell"):
        eid = make_vessel(tag)
        r = self.client.post(f"{API}/equipment/{eid}/components", json={"name": name or f"{tag}-SH", "component_type": kind}, headers=self.h(user))
        self.assertEqual(r.status_code, 201, r.text)
        return eid, r.json()["component"]["id"]

    def draft(self, tag, user="eng"):
        eid, cid = self.component(tag, user)
        r = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h(user))
        self.assertEqual(r.status_code, 201, r.text)
        return eid, cid, r.json()

    def completed_draft(self, tag, user="eng"):
        eid, cid, d = self.draft(tag, user)
        r = self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": self.full_inputs()}, headers=self.h(user))
        self.assertEqual(r.status_code, 200, r.text)
        return eid, cid, r.json()


@unittest.skipUnless(HAVE_SHEET, "the client's workbook is not next to the project")
class ReferenceLists(Base_):
    def test_the_workbooks_lists_were_loaded(self):
        self.assertEqual(IMPORT_RESULT["fluids"]["added"], 112)          # 116 rows, 4 of them have no fluid name
        self.assertEqual(IMPORT_RESULT["mechanisms"]["added"], 96)
        self.assertEqual(IMPORT_RESULT["without_location"], ["Sensitization and Intergranular Corrosion"])
        self.assertEqual(sorted(IMPORT_RESULT["damages_list_not_in_library"]), ["General Corrosion", "Microbiologically Induced Corrosion (MIC)"])
        self.assertEqual(IMPORT_RESULT["duplicate_codes"], ["DM35: Internal Corrosion, Graphitization"])
        self.assertEqual(sorted(IMPORT_RESULT["flammability_outside_0_to_4"]), ["C2", "C25+ Resid", "C5", "C8 Gasoline", "C9", "Methanol", "water"])

    def test_loading_again_changes_nothing(self):
        db = Session()
        again = import_lists(db, SHEET_FILE, apply=True)
        db.close()
        self.assertEqual(again["fluids"], {"added": 0, "changed": 0, "unchanged": 112})
        self.assertEqual(again["mechanisms"], {"added": 0, "changed": 0, "unchanged": 96})

    def test_fluids_are_found_by_name_for_the_right_use(self):
        toxic = self.client.get(f"{API}/fluids", params={"kind": "toxic", "q": "furf"}, headers=self.h("view")).json()
        self.assertEqual([f["name"] for f in toxic], ["Furfural"])
        self.assertEqual(toxic[0]["toxicity_concentration"], 100)
        flam = self.client.get(f"{API}/fluids", params={"kind": "flammable", "q": "EE"}, headers=self.h("view")).json()
        self.assertIn("EE", [f["name"] for f in flam])

    def test_mechanisms_can_be_listed_by_location(self):
        inside = self.client.get(f"{API}/mechanisms", params={"location": "Internal"}, headers=self.h("view")).json()
        outside = self.client.get(f"{API}/mechanisms", params={"location": "External"}, headers=self.h("view")).json()
        self.assertEqual((len(inside), len(outside)), (88, 7))
        self.assertTrue(all(m["location"] == "External" for m in outside))
        self.assertTrue(any(m["definition"] for m in inside))               # the damages-list text is there for most

    def test_the_form_description_comes_from_the_rules(self):
        form = self.client.get(f"{API}/form", headers=self.h("view")).json()
        self.assertEqual(form["version"], load_rules()["version"])
        self.assertEqual(len(form["factors"]), 39)
        self.assertEqual(form["max_mechanisms"], 8)
        keys = [i["key"] for i in form["inputs"]]
        self.assertIn("internal_corrosion_rate_kind", keys)
        self.assertLess(keys.index("internal_corrosion_rate_kind"), keys.index("internal_corrosion_rate"))
        self.assertTrue(form["known_issues"])
        self.assertEqual({l["label"] for l in form["levels"]}, {"Low", "Medium", "Medium High", "High"})


class Components(Base_):
    def test_an_inspector_creates_a_component_and_it_is_in_the_asset_summary(self):
        eid, cid = self.component("V-C-1")
        body = self.client.get(f"{API}/equipment/{eid}", headers=self.h("view")).json()
        self.assertEqual([c["name"] for c in body["components"]], ["V-C-1-SH"])
        self.assertEqual(body["components"][0]["component_type"], "Shell")
        self.assertEqual(body["suggested_components"][0]["component_type"], "Head / Dish")        # the dish has thickness locations too
        self.assertIsNone(body["highest"])

    def test_a_name_is_used_once_per_equipment_and_the_type_must_be_known(self):
        eid, cid = self.component("V-C-2")
        dup = self.client.post(f"{API}/equipment/{eid}/components", json={"name": "v-c-2-sh", "component_type": "Shell"}, headers=self.h("eng"))
        self.assertEqual(dup.status_code, 400)
        self.assertIn("already has a component", dup.json()["detail"])
        bad = self.client.post(f"{API}/equipment/{eid}/components", json={"name": "X", "component_type": "Wing"}, headers=self.h("eng"))
        self.assertEqual(bad.status_code, 400)
        none = self.client.post(f"{API}/equipment/{eid}/components", json={"component_type": "Shell"}, headers=self.h("eng"))
        self.assertEqual(none.status_code, 400)

    def test_only_an_inspector_or_admin_writes_a_viewer_and_super_admin_only_look(self):
        eid = make_vessel("V-C-3")
        for user in ("view", "sup"):
            r = self.client.post(f"{API}/equipment/{eid}/components", json={"name": "A", "component_type": "Shell"}, headers=self.h(user))
            self.assertEqual(r.status_code, 403, user)
        self.assertEqual(self.client.get(f"{API}/equipment/{eid}", headers=self.h("sup")).status_code, 200)

    def test_a_component_is_edited_and_set_inactive(self):
        eid, cid = self.component("V-C-4")
        r = self.client.put(f"{API}/components/{cid}", json={"name": "Shell A", "status": "Inactive", "in_service_date": "2019-05-01"}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 200, r.text)
        c = r.json()["component"]
        self.assertEqual((c["name"], c["status"], c["in_service_date"]), ("Shell A", "Inactive", "2019-05-01"))
        self.assertEqual(self.client.put(f"{API}/components/{cid}", json={"status": "Gone"}, headers=self.h("eng")).status_code, 400)

    def test_deleting_a_component_is_for_admins_and_not_after_a_decision(self):
        eid, cid = self.component("V-C-5")
        self.assertEqual(self.client.delete(f"{API}/components/{cid}", headers=self.h("eng")).status_code, 403)
        self.assertEqual(self.client.delete(f"{API}/components/{cid}", headers=self.h("adm")).status_code, 204)
        self.assertEqual(self.client.get(f"{API}/components/{cid}", headers=self.h("view")).status_code, 404)
        eid2, cid2, d = self.completed_draft("V-C-6")
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng")).status_code, 200)
        r = self.client.delete(f"{API}/components/{cid2}", headers=self.h("adm"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Inactive", r.json()["detail"])

    def test_unknown_things_are_not_found(self):
        for path in (f"{API}/equipment/{uuid.uuid4()}", f"{API}/components/{uuid.uuid4()}", f"{API}/assessments/{uuid.uuid4()}"):
            self.assertEqual(self.client.get(path, headers=self.h("view")).status_code, 404, path)

    def test_a_deleted_equipment_is_gone_from_the_risk_assessment(self):
        eid = make_vessel("V-C-7")
        db = Session()
        db.query(Equipment).filter(Equipment.id == eid).update({"status": "DELETED"})
        db.commit()
        db.close()
        self.assertEqual(self.client.get(f"{API}/equipment/{eid}", headers=self.h("view")).status_code, 404)
        found = self.client.get(f"{API}/equipment", params={"q": "V-C-7"}, headers=self.h("view")).json()
        self.assertEqual(found["total"], 0)

    def test_the_equipment_list_finds_by_tag(self):
        make_vessel("V-LIST-1")
        body = self.client.get(f"{API}/equipment", params={"q": "V-LIST"}, headers=self.h("view")).json()
        self.assertEqual([e["tag"] for e in body["items"]], ["V-LIST-1"])
        self.assertEqual((body["items"][0]["components"], body["items"][0]["assessed"]), (0, 0))
        self.assertIn("Pressure Vessel", body["equipment_types"])


    def test_the_equipment_list_can_be_sorted_by_urgency_and_filtered_by_risk_level(self):
        from app.crud.rbi_assessment import _urgency
        h = lambda rank, pr, cof, d: {"risk_rank": rank, "priority": pr, "cof_category": cof, "assessment_date": d}       # noqa: E731
        items = [
            {"tag": "E", "highest": None},
            {"tag": "D", "highest": h(2, 10, 3, "2026-01-01")},
            {"tag": "C", "highest": h(4, 6, 4, "2026-05-01")},
            {"tag": "B", "highest": h(4, 3, 5, "2026-05-01")},
            {"tag": "A", "highest": h(4, 3, 5, "2026-02-01")},          # same level, priority and consequence as B: the older assessment first
            {"tag": "F", "highest": h(4, 3, 4, "2026-01-01")},          # same priority as A and B, lower consequence
        ]
        self.assertEqual([i["tag"] for i in sorted(items, key=_urgency)], ["A", "B", "F", "C", "D", "E"])
        make_vessel("V-URG-1")
        body = self.client.get(f"{API}/equipment", params={"q": "V-URG", "sort": "priority"}, headers=self.h("view")).json()
        self.assertEqual([e["tag"] for e in body["items"]], ["V-URG-1"])
        none = self.client.get(f"{API}/equipment", params={"q": "V-URG", "risk_level": "none"}, headers=self.h("view")).json()
        self.assertEqual(none["total"], 1)
        high = self.client.get(f"{API}/equipment", params={"q": "V-URG", "risk_level": "High"}, headers=self.h("view")).json()
        self.assertEqual((high["total"], high["items"]), (0, []))
        self.assertEqual(self.client.get(f"{API}/equipment", params={"sort": "bogus"}, headers=self.h("view")).status_code, 422)


class RiskToInspectionTasks(Base_):
    """The inspection plan of an approved risk assessment becomes inspection tasks, as the person chose for each side while it was a draft."""
    TASKS = "/api/v1/inspection-tasks"
    BOTH = {"internal": {"action": "new"}, "external": {"action": "new"}}

    def approved(self, tag, plan, user="eng"):
        eid, cid, d = self.completed_draft(tag, user)
        if plan is not None:
            put = self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": d["inputs"], "task_plan": plan}, headers=self.h(user))
            self.assertEqual(put.status_code, 200, put.text)
        fin = self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h(user))
        self.assertEqual(fin.status_code, 200, fin.text)
        ok = self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm"))
        self.assertEqual(ok.status_code, 200, ok.text)
        return eid, cid, ok.json()

    def tasks(self, eid):
        r = self.client.get(self.TASKS, params={"equipment_id": eid}, headers=self.h("view"))
        self.assertEqual(r.status_code, 200, r.text)
        return {t["task_type"]: t for t in r.json()["data"]}

    def manual_task(self, eid, task_type, name):
        r = self.client.post(self.TASKS, json={"equipment_id": eid, "name": name, "task_type": task_type, "duration_days": 30,
                                               "rbi_interval_years": 15, "last_date": "2024-12-15", "next_date": "2030-12-15"}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_a_new_task_for_each_side_is_made_when_approved(self):
        eid, cid, a = self.approved("V-RT-1", self.BOTH)
        planning = a["result"]["planning"]
        tasks = self.tasks(eid)
        self.assertEqual(sorted(tasks), ["External", "Internal"])
        for kind, key in (("Internal", "internal"), ("External", "external")):
            t = tasks[kind]
            self.assertEqual(t["source"], "rbi")
            self.assertEqual(float(t["rbi_interval_years"]), planning[f"{key}_interval_years"])
            self.assertEqual((t["last_date"], t["next_date"]), (planning[key]["last"], planning[key]["due"]))
            self.assertEqual(t["rbi_assessment_id"], a["id"])
            self.assertIn("V-RT-1-SH", t["name"])
            self.assertIn(a["result"]["risk"]["level"], t["details"])
            self.assertEqual((a["task_plan"][key]["action"], a["task_plan"][key]["done"], a["task_plan"][key]["task_id"]), ("new", "created", t["id"]))

    def test_nothing_is_added_unless_the_person_chose_it(self):
        eid, cid, a = self.approved("V-RT-2", None)                               # nothing chosen
        self.assertEqual((a["task_plan"]["internal"]["action"], a["task_plan"]["external"]["action"]), ("none", "none"))
        self.assertEqual(self.tasks(eid), {})
        eid, cid, a = self.approved("V-RT-2B", {"internal": {"action": "new"}, "external": {"action": "none"}})
        self.assertEqual(sorted(self.tasks(eid)), ["Internal"])                   # one side only

    def test_nothing_is_added_while_it_waits_or_when_it_is_returned(self):
        eid, cid, d = self.completed_draft("V-RT-3")
        self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": d["inputs"], "task_plan": self.BOTH}, headers=self.h("eng"))
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(self.tasks(eid), {})                                              # waiting for approval: nothing yet
        self.client.post(f"{API}/assessments/{d['id']}/return", json={"reason": "check"}, headers=self.h("adm"))
        self.assertEqual(self.tasks(eid), {})                                              # returned: nothing

    def test_a_task_the_equipment_already_has_can_be_linked_instead_of_making_a_second_one(self):
        eid, cid, d = self.completed_draft("V-RT-8")
        mine = self.manual_task(eid, "Internal", "task1")
        opts = self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("eng")).json()["task_options"]
        self.assertEqual([o["name"] for o in opts["internal"]], ["task1"])
        self.assertEqual(opts["external"], [])
        plan = {"internal": {"action": "link", "task_id": mine["id"]}, "external": {"action": "new"}}
        self.assertEqual(self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": d["inputs"], "task_plan": plan}, headers=self.h("eng")).status_code, 200)
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        a = self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm")).json()
        tasks = self.tasks(eid)
        self.assertEqual(len(tasks), 2)                                                    # task1 (linked) and the new external one, not three
        t = tasks["Internal"]
        self.assertEqual((t["id"], t["name"], t["duration_days"], t["source"]), (mine["id"], "task1", 30, "rbi"))
        self.assertEqual(float(t["rbi_interval_years"]), a["result"]["planning"]["internal_interval_years"])    # now from the assessment, not the typed 15
        self.assertEqual(a["task_plan"]["internal"]["done"], "linked")
        self.assertEqual(a["task_plan"]["internal"]["task_name"], "task1")

    def test_a_link_to_a_wrong_task_is_refused(self):
        eid, cid, d = self.completed_draft("V-RT-9")
        other_eid = make_vessel("V-RT-9B")
        theirs = self.manual_task(other_eid, "Internal", "theirs")
        wrong_type = self.manual_task(eid, "External", "ext")
        for entry in ({"action": "link", "task_id": theirs["id"]}, {"action": "link", "task_id": wrong_type["id"]}, {"action": "link"}, {"action": "maybe"}):
            r = self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": d["inputs"], "task_plan": {"internal": entry}}, headers=self.h("eng"))
            self.assertEqual(r.status_code, 400, (entry, r.text))

    def test_a_newer_revision_updates_the_same_tasks_and_keeps_what_the_person_wrote(self):
        eid, cid, a1 = self.approved("V-RT-4", self.BOTH)
        t = self.tasks(eid)["Internal"]
        put = self.client.put(f"{self.TASKS}/{t['id']}", json={"duration_days": 30, "name": "Internal inspection of the shell"}, headers=self.h("eng"))
        self.assertEqual(put.status_code, 200, put.text)
        d2 = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng")).json()
        self.assertEqual(d2["task_plan"]["internal"]["action"], "new")                    # the same task is updated, unless the person changes it
        inputs = {**d2["inputs"], "internal_corrosion_rate": 0.2, "last_internal_inspection": "2025-06-01"}
        self.assertEqual(self.client.put(f"{API}/assessments/{d2['id']}", json={"inputs": inputs}, headers=self.h("eng")).status_code, 200)
        self.assertEqual(self.client.post(f"{API}/assessments/{d2['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng")).status_code, 200)
        a2 = self.client.post(f"{API}/assessments/{d2['id']}/approve", headers=self.h("adm")).json()
        tasks = self.tasks(eid)
        self.assertEqual(sorted(tasks), ["External", "Internal"])                         # still two, not four
        self.assertEqual(tasks["Internal"]["id"], t["id"])
        self.assertEqual((tasks["Internal"]["duration_days"], tasks["Internal"]["name"]), (30, "Internal inspection of the shell"))
        self.assertEqual(tasks["Internal"]["rbi_assessment_id"], a2["id"])
        self.assertEqual(tasks["Internal"]["next_date"], a2["result"]["planning"]["internal"]["due"])
        self.assertEqual(a2["task_plan"]["internal"]["done"], "updated")

    def test_a_newer_revision_keeps_the_persons_planned_next_date_while_it_fits_the_rbi_date(self):
        from datetime import date, timedelta
        eid, cid, a1 = self.approved("V-RT-10", self.BOTH)
        t = self.tasks(eid)["Internal"]
        planned = (date.fromisoformat(t["next_date"]) - timedelta(days=40)).isoformat()                # earlier than the RBI date, after the last date
        self.assertEqual(self.client.put(f"{self.TASKS}/{t['id']}", json={"next_date": planned}, headers=self.h("eng")).status_code, 200)
        d2 = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng")).json()   # same answers: the RBI date does not change
        self.client.post(f"{API}/assessments/{d2['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        a2 = self.client.post(f"{API}/assessments/{d2['id']}/approve", headers=self.h("adm")).json()
        after = self.tasks(eid)["Internal"]
        self.assertEqual(after["next_date"], planned)                                                  # not overwritten by the RBI date
        self.assertEqual(after["rbi_assessment_id"], a2["id"])
        self.assertEqual(float(after["rbi_interval_years"]), a2["result"]["planning"]["internal_interval_years"])

    def test_the_interval_of_an_rbi_task_cannot_be_typed_but_the_rest_can(self):
        eid, cid, a = self.approved("V-RT-5", self.BOTH)
        t = self.tasks(eid)["External"]
        bad = self.client.put(f"{self.TASKS}/{t['id']}", json={"rbi_interval_years": 3}, headers=self.h("eng"))
        self.assertEqual(bad.status_code, 400)
        self.assertIn("comes from the approved risk assessment", bad.json()["detail"])
        ok = self.client.put(f"{self.TASKS}/{t['id']}", json={"duration_days": 5, "last_date": "2026-01-10"}, headers=self.h("eng"))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["duration_days"], 5)

    def test_the_new_task_form_can_ask_what_the_approved_assessments_say(self):
        eid, cid, a = self.approved("V-RT-6", None)
        planning = a["result"]["planning"]
        got = self.client.get(f"{self.TASKS}/rbi-suggestion", params={"equipment_id": eid, "task_type": "Internal"}, headers=self.h("eng")).json()
        self.assertEqual(got["interval_years"], planning["internal_interval_years"])
        self.assertEqual(got["next_date"], planning["internal"]["due"])
        self.assertEqual(got["component"], "V-RT-6-SH")
        self.assertIsNone(self.client.get(f"{self.TASKS}/rbi-suggestion", params={"equipment_id": eid, "task_type": "NDE"}, headers=self.h("eng")).json()["interval_years"])
        other = make_vessel("V-RT-7")
        self.assertIsNone(self.client.get(f"{self.TASKS}/rbi-suggestion", params={"equipment_id": other, "task_type": "Internal"}, headers=self.h("eng")).json()["interval_years"])


class WhatTheSoftwareKnows(Base_):
    def test_a_new_assessment_starts_with_what_the_equipment_register_has(self):
        eid, cid, d = self.draft("V-K-1")
        i = d["inputs"]
        self.assertEqual(d["state"], "draft")
        self.assertEqual((d["revision"], i["in_service_date"], i["design_life_years"], i["t_nominal"]), (1, "2016-01-01", 25, 12.0))
        self.assertEqual((i["operating_temperature"], i["operating_pressure"], i["insulated"]), (50.0, 12.5, "Yes"))
        self.assertNotIn("t_actual", i)

    def test_the_thicknesses_come_from_the_inspection_data_and_the_equipment_register(self):
        eid, cid, d = self.completed_draft("V-K-2")
        self.assertEqual(d["effective_inputs"]["t_actual"], 10.0)
        self.assertEqual(d["effective_inputs"]["t_min"], 8.0)
        self.assertEqual((d["sources"]["t_actual"], d["sources"]["t_min"]), ("inspection data", "equipment register"))
        self.assertEqual(d["facts"]["governing_location"], "S-1")
        self.assertEqual(d["result"]["derived"]["remaining_allowance"], 2.0)

    def test_a_typed_thickness_does_not_beat_the_inspection_data(self):
        eid, cid, d = self.draft("V-K-3")
        r = self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": {"t_actual": 99}}, headers=self.h("eng"))
        self.assertEqual(r.json()["effective_inputs"]["t_actual"], 10.0)

    def test_the_corrosion_rate_follows_the_chosen_kind(self):
        eid, cid, d = self.draft("V-K-4")
        for kind, expected in (("Controlling", 0.30), ("Short term", 0.25), ("Long term", 0.10)):
            r = self.client.post(f"{API}/assessments/{d['id']}/preview", json={"inputs": {"internal_corrosion_rate_kind": kind, "internal_corrosion_rate": 9}}, headers=self.h("eng"))
            self.assertEqual(r.json()["effective_inputs"]["internal_corrosion_rate"], expected, kind)
            self.assertEqual(r.json()["sources"]["internal_corrosion_rate"], "inspection data")
        r = self.client.post(f"{API}/assessments/{d['id']}/preview", json={"inputs": {"internal_corrosion_rate_kind": "Expected", "internal_corrosion_rate": 0.4}}, headers=self.h("eng"))
        self.assertEqual((r.json()["effective_inputs"]["internal_corrosion_rate"], r.json()["sources"]["internal_corrosion_rate"]), (0.4, "person"))

    def test_a_date_saved_on_the_component_after_the_draft_was_started_is_used(self):
        eid, cid = self.component("V-K-6")
        db = Session()
        db.query(Equipment).filter(Equipment.id == eid).update({"commissioning_date": None})          # like the client's register: no date
        db.commit()
        db.close()
        d = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng")).json()
        self.assertFalse(d["inputs"].get("in_service_date"))
        r = self.client.put(f"{API}/components/{cid}", json={"in_service_date": "2015-01-01"}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 200, r.text)
        view = self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("view")).json()
        self.assertEqual(view["inputs"]["in_service_date"], "2015-01-01")
        self.assertEqual(view["result"]["derived"]["age_years"] > 10, True)
        # an own date of the assessment wins
        own = self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": {"in_service_date": "2020-01-01"}}, headers=self.h("eng")).json()
        self.assertEqual(own["inputs"]["in_service_date"], "2020-01-01")

    def test_a_component_without_inspection_data_needs_the_thicknesses_typed(self):
        eid = make_vessel("V-K-5", with_readings=False)
        c = self.client.post(f"{API}/equipment/{eid}/components", json={"name": "Shell", "component_type": "Shell"}, headers=self.h("eng")).json()
        d = self.client.post(f"{API}/components/{c['component']['id']}/assessments", headers=self.h("eng")).json()
        self.assertIsNone(d["facts"]["t_actual"])
        r = self.client.post(f"{API}/assessments/{d['id']}/preview", json={"inputs": {"t_actual": 10, "t_min": 8}}, headers=self.h("eng"))
        self.assertEqual((r.json()["sources"]["t_actual"], r.json()["effective_inputs"]["t_min"]), ("person", 8.0))


class WhatThePersonWrites(Base_):
    def put(self, d, inputs, user="eng"):
        return self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": inputs}, headers=self.h(user))

    def test_a_wrong_value_is_refused_with_a_message_and_nothing_is_saved(self):
        eid, cid, d = self.draft("V-W-1")
        cases = ({"detection_time": "soon"}, {"detection_time": -1}, {"external_inspections": 2.5}, {"coating_quality": "Magic"},
                 {"in_service_date": "yesterday"}, {"toxic_fluid": "Moon water"}, {"internal_corrosion_rate_kind": "Guess"},
                 {"mechanisms": [{"name": "Moon dust"}]}, {"mechanisms": [{"name": "Internal Corrosion", "susceptibility": 9}]},
                 {"mechanisms": [{"name": "Internal Corrosion"}, {"name": "internal corrosion"}]},
                 {"mechanisms": [{"name": "Internal Corrosion", "inspection_effectiveness": "Z"}]})
        for body in cases:
            r = self.put(d, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.json()["detail"])
        self.assertNotIn("detection_time", self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("view")).json()["inputs"])

    def test_choices_ignore_capitals_and_are_saved_in_the_workbooks_words(self):
        eid, cid, d = self.draft("V-W-2")
        r = self.put(d, {"coating_quality": "  v good/NEW ", "insulated": "no", "toxic_fluid": "furfural"})
        self.assertEqual(r.status_code, 200, r.text)
        i = r.json()["inputs"]
        self.assertEqual((i["coating_quality"], i["insulated"], i["toxic_fluid"]), ("V Good/New", "No", "Furfural"))

    def test_at_most_8_mechanisms(self):
        eid, cid, d = self.draft("V-W-3")
        names = ["Internal Corrosion", "External corrosion"] * 5
        r = self.put(d, {"mechanisms": [{"name": n} for n in names]})
        self.assertEqual(r.status_code, 400)

    def test_preview_shows_the_result_without_saving(self):
        eid, cid, d = self.draft("V-W-4")
        r = self.client.post(f"{API}/assessments/{d['id']}/preview", json={"inputs": {"detection_time": 30}}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("detection_time", self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("view")).json()["inputs"])
        f = {x["id"]: x for x in r.json()["result"]["factors"]}
        self.assertEqual(f["C2.4"]["score"], 5)
        self.assertFalse(r.json()["result"]["complete"])


class TheLifeOfAnAssessment(Base_):
    def test_an_incomplete_assessment_cannot_be_finalized_and_says_what_is_missing(self):
        eid, cid, d = self.draft("V-L-1")
        r = self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Still to fill in", r.json()["detail"])
        self.assertIn("Detection time", r.json()["detail"])

    def test_finalize_keeps_the_result_and_the_version_of_the_rules_and_locks_the_inputs(self):
        eid, cid, d = self.completed_draft("V-L-2")
        r = self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        self.assertEqual((a["state"], a["finalized_by"], a["rules_version"]), ("finalized", "eng", load_rules()["version"]))
        self.assertIn(a["result"]["risk"]["level"], ("Low", "Medium", "Medium High", "High"))
        self.assertTrue(a["result"]["complete"])
        db = Session()
        row = db.query(RbiAssessment).filter(RbiAssessment.id == d["id"]).one()
        self.assertEqual(row.results["result"]["risk"], a["result"]["risk"])
        db.close()
        for call in (lambda: self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": {"detection_time": 1}}, headers=self.h("eng")),
                     lambda: self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))):
            self.assertEqual(call().status_code, 400)
        self.assertEqual(self.client.delete(f"{API}/assessments/{d['id']}", headers=self.h("eng")).status_code, 400)

    def test_only_an_admin_approves_and_not_what_he_finalized_himself(self):
        eid, cid, d = self.completed_draft("V-L-3")
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("eng2")).status_code, 403)       # a role problem
        ok = self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm"))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["state"], ok.json()["approved_by"]), ("approved", "adm"))
        eid2, cid2, d2 = self.completed_draft("V-L-4", user="adm")
        self.assertEqual(self.client.post(f"{API}/assessments/{d2['id']}/finalize", json={"approver": "adm2"}, headers=self.h("adm")).status_code, 200)
        own = self.client.post(f"{API}/assessments/{d2['id']}/approve", headers=self.h("adm"))
        self.assertEqual(own.status_code, 403)
        self.assertIn("Only adm2 can approve", own.json()["detail"])
        self.assertEqual(self.client.post(f"{API}/assessments/{d2['id']}/approve", headers=self.h("adm2")).status_code, 200)

    def test_finalize_is_sent_to_one_admin_who_alone_decides_and_is_told(self):
        from app.models.notification import Notification
        eid, cid, d = self.completed_draft("V-L-S1")
        fin = f"{API}/assessments/{d['id']}/finalize"
        # the admin must be chosen, an admin, and not oneself
        self.assertEqual(self.client.post(fin, json={}, headers=self.h("eng")).status_code, 400)
        self.assertEqual(self.client.post(fin, json={"approver": "eng2"}, headers=self.h("eng")).status_code, 400)             # an inspector, not an admin
        self.assertEqual(self.client.post(fin, json={"approver": "nobody"}, headers=self.h("eng")).status_code, 400)
        ok = self.client.post(fin, json={"approver": "adm", "comment": "Please check the corrosion rate"}, headers=self.h("eng"))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["assigned_reviewer"], ok.json()["submit_comment"]), ("adm", "Please check the corrosion rate"))
        # the admin it was sent to is told; nobody else is
        db = Session()
        try:
            told = db.query(Notification).filter(Notification.record_id == uuid.UUID(d["id"])).all()
            self.assertEqual([(n.recipient, n.kind) for n in told], [("adm", "risk_assigned")])
            self.assertIn("Please check the corrosion rate", told[0].detail)
        finally:
            db.close()
        # only that admin decides: another admin cannot approve or return it
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm2")).status_code, 403)
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/return", json={"reason": "x"}, headers=self.h("adm2")).status_code, 403)
        # approving tells the person who finalized it
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm")).status_code, 200)
        db = Session()
        try:
            kinds = sorted((n.recipient, n.kind) for n in db.query(Notification).filter(Notification.record_id == uuid.UUID(d["id"])).all())
            self.assertEqual(kinds, [("adm", "risk_assigned"), ("eng", "risk_approved")])
        finally:
            db.close()

    def test_a_return_tells_the_person_and_a_withdraw_tells_the_admin(self):
        from app.models.notification import Notification
        eid, cid, d = self.completed_draft("V-L-S2")
        fin = f"{API}/assessments/{d['id']}/finalize"
        self.client.post(fin, json={"approver": "adm"}, headers=self.h("eng"))
        # only the person who sent it (or a super admin) takes it back
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/withdraw", headers=self.h("eng2")).status_code, 403)
        w = self.client.post(f"{API}/assessments/{d['id']}/withdraw", headers=self.h("eng"))
        self.assertEqual(w.status_code, 200, w.text)
        self.assertEqual((w.json()["state"], w.json()["assigned_reviewer"]), ("draft", None))
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/withdraw", headers=self.h("eng")).status_code, 400)       # not waiting any more
        # sent again, to the other admin, and returned with a reason
        self.client.post(fin, json={"approver": "adm2"}, headers=self.h("eng"))
        r = self.client.post(f"{API}/assessments/{d['id']}/return", json={"reason": "Check the cooling tower distance"}, headers=self.h("adm2"))
        self.assertEqual((r.status_code, r.json()["assigned_reviewer"]), (200, None))
        db = Session()
        try:
            kinds = sorted((n.recipient, n.kind) for n in db.query(Notification).filter(Notification.record_id == uuid.UUID(d["id"])).all())
            self.assertEqual(kinds, [("adm", "risk_assigned"), ("adm", "risk_withdrawn"), ("adm2", "risk_assigned"), ("eng", "risk_returned")])
        finally:
            db.close()

    def test_a_returned_assessment_is_a_draft_again_with_the_reason(self):
        eid, cid, d = self.completed_draft("V-L-5")
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/return", json={}, headers=self.h("adm")).status_code, 400)
        r = self.client.post(f"{API}/assessments/{d['id']}/return", json={"reason": "Check the corrosion rate"}, headers=self.h("adm"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["state"], r.json()["return_reason"]), ("draft", "Check the corrosion rate"))
        again = self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.assertEqual(again.status_code, 200)
        self.assertIsNone(again.json()["return_reason"])

    def test_a_new_revision_copies_the_last_and_supersedes_it_when_approved(self):
        eid, cid, d = self.completed_draft("V-L-6")
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        blocked = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng"))
        self.assertEqual(blocked.status_code, 400)                                   # one is waiting for approval
        self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm"))
        r2 = self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng"))
        self.assertEqual(r2.status_code, 201, r2.text)
        rev2 = r2.json()
        self.assertEqual(rev2["revision"], 2)
        self.assertEqual(rev2["inputs"]["detection_time"], 30)                      # copied from revision 1
        self.assertEqual(self.client.post(f"{API}/components/{cid}/assessments", headers=self.h("eng")).status_code, 400)   # a draft exists
        changed = self.client.put(f"{API}/assessments/{rev2['id']}", json={"inputs": {**rev2["inputs"], "detection_time": 1}}, headers=self.h("eng"))
        self.assertEqual(changed.status_code, 200, changed.text)
        self.client.post(f"{API}/assessments/{rev2['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.client.post(f"{API}/assessments/{rev2['id']}/approve", headers=self.h("adm"))
        comp = self.client.get(f"{API}/components/{cid}", headers=self.h("view")).json()
        self.assertEqual([(a["revision"], a["state"]) for a in comp["assessments"]], [(2, "approved"), (1, "superseded")])
        self.assertEqual(comp["summary"]["counted_revision"], 2)
        old = self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("view")).json()
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["inputs"]["detection_time"], 30)                       # the record of what was approved stays

    def test_a_draft_can_be_taken_back_by_an_inspector(self):
        eid, cid, d = self.draft("V-L-7")
        self.assertEqual(self.client.delete(f"{API}/assessments/{d['id']}", headers=self.h("eng")).status_code, 204)
        self.assertEqual(self.client.get(f"{API}/assessments/{d['id']}", headers=self.h("view")).status_code, 404)

    def test_a_super_admin_and_a_viewer_cannot_work_on_assessments(self):
        eid, cid, d = self.draft("V-L-8")
        for user in ("view", "sup"):
            self.assertEqual(self.client.put(f"{API}/assessments/{d['id']}", json={"inputs": {}}, headers=self.h(user)).status_code, 403, user)
            self.assertEqual(self.client.post(f"{API}/components/{cid}/assessments", headers=self.h(user)).status_code, 403, user)
            self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/finalize", headers=self.h(user)).status_code, 403, user)
        self.assertEqual(self.client.post(f"{API}/assessments/{d['id']}/preview", json={"inputs": {}}, headers=self.h("sup")).status_code, 200)

    def test_every_step_is_in_the_audit_trail(self):
        eid, cid, d = self.completed_draft("V-L-9")
        self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng"))
        self.client.post(f"{API}/assessments/{d['id']}/approve", headers=self.h("adm"))
        db = Session()
        rows = db.query(AuditLog).filter(AuditLog.target.in_([cid, d["id"]])).all()
        db.close()
        self.assertEqual(sorted(r.action for r in rows), ["rbi.assessment.approve", "rbi.assessment.create", "rbi.assessment.finalize", "rbi.component.create"])
        self.assertEqual({r.actor for r in rows}, {"eng", "adm"})


class TheWholePlant(Base_):
    def test_the_matrix_and_the_summary_count_finalized_and_approved_assessments(self):
        before = self.client.get(f"{API}/summary", headers=self.h("view")).json()
        eid, cid, d = self.completed_draft("V-P-1")
        waiting = self.client.get(f"{API}/summary", headers=self.h("view")).json()
        self.assertEqual(waiting["counted"], before["counted"])                       # a draft does not count
        fin = self.client.post(f"{API}/assessments/{d['id']}/finalize", json={"approver": "adm"}, headers=self.h("eng")).json()
        mid = self.client.get(f"{API}/summary", headers=self.h("view")).json()
        self.assertEqual(mid["counted"], before["counted"] + 1)
        self.assertEqual(mid["totals"]["awaiting_approval"], before["totals"]["awaiting_approval"] + 1)
        matrix = self.client.get(f"{API}/matrix", headers=self.h("view")).json()
        cell = next(c for c in matrix["cells"] if c["pof"] == fin["result"]["pof"]["category"] and c["cof"] == fin["result"]["cof"]["category"])
        self.assertEqual(cell["level"], fin["result"]["risk"]["level"])
        self.assertIn(cid, [c["component_id"] for c in cell["components"]])
        self.assertEqual(sum(c["count"] for c in matrix["cells"]), matrix["total"])
        self.assertEqual(len(matrix["cells"]), 25)
        listed = self.client.get(f"{API}/equipment", params={"q": "V-P-1"}, headers=self.h("view")).json()["items"][0]
        self.assertEqual((listed["assessed"], listed["awaiting_approval"], listed["highest"]["risk_level"]), (1, 1, fin["result"]["risk"]["level"]))

    def test_the_most_urgent_list_is_ordered_by_priority(self):
        top = self.client.get(f"{API}/summary", headers=self.h("view")).json()["most_urgent"]
        priorities = [t["priority"] for t in top]
        self.assertEqual(priorities, sorted(priorities))


if __name__ == "__main__":
    unittest.main()
