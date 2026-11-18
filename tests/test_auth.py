"""Login, tokens and role-based access (viewer < engineer < admin < super_admin) on a TEMPORARY database.

Passwords are random per run; nothing here is a real credential.
Run from backend/:  python -m unittest tests.test_auth -v
"""
import os
import secrets
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import deps  # noqa: E402
from app.api.deps import required_role  # noqa: E402
from app.api.endpoints import auth as auth_endpoints  # noqa: E402
from app.core import security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
import app.models.equipment, app.models.inspection, app.models.cml, app.models.user  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

BASE_URL = make_url(settings.DATABASE_URL)
TEST_DB = "ams_test_" + uuid.uuid4().hex[:8]
engine = create_engine(BASE_URL.set(database=TEST_DB), pool_pre_ping=True)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
PW = {r: secrets.token_urlsafe(14) for r in ("super_admin", "admin", "engineer", "viewer")}


_auth_before = settings.AUTH_REQUIRED


def setUpModule():
    settings.AUTH_REQUIRED = True            # login is off by default; these tests check it switched ON
    admin = create_engine(BASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()
    Base.metadata.create_all(bind=engine)
    db = Session()
    for role in PW:
        db.add(User(username=f"{role}1", full_name=role.title(), role=role,
                    password_hash=security.hash_password(PW[role])))
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


class PasswordsAndTokens(unittest.TestCase):
    def test_hash_and_verify(self):
        h = security.hash_password("a-long-enough-pass")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(security.verify_password("a-long-enough-pass", h))
        self.assertFalse(security.verify_password("a-long-enough-pasS", h))
        self.assertNotEqual(h, security.hash_password("a-long-enough-pass"))      # random salt
        self.assertFalse(security.verify_password("x", "garbage"))

    def test_password_rules(self):
        self.assertIsNotNone(security.validate_new_password("short"))
        self.assertIsNotNone(security.validate_new_password(" leading-space-here"))
        self.assertIsNone(security.validate_new_password("long-enough-1"))

    def test_token_roundtrip_expiry_and_tampering(self):
        t = security.create_access_token("bob", "viewer")
        self.assertEqual(security.decode_access_token(t)["sub"], "bob")
        self.assertIsNone(security.decode_access_token(security.create_access_token("bob", "viewer", minutes=-1)))
        self.assertIsNone(security.decode_access_token(t[:-3] + "abc"))
        forged = jwt.encode({"sub": "bob", "role": "admin",
                             "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "another-key", algorithm="HS256")
        self.assertIsNone(security.decode_access_token(forged))
        none_alg = jwt.encode({"sub": "bob"}, None, algorithm="none")
        self.assertIsNone(security.decode_access_token(none_alg))

    def test_role_rule_per_method(self):
        self.assertEqual(required_role("GET", "/api/v1/equipment"), "viewer")
        self.assertEqual(required_role("POST", "/api/v1/equipment"), "engineer")
        self.assertEqual(required_role("PUT", "/api/v1/inspection/abc"), "engineer")
        self.assertEqual(required_role("DELETE", "/api/v1/inspection/abc"), "admin")
        self.assertEqual(required_role("POST", "/api/v1/inspection/abc/approve"), "admin")
        self.assertEqual(required_role("POST", "/api/v1/inspection/abc/return"), "admin")
        self.assertEqual(required_role("POST", "/api/v1/inspection/abc/reopen"), "admin")
        self.assertEqual(required_role("GET", "/api/v1/inspection/approvals"), "admin")
        self.assertEqual(required_role("POST", "/api/v1/inspection/abc/submit"), "engineer")
        self.assertEqual(required_role("POST", "/api/v1/asme/calculate-tmin"), "viewer")
        self.assertEqual(required_role("POST", "/api/v1/auth/change-password"), "viewer")
        self.assertEqual(required_role("GET", "/api/v1/auth/users"), "super_admin")
        self.assertEqual(required_role("PATCH", "/api/v1/auth/users/bob"), "super_admin")
        self.assertEqual(required_role("GET", "/api/v1/auth/audit"), "super_admin")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        auth_endpoints._failures.clear()

    def login(self, role):
        r = self.client.post("/api/v1/auth/login", json={"username": f"{role}1", "password": PW[role]})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    NEW_EQ = {"equipment_id": "T-1", "equipment_name": "Test", "equipment_type": "Vessel",
              "created_by": "someone-else"}

    def test_public_and_protected(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/equipment").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/equipment", headers={"Authorization": "Bearer nonsense"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/equipment", json=self.NEW_EQ).status_code, 401)

    def test_login_failures_and_throttle(self):
        bad = {"username": "viewer1", "password": "wrong-password"}
        self.assertEqual(self.client.post("/api/v1/auth/login", json=bad).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"}).status_code, 401)
        for _ in range(auth_endpoints.MAX_FAILURES):
            self.client.post("/api/v1/auth/login", json=bad)
        self.assertEqual(self.client.post("/api/v1/auth/login", json=bad).status_code, 429)
        # even the right password is refused while locked
        self.assertEqual(self.client.post("/api/v1/auth/login",
                                          json={"username": "viewer1", "password": PW["viewer"]}).status_code, 429)

    def test_viewer_reads_but_cannot_write(self):
        h = self.login("viewer")
        self.assertEqual(self.client.get("/api/v1/equipment", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/equipment", json=self.NEW_EQ, headers=h).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/users", headers=h).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/audit", headers=h).status_code, 403)
        calc = {"component_type": "cylindrical_shell", "design_pressure": 5, "pressure_unit": "bar",
                "inside_diameter": 1000, "dimension_unit": "mm", "design_temperature": 150,
                "temperature_unit": "celsius", "joint_efficiency": 0.85, "spec_number": "X", "grade": "X",
                "product_form": "X", "custom_stress": 137.9, "custom_stress_unit": "mpa",
                "custom_stress_source": "test"}
        self.assertEqual(self.client.post("/api/v1/asme/calculate-tmin", json=calc, headers=h).status_code, 200)

    def test_engineer_writes_but_cannot_delete_and_user_is_recorded(self):
        h = self.login("engineer")
        r = self.client.post("/api/v1/equipment", json=self.NEW_EQ, headers=h)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["created_by"], "engineer1")                    # not "someone-else"
        eid = r.json()["id"]
        r = self.client.put(f"/api/v1/equipment/{eid}", json={"equipment_name": "Renamed", "equipment_type": "Vessel"}, headers=h)
        self.assertEqual((r.status_code, r.json()["updated_by"]), (200, "engineer1"))
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{eid}", headers=h).status_code, 403)
        ha = self.login("admin")
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{eid}", headers=ha).status_code, 200)

    def test_upload_requires_engineer(self):
        pdf = {"file": ("doc.pdf", b"%PDF-1.4 test", "application/pdf")}
        self.assertEqual(self.client.post("/api/v1/equipment/upload-document", files=pdf,
                                          headers=self.login("viewer")).status_code, 403)
        r = self.client.post("/api/v1/equipment/upload-document", files=pdf, headers=self.login("engineer"))
        self.assertEqual(r.status_code, 200, r.text)
        fake = {"file": ("doc.pdf", b"MZ not a pdf", "application/pdf")}
        self.assertEqual(self.client.post("/api/v1/equipment/upload-document", files=fake,
                                          headers=self.login("engineer")).status_code, 400)

    def test_equipment_documents_live_in_the_database_and_need_a_sign_in(self):
        body = b"%PDF-1.4 datasheet bytes"
        r = self.client.post("/api/v1/equipment/upload-document", headers=self.login("engineer"),
                             files={"file": ("Data Sheet 1.pdf", body, "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["document_url"]
        self.assertTrue(url.startswith("/api/v1/equipment/documents/"), url)
        self.assertEqual(r.json()["document_name"], "Data Sheet 1.pdf")
        self.assertEqual(self.client.get(url).status_code, 401)                       # no token: no document
        for role in ("viewer", "engineer", "admin"):                                  # everybody who is signed in may read it
            got = self.client.get(url, headers=self.login(role))
            self.assertEqual(got.status_code, 200, role)
            self.assertEqual(got.content, body)
            self.assertEqual(got.headers["content-type"], "application/pdf")
            self.assertIn("no-store", got.headers["cache-control"])
        self.assertEqual(self.client.get("/api/v1/equipment/documents/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                         headers=self.login("viewer")).status_code, 404)
        self.assertEqual(self.client.get("/uploads/documents/anything.pdf").status_code, 404)   # no public folder any more

    def test_only_a_super_admin_manages_users(self):
        for role in ("viewer", "engineer", "admin"):
            h = self.login(role)
            self.assertEqual(self.client.get("/api/v1/auth/users", headers=h).status_code, 403, role)
            self.assertEqual(self.client.post("/api/v1/auth/users", headers=h, json={
                "username": "intruder1", "role": "viewer", "password": secrets.token_urlsafe(14)}).status_code, 403, role)
            self.assertEqual(self.client.patch("/api/v1/auth/users/viewer1", headers=h, json={"role": "admin"}).status_code, 403, role)
            self.assertEqual(self.client.get("/api/v1/auth/audit", headers=h).status_code, 403, role)
        hs = self.login("super_admin")
        self.assertEqual(self.client.get("/api/v1/auth/users", headers=hs).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/audit", headers=hs).status_code, 200)

    def test_a_super_admin_sees_everything_but_does_not_enter_or_delete_data(self):
        hs = self.login("super_admin")
        ha = self.login("admin")
        made = self.client.post("/api/v1/equipment", json={**self.NEW_EQ, "equipment_id": "T-SA"}, headers=ha)
        self.assertEqual(made.status_code, 201, made.text)
        eid = made.json()["id"]
        # reads are open
        self.assertEqual(self.client.get("/api/v1/equipment", headers=hs).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/equipment/{eid}", headers=hs).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/inspection", headers=hs).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/asme/calculate-tmin", headers=hs, json={}).status_code != 403, True)   # calculations stay open
        # writes and deletes are not
        denied = self.client.post("/api/v1/equipment", json={**self.NEW_EQ, "equipment_id": "T-SA2"}, headers=hs)
        self.assertEqual(denied.status_code, 403)
        self.assertIn("does not enter", denied.json()["detail"])
        self.assertEqual(self.client.put(f"/api/v1/equipment/{eid}", json={**self.NEW_EQ, "equipment_id": "T-SA"}, headers=hs).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{eid}", headers=hs).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/inspection", headers=hs, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/inspection/import-excel", headers=hs).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/cml/tml", headers=hs, json={}).status_code, 403)
        # but it runs the system, and may withdraw or reopen an inspection (checked by the workflow, not by this rule)
        self.assertEqual(self.client.get("/api/v1/auth/users", headers=hs).status_code, 200)
        self.assertNotEqual(self.client.post("/api/v1/inspection/00000000-0000-0000-0000-000000000000/reopen", headers=hs,
                                             json={"reason": "x"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/equipment/{eid}", headers=ha).status_code, 200)     # an admin still can

    def test_user_management_rules(self):
        hs = self.login("super_admin")
        newpw = secrets.token_urlsafe(14)
        r = self.client.post("/api/v1/auth/users", headers=hs,
                             json={"username": "New.User", "role": "engineer", "password": newpw})
        self.assertEqual((r.status_code, r.json()["username"], r.json()["must_change_password"]), (201, "new.user", True))
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs,
                                          json={"username": "new.user", "role": "viewer", "password": newpw}).status_code, 400)
        r = self.client.post("/api/v1/auth/users", headers=hs, json={"username": "sv.test", "role": "supervisor", "password": newpw})
        self.assertEqual(r.status_code, 400)                                            # there is no separate supervisor role
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs,
                                          json={"username": "weak1", "role": "viewer", "password": "short"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs,
                                          json={"username": "odd1", "role": "king", "password": newpw}).status_code, 400)
        # nobody changes their own role or deactivates themselves
        self.assertEqual(self.client.patch("/api/v1/auth/users/super_admin1", headers=hs, json={"role": "admin"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/auth/users/super_admin1", headers=hs, json={"is_active": False}).status_code, 400)
        # a deactivated user's existing token stops working
        hn = self.client.post("/api/v1/auth/login", json={"username": "new.user", "password": newpw}).json()["access_token"]
        hn = {"Authorization": "Bearer " + hn}
        changed = self.client.post("/api/v1/auth/change-password", headers=hn,
                                   json={"current_password": newpw, "new_password": secrets.token_urlsafe(14)})
        self.assertEqual(changed.status_code, 200)
        hn = {"Authorization": "Bearer " + changed.json()["access_token"]}           # the fresh token of the same person
        self.assertEqual(self.client.get("/api/v1/equipment", headers=hn).status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/auth/users/new.user", headers=hs, json={"is_active": False}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=hn).status_code, 401)

    def test_a_super_admin_can_delete_a_user_but_nobody_else_can(self):
        hs = self.login("super_admin")
        pw = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs, json={
            "username": "leaver", "role": "engineer", "password": pw}).status_code, 201)
        # an admin cannot delete users, only the super admin
        self.assertEqual(self.client.delete("/api/v1/auth/users/leaver", headers=self.login("admin")).status_code, 403)
        # nobody deletes their own account
        self.assertEqual(self.client.delete("/api/v1/auth/users/super_admin1", headers=hs).status_code, 400)
        self.assertEqual(self.client.delete("/api/v1/auth/users/leaver", headers=hs).status_code, 204)
        self.assertEqual(self.client.delete("/api/v1/auth/users/leaver", headers=hs).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "leaver", "password": pw}).status_code, 401)
        lines = [r["action"] for r in self.client.get("/api/v1/auth/audit", headers=hs).json() if r["target"] == "leaver"]
        self.assertIn("user.delete", lines)

    def test_there_is_always_one_active_super_admin(self):
        hs = self.login("super_admin")
        pw = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs, json={
            "username": "second.super", "role": "super_admin", "password": pw}).status_code, 201)
        login = self.client.post("/api/v1/auth/login", json={"username": "second.super", "password": pw}).json()
        h2 = {"Authorization": "Bearer " + login["access_token"]}
        mine = secrets.token_urlsafe(14)
        changed = self.client.post("/api/v1/auth/change-password", headers=h2, json={"current_password": pw, "new_password": mine})
        self.assertEqual(changed.status_code, 200)
        h2 = {"Authorization": "Bearer " + changed.json()["access_token"]}
        # two exist: one can demote the other ...
        self.assertEqual(self.client.patch("/api/v1/auth/users/second.super", headers=hs, json={"role": "admin"}).status_code, 200)
        # ... and that account has lost the power at once (the role is read from the database, not the token)
        self.assertEqual(self.client.patch("/api/v1/auth/users/super_admin1", headers=h2, json={"role": "viewer"}).status_code, 403)
        # only one is left: even another route to remove it is closed
        self.assertEqual(self.client.patch("/api/v1/auth/users/super_admin1", headers=hs, json={"is_active": False}).status_code, 400)

    def test_a_new_user_must_choose_a_password_before_anything_else(self):
        hs = self.login("super_admin")
        temp = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs,
                                          json={"username": "fresh.user", "role": "engineer", "password": temp}).status_code, 201)
        login = self.client.post("/api/v1/auth/login", json={"username": "fresh.user", "password": temp}).json()
        self.assertTrue(login["user"]["must_change_password"])
        h = {"Authorization": "Bearer " + login["access_token"]}
        for url in ("/api/v1/equipment", "/api/v1/inspection", "/api/v1/auth/users"):
            self.assertEqual(self.client.get(url, headers=h).status_code, 403, url)
        self.assertIn("new password", self.client.get("/api/v1/equipment", headers=h).json()["detail"])
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=h).status_code, 200)            # allowed
        self.assertEqual(self.client.post("/api/v1/auth/change-password", headers=h,                # the same password again: refused
                                          json={"current_password": temp, "new_password": temp}).status_code, 400)
        mine = secrets.token_urlsafe(14)
        changed = self.client.post("/api/v1/auth/change-password", headers=h, json={"current_password": temp, "new_password": mine})
        self.assertEqual(changed.status_code, 200)
        h = {"Authorization": "Bearer " + changed.json()["access_token"]}
        self.assertEqual(self.client.get("/api/v1/equipment", headers=h).status_code, 200)          # free to work now
        self.assertFalse(self.client.get("/api/v1/auth/me", headers=h).json()["must_change_password"])

    def test_changing_or_resetting_a_password_ends_every_earlier_sign_in(self):
        hs = self.login("super_admin")
        pw = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/users", headers=hs, json={
            "username": "sessions.user", "role": "engineer", "password": pw}).status_code, 201)
        sign_in = lambda p: {"Authorization": "Bearer " + self.client.post(
            "/api/v1/auth/login", json={"username": "sessions.user", "password": p}).json()["access_token"]}
        first_login = sign_in(pw)
        # the temporary password has to be changed first (forced); the token that comes back works, the earlier one does not
        mine = secrets.token_urlsafe(14)
        changed = self.client.post("/api/v1/auth/change-password", headers=first_login, json={"current_password": pw, "new_password": mine})
        fresh = {"Authorization": "Bearer " + changed.json()["access_token"]}
        self.assertEqual(self.client.get("/api/v1/equipment", headers=fresh).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=first_login).status_code, 401)
        # a second device that signed in with the new password
        other_device = sign_in(mine)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=other_device).status_code, 200)
        # the person changes the password again on one device: the other device is signed out
        newer = secrets.token_urlsafe(14)
        again = self.client.post("/api/v1/auth/change-password", headers=fresh, json={"current_password": mine, "new_password": newer})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=other_device).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/equipment", headers={"Authorization": "Bearer " + again.json()["access_token"]}).status_code, 200)
        # a super admin resets the password (a forgotten or stolen one): whoever is signed in with the old one is out
        live = sign_in(newer)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=live).status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/auth/users/sessions.user", headers=hs, json={"new_password": secrets.token_urlsafe(14)}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/equipment", headers=live).status_code, 401)

    def test_a_token_that_does_not_carry_the_password_fingerprint_is_refused(self):
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone
        old_style = pyjwt.encode({"sub": "engineer1", "role": "engineer", "iat": datetime.now(timezone.utc),
                                  "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, security._secret_key(), algorithm="HS256")
        self.assertEqual(self.client.get("/api/v1/equipment", headers={"Authorization": "Bearer " + old_style}).status_code, 401)

    def test_a_reset_password_is_temporary_too(self):
        hs = self.login("super_admin")
        temp = secrets.token_urlsafe(14)
        self.assertEqual(self.client.patch("/api/v1/auth/users/engineer1", headers=hs, json={"new_password": temp}).status_code, 200)
        login = self.client.post("/api/v1/auth/login", json={"username": "engineer1", "password": temp}).json()
        self.assertTrue(login["user"]["must_change_password"])
        h = {"Authorization": "Bearer " + login["access_token"]}
        self.assertEqual(self.client.get("/api/v1/equipment", headers=h).status_code, 403)
        PW["engineer"] = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/change-password", headers=h,
                                          json={"current_password": temp, "new_password": PW["engineer"]}).status_code, 200)

    def test_user_administration_is_written_to_the_audit_trail(self):
        hs = self.login("super_admin")
        temp = secrets.token_urlsafe(14)
        self.client.post("/api/v1/auth/users", headers=hs, json={"username": "audited.user", "role": "viewer", "password": temp})
        self.client.patch("/api/v1/auth/users/audited.user", headers=hs, json={"role": "engineer", "full_name": "Audit Test"})
        self.client.patch("/api/v1/auth/users/audited.user", headers=hs, json={"is_active": False})
        self.client.patch("/api/v1/auth/users/audited.user", headers=hs, json={"new_password": secrets.token_urlsafe(14)})
        rows = [r for r in self.client.get("/api/v1/auth/audit", headers=hs).json() if r["target"] == "audited.user"]
        actions = [r["action"] for r in rows]
        for a in ("user.create", "user.role", "user.name", "user.deactivate", "user.password_reset"):
            self.assertIn(a, actions)
        self.assertTrue(all(r["actor"] == "super_admin1" for r in rows))
        role_line = next(r for r in rows if r["action"] == "user.role")
        self.assertEqual(role_line["detail"], "viewer -> engineer")
        self.assertNotIn(temp, str(rows))                       # no password ever lands in the trail

    def test_change_password(self):
        h = self.login("viewer")
        new = secrets.token_urlsafe(14)
        self.assertEqual(self.client.post("/api/v1/auth/change-password", headers=h,
                                          json={"current_password": "wrong", "new_password": new}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/change-password", headers=h,
                                          json={"current_password": PW["viewer"], "new_password": "short"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/auth/change-password", headers=h,
                                          json={"current_password": PW["viewer"], "new_password": new}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "viewer1", "password": PW["viewer"]}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "viewer1", "password": new}).status_code, 200)
        PW["viewer"] = new
        lines = [r["action"] for r in self.client.get("/api/v1/auth/audit", headers=self.login("super_admin")).json()
                 if r["target"] == "viewer1"]
        self.assertIn("password.change", lines)

    def test_me(self):
        r = self.client.get("/api/v1/auth/me", headers=self.login("engineer"))
        self.assertEqual((r.status_code, r.json()["role"]), (200, "engineer"))
        self.assertNotIn("hash", r.text)                       # the stored password never leaves the server

    def test_auth_can_be_switched_off_for_local_experiments(self):
        settings.AUTH_REQUIRED = False
        try:
            self.assertEqual(self.client.get("/api/v1/equipment").status_code, 200)
        finally:
            settings.AUTH_REQUIRED = True
        self.assertEqual(self.client.get("/api/v1/equipment").status_code, 401)


if __name__ == "__main__":
    unittest.main()
