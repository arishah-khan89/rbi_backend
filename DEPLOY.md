# Deploy checklist (backend first, then frontend)

Nothing here has been run against the real server yet. The steps that depend on how the Railway project is set up are
marked **check**.

## 0. Before anything is pushed (on your computer)

1. **Rotate the database password.** `backend/.env` was committed once (it is staged as deleted, but the old commit
   still has it). Treat that password as public: change it in Railway's Postgres settings and use the new one only in
   Railway's variables. (The `.env` on your computer is not touched by this.)
2. Stop tracking the files that must not be in git (this only changes the index, nothing is deleted from disk):
   ```
   cd C:\client\RBI\backend
   git rm -r --cached uploads
   git status        # .env and .secret_key must show as deleted-from-index, never as "modified" or "new"
   ```
3. Never use `git add .` in `C:\client\RBI` (the root folder): it holds the client's Excel files and the large ASME PDF.
   Push `backend` and `frontend` from their own folders, each its own repository.
4. Run the tests once more: `cd backend && python -m unittest discover -s tests` (all must pass) and `npx tsc --noEmit` in `frontend`.

## 1. Backend (Railway)

Variables to set in the Railway service (never in git):

| variable | value |
|---|---|
| `DATABASE_URL` | the Railway Postgres URL (with the new password) |
| `AUTH_REQUIRED` | `true` |
| `SECRET_KEY` | a long random text, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Without it the key is created on the server's disk and **changes on every redeploy**, which signs everybody out |
| `CORS_ORIGINS` | the exact address of the frontend, e.g. `https://your-frontend.up.railway.app` (no trailing slash) |
| `APP_ENV` | `production` (database error details are hidden from users) |

Start command (**check** what the service uses now): the database changes must run before the app starts, e.g.
`alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

The new migrations (reports, tasks, notifications, equipment documents) are additive. The equipment-documents migration
also moves any datasheet file that still exists on the server's disk into the database; files that are already gone stay
as they are and show "This document is no longer available. Please attach it again."

## 2. First super admin (once)

Open a shell on the Railway service (**check**: Railway CLI `railway run` / the service shell) and run:
```
python -m app.seed.create_user --username <name> --role super_admin --full-name "<Full name>"
```
The password is typed there (at least 10 characters). After that this person creates the admins and inspectors on the
Users screen; they get a temporary password and must choose their own at the first sign-in.

## 3. Frontend

Variables (in the frontend service, at build time):

| variable | value |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | the backend's public address |
| `NEXT_PUBLIC_AUTH_REQUIRED` | `true` |
| `NEXT_PUBLIC_IDLE_MINUTES` | `0` for no automatic sign-out (or the number of minutes the client wants) |

`NEXT_PUBLIC_*` values are baked in when the frontend is built, so change them and **redeploy**, not just restart.

## 4. After the deploy: check in this order

1. `https://<backend>/health` answers `ok`.
2. Open the frontend, sign in as the super admin; create one admin and one inspector; sign in as each.
3. As the inspector: add equipment, attach a PDF, open it (it opens only while signed in).
4. Create a task and a report, send it to the admin, approve it as the admin.
5. Opening `https://<backend>/api/v1/equipment` without signing in must answer 401.

## 5. Tell the client

- Where there is a measured thickness loss, the corrosion rate is never taken below 0.001 (`app/crud/cml.py`, lines
  103 and 108), so a very small loss cannot give an absurdly long life. This floor is not in the client's sheet.
- Task dates are written by the person; approving a report does not change them (question still open with the client).

## 6. Risk Assessment (RBI) after the client's workbook `030-E-11- Shell.xlsx`
What the migrations do: `s9n0i1j2k3l4` adds four new tables (fluid list, damage-mechanism library, components, assessments). Nothing that
exists is changed. The older RBI tables of the first version (`rbi_fluids`, `rbi_damage_mechanisms`, `rbi_analyses`) stay in the database
unused; they can be dropped later (they hold nothing the new module needs).

The scoring rules (`backend/app/rbi_engine/rules.json`) are in the code and need nothing: they were READ from the workbook.
The **lists** (fluids, damage mechanisms) must be loaded once. From your computer, against the Railway database (use the Postgres
service's public URL; turn the TCP proxy off again afterwards):
```
cd C:\client\RBIackend
$env:DATABASE_URL = "<Railway public URL>"
.venv\Scripts\python.exe -m app.seed.import_rbi_lists --file "C:/client/RBI/030-E-11- Shell.xlsx"           # shows what it would do
.venv\Scripts\python.exe -m app.seed.import_rbi_lists --file "C:/client/RBI/030-E-11- Shell.xlsx" --apply   # does it
```
It also lists what is wrong in the client's lists (duplicate IDs, a mechanism without a location, fluids whose flammability is not an NFPA
rating). Until this is done the fluid and mechanism lists in the form are empty.

The second migration of this module, `t0o1k2l3m4n5`, adds two empty, optional columns to `rbi_assessments` (`assigned_reviewer`, `submit_comment`);
it is additive like the first (the start command `alembic upgrade head` runs it).

Rights: an Inspector creates components, works on assessments and finalizes them by **sending them to one chosen Admin** (with an optional
note); only that Admin approves or returns it (never the person who sent it), and the sender can withdraw it until it is decided. An Admin
also deletes components; a Super Admin and a Viewer only look. The Admin gets a notification (bell) when it is sent, the Inspector when it is
approved or returned. Every step is in the audit log. Without a login (`AUTH_REQUIRED` false) no Admin is chosen.

Rules of the software that are NOT in the client's sheet (they are in `RBI_CLIENT_QUESTIONS.md`, to confirm with the client): the probability
asks and counts only the side (external / internal) of the damage mechanisms entered (a draft with no damage mechanism stays incomplete);
the CUI questions are not asked when the vessel is not insulated; approval goes to one chosen Admin.

One typing slip of the client's workbook is corrected on purpose (`CORRECTIONS` in `app/rbi_engine/rules_builder.py`: the toxic table
`Score_Bands!A146` 5000 is used as 500, to be confirmed by the client). It stops by itself when the client fixes the cell, and it is always
listed under "known_issues".

One setting of the client's Config sheet is also chosen on purpose (`SETTING_CHOICES` in the same file): `Config!B6` (normalise the external score of an
uninsulated item) is 0 in the workbook and 1 in the software, as the workbook itself recommends; to be confirmed by the client.

When the client sends a newer workbook:
1. `python -m app.rbi_engine.rules_builder --file <new workbook>` shows what changed (a formula it does not understand stops the build with a
   message), then add `--apply` to write `app/rbi_engine/rules.json`.
2. Load the lists again as above.
3. Run the tests (`python -m unittest discover -s tests`): the engine tests compare the software with the workbook cell by cell, and
   `tests.test_rbi_engine.Builder` fails when `rules.json` is older than the workbook.
4. Push. Assessments that are already finalized keep the result they were approved with and show the version of the rules they used.
