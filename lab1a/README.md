# FNMS Lab1a — Foundations Auth App

A minimal full-stack authentication app: a FastAPI + Postgres backend with
hand-rolled JWT auth and argon2id password hashing, plus a static
HTML/Tailwind/vanilla-JS frontend. It is the bedrock for future assignments.

## Prerequisites

- Docker Desktop (for the Postgres database)
- Python 3.11+ (tested on 3.13)

## Architecture

- Backend: FastAPI on `http://localhost:8000`
- Frontend: static files served on `http://localhost:5173` (a **different origin**,
  so requests are genuinely cross-origin — CORS is configured on the backend)
- Database: Postgres 16 in Docker with a **named volume**, so data survives restarts

## Run it (copy-paste, in order)

### 1. Start the database

```bash
docker compose up -d
```

### 2. Start the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python seed.py                       # creates the grader account NYUgrader / Courant2026!
uvicorn app.main:app --reload --port 8000
```

Leave this running. Health check: <http://localhost:8000/healthz> → `{"status":"ok"}`

### 3. Start the frontend (new terminal)

```bash
cd frontend
python3 -m http.server 5173
```

Open <http://localhost:5173> and register or log in.

### Grader account

- Username: `NYUgrader`
- Password: `Courant2026!`

Created by `python seed.py` (idempotent — re-running resets the password).

## Environment

See [backend/.env.example](backend/.env.example). A working `.env` for the
throwaway database is committed at [backend/.env](backend/.env) (per the
assignment's exception for disposable DB credentials). No personal secrets are
included.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy connection string to the Dockerized Postgres |
| `JWT_SECRET` | HMAC secret used to sign/verify JWTs (throwaway value) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token lifetime |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins |

## API

JSON in, JSON out. Protected routes require `Authorization: Bearer <token>`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/healthz` | no | `{"status":"ok"}` |
| POST | `/api/auth/register` | no | create an account → `201` with user |
| POST | `/api/auth/login` | no | `{"token": "..."}` |
| GET | `/api/auth/me` | yes | the logged-in user |
| GET | `/api/users/:id` | yes | read a user |
| PATCH | `/api/users/:id` | yes | update email/password |
| DELETE | `/api/users/:id` | yes | delete a user → `204` |

### The three rules

1. **Password hash is never returned.** Response schemas
   ([backend/app/schemas.py](backend/app/schemas.py)) expose only
   `id, username, email, created_at`; `password_hash` cannot be serialized.
2. **No token / bad token / expired token → 401.** Enforced in
   `get_current_user` ([backend/app/security.py](backend/app/security.py)).
3. **You cannot touch another user's account → 404.** We chose **404** over 403
   (used consistently for GET, PATCH, and DELETE) so the API never reveals that
   an account with someone else's id exists — this avoids user enumeration.
   See `_get_own_user_or_404` in [backend/app/main.py](backend/app/main.py).

## Security notes

- Passwords are hashed with **argon2id** via `argon2-cffi`
  ([backend/app/security.py](backend/app/security.py)).
- Login and register return generic errors to avoid confirming which accounts exist.

## Verify persistence

```bash
docker compose restart db   # or: docker compose down && docker compose up -d
```

Your registered users are still there because the data lives in the `pgdata`
named volume.

## Development

Linting and formatting use [Ruff](https://docs.astral.sh/ruff/) (config in
[backend/pyproject.toml](backend/pyproject.toml)):

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-dev.txt   # installs ruff alongside runtime deps
ruff check .                          # lint
ruff check --fix .                    # lint + autofix
ruff format .                         # format
```
