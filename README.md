# AMS / RBI backend

FastAPI + SQLAlchemy + PostgreSQL. Settings come from `backend/.env` (see `.env.example`; the real `.env` is not in git).

## Run
```
python -m uvicorn app.main:app --reload --port 8000
```
Database changes are applied with `alembic upgrade head`.

## Sign-in (OFF by default)
Login is off unless `AUTH_REQUIRED=true` is set in the environment (and `NEXT_PUBLIC_AUTH_REQUIRED=true` in the
frontend). When on, every API call except `/api/v1/auth/login` needs a token. Create the first super admin once, on the
server, from `backend/`:
```
python -m app.seed.create_user --username <name> --role super_admin --full-name "<Full name>"
```
The password is asked for interactively (at least 10 characters). Everybody else is created by the super admin on the
Users screen (they get a temporary password and must choose their own at the first sign-in). Roles:

| role | can do |
|---|---|
| viewer | read everything, run the t_min calculator |
| engineer (Inspector) | + create / edit equipment, inspections, tasks and reports; imports; document uploads; send for approval |
| admin | + approve / return / reject what is sent to them, delete records |
| super_admin | users, roles and the audit log; reads everything, enters no inspection data (can withdraw / reopen so nobody is stuck) |

Tokens last `ACCESS_TOKEN_EXPIRE_MINUTES` (default 10080 = 7 days) and stop working as soon as that person's password is
changed or reset. The signing key is `SECRET_KEY` (set it on the server), otherwise it is created once in
`backend/.secret_key` (not in git).

Documents (equipment datasheets, report files) are stored in the database and are only given to signed-in people;
there is no public uploads folder.

## Tests
```
python -m unittest discover -s tests
```
They use throw-away databases (`ams_test_*`) on the same server and never touch the real one.
Formula tests follow the client's sheets (`T min calculation.xlsx`, `Equipment Register-Rev02.xlsx`,
`tank calculations.xlsx`).

## Material stress data (ASME II-D Table 1A / 1B)
`app/seed/seed_full_pdf.py` extracts the tables from the PDF and finishes with `app/seed/backfill_asme_metadata.py`
(clean spec / grade / UNS / page / line columns). Run the backfill alone (`python -m app.seed.backfill_asme_metadata
--apply`) to repair or extend an existing table; without `--apply` it is a dry run.
