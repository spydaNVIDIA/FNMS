"""Reusable offline PostgreSQL trial; refuses targets outside local Lab 2 databases.

Run as a standalone script. Provider calls are replaced with labeled fixtures.
The current application process is never restarted or contacted.
"""

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def validate_target(value):
    """Check before importing the app or opening a connection; query overrides forbidden."""
    try:
        target = make_url(value)
        permitted = (
            target.drivername == "postgresql+psycopg"
            and target.host in {"127.0.0.1", "localhost"}
            and target.port in {5434, 5435}
            and target.username == "lab2"
            and target.database == "lab2"
            and not target.query
        )
    except Exception:
        raise ValueError("Invalid PostgreSQL trial URL") from None
    if not permitted:
        raise ValueError(
            "Trial requires PostgreSQL/psycopg, local port5434 or5435, role lab2, "
            "database lab2, and no URL query overrides"
        )
    return target


def fixture_snapshot(run_id, topic, k):
    return {
        "run_id": run_id,
        "topic": topic,
        "k": k,
        "status": "complete",
        "message": "OFFLINE POSTGRESQL TEST FIXTURE; no real research was performed.",
        "trial_mode": "offline_fixture",
        "failed_workers": [],
        "developments": [
            {
                "id": f"offline-pg-fixture-{index}",
                "rank": index,
                "bucket": "new",
                "stale": False,
                "title": f"Offline PostgreSQL fixture {index}",
                "summary": "A synthetic persistence test record.",
                "claims": [
                    {
                        "claim_id": f"offline-claim-{index}",
                        "text": "Synthetic claim used only to check JSON persistence.",
                        "source_url": "https://example.org/offline-fixture",
                        "verdict": "unverified",
                        "evidence": None,
                    }
                ],
            }
            for index in range(1, k + 1)
        ],
        "dropped": [],
        "ledger": {"offline-request": {"worker": "researcher", "charged": {"steps": 0}}},
        "charged_usage": {"steps": 0, "fetches": 0, "tokens": 0},
        "completed_at": datetime.now(UTC).isoformat(),
    }


def run_trial(target):
    """Use real app/ORM/auth behavior and clean up only this trial's accounts and runs."""
    target = validate_target(target.render_as_string(hide_password=False))
    sys.path.insert(0, str(ROOT))
    if "orchestrator.db" in sys.modules:
        raise RuntimeError("Run the PostgreSQL trial in a fresh Python process")
    trial_root = ROOT / ".state" / f"postgresql-trial-{target.port}"
    os.environ["DATABASE_URL"] = target.render_as_string(hide_password=False)
    os.environ["JWT_SECRET"] = secrets.token_urlsafe(32)
    os.environ["TRACE_DIR"] = str(trial_root / "traces")
    os.environ["LOG_DIR"] = str(trial_root / "traces")

    # Settings must see the validated target before SQLAlchemy constructs its engine.
    from orchestrator import routes
    from orchestrator.app import app
    from orchestrator.db import Base, SessionLocal, engine
    from orchestrator.models import Run, User
    from orchestrator.seed import seed

    started = datetime.now(UTC)
    clock = time.monotonic()
    checks = []
    created_users = []
    observed_runs = []
    original_orchestrate = routes.orchestrate
    original_root = routes.ROOT

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "status": "passed"})

    async def fake_orchestrate(run_id, topic, k, previous, policy):
        observed_runs.append(run_id)
        return fixture_snapshot(run_id, topic, k)

    try:
        with engine.connect() as connection:
            database = connection.execute(
                text("select version(), current_database(), current_user, inet_server_port()")
            ).one()
        check(
            "isolated_database_and_port",
            database[1:3] == ("lab2", "lab2") and target.port in {5434, 5435},
        )
        Base.metadata.create_all(engine)
        # Lifespan recovery changes running rows, so refuse a database with active work.
        with SessionLocal() as db:
            if db.query(Run).filter_by(status="running").count():
                raise RuntimeError("The selected database contains an active run; trial refused")
            prior_users = {user.id for user in db.query(User).all()}
            prior_runs = {run.id: run.snapshot for run in db.query(Run).all()}
        seed()
        seed()
        with SessionLocal() as db:
            graders = db.query(User).filter_by(username="NYUgrader").all()
            check("idempotent_seed_one_grader", len(graders) == 1)
            check("argon2id_hash_persisted", graders[0].password_hash.startswith("$argon2id$"))

        routes.orchestrate = fake_orchestrate
        routes.ROOT = trial_root
        suffix = uuid4().hex[:8]
        password = secrets.token_urlsafe(24)
        headers = {}
        with TestClient(app) as client:
            check("health_endpoint", client.get("/healthz").json() == {"status": "ok"})
            check("missing_bearer_rejected", client.get("/api/tracker/runs").status_code == 401)
            check(
                "malformed_bearer_rejected",
                client.get(
                    "/api/tracker/runs", headers={"Authorization": "Bearer malformed"}
                ).status_code
                == 401,
            )
            check(
                "seeded_grader_login",
                client.post(
                    "/api/auth/login", json={"username": "NYUgrader", "password": "Courant2026!"}
                ).status_code
                == 200,
            )
            for label in ("owner", "other"):
                username = f"pgtrial-{label}-{suffix}"
                response = client.post(
                    "/api/auth/register", json={"username": username, "password": password}
                )
                check(f"register_{label}", response.status_code == 201)
                check(f"password_hash_not_returned_{label}", "password_hash" not in response.json())
                created_users.append(response.json()["id"])
                login = client.post(
                    "/api/auth/login", json={"username": username, "password": password}
                )
                check(f"login_{label}", login.status_code == 200)
                headers[label] = {"Authorization": "Bearer " + login.json()["token"]}
            owner, other = created_users
            check(
                "jwt_auth_me",
                client.get("/api/auth/me", headers=headers["owner"]).json()["id"] == owner,
            )
            check(
                "user_ownership_returns_404",
                client.get(f"/api/users/{owner}", headers=headers["other"]).status_code == 404,
            )
            launch = client.post(
                "/api/tracker/run-now",
                json={"topic": "Offline PostgreSQL persistence trial", "k": 3},
                headers=headers["owner"],
            )
            check("run_now_accepted", launch.status_code == 202)
            run_id = launch.json()["id"]
            for _ in range(100):
                response = client.get(f"/api/tracker/runs/{run_id}", headers=headers["owner"])
                if response.json()["status"] != "running":
                    break
                time.sleep(0.01)
            data = response.json()
            check("background_run_saved_complete", data["status"] == "complete")
            check(
                "nested_json_snapshot_roundtrip",
                data["snapshot"]["developments"][1]["claims"][0]["evidence"] is None
                and data["snapshot"]["ledger"]["offline-request"]["charged"]["steps"] == 0,
            )
            check(
                "duration_and_timestamps",
                data["duration_seconds"] >= 0 and data["completed_at"] is not None,
            )
            check("markdown_saved", "OFFLINE POSTGRESQL TEST FIXTURE" in data["report_markdown"])
            check(
                "run_ownership_returns_404",
                client.get(f"/api/tracker/runs/{run_id}", headers=headers["other"]).status_code
                == 404,
            )
            check(
                "other_user_history_empty",
                client.get("/api/tracker/runs", headers=headers["other"]).json() == [],
            )
            saved_snapshot = data["snapshot"]
            with SessionLocal() as db:
                persisted = db.get(Run, run_id)
                check("separate_session_persistence", persisted.snapshot == saved_snapshot)
                check(
                    "postgres_timestamps_timezone_aware",
                    persisted.started_at.tzinfo is not None
                    and persisted.completed_at.tzinfo is not None,
                )
                interrupted_id = str(uuid4())
                db.add(
                    Run(
                        id=interrupted_id,
                        user_id=owner,
                        started_at=datetime.now(UTC) - timedelta(seconds=5),
                        status="running",
                        snapshot={"topic": "Offline interrupted trial", "k": 3},
                    )
                )
                db.commit()

        with TestClient(app) as client:
            history = client.get(f"/api/tracker/runs/{run_id}", headers=headers["owner"]).json()
            check("completed_snapshot_survives_restart", history["snapshot"] == saved_snapshot)
            recovered = client.get(
                f"/api/tracker/runs/{interrupted_id}", headers=headers["owner"]
            ).json()
            check(
                "running_snapshot_settles_on_restart",
                recovered["status"] == "partial"
                and recovered["snapshot"]["failed_workers"][-1]["reason"] == "orchestrator_restart",
            )
            check("recovered_duration_persisted", recovered["duration_seconds"] >= 5)
            check(
                "account_delete",
                client.delete(f"/api/users/{owner}", headers=headers["owner"]).status_code == 204,
            )
            with SessionLocal() as db:
                check(
                    "postgres_foreign_key_cascade",
                    db.query(Run).filter_by(user_id=owner).count() == 0,
                )
            check(
                "deleted_account_token_rejected",
                client.get("/api/auth/me", headers=headers["owner"]).status_code == 401,
            )

        check("only_mock_orchestration_used", observed_runs == [run_id])
    finally:
        routes.orchestrate = original_orchestrate
        routes.ROOT = original_root
        with SessionLocal() as db:
            if created_users:
                db.execute(delete(User).where(User.id.in_(created_users)))
                db.commit()

    with SessionLocal() as db:
        remaining_users = {user.id for user in db.query(User).all()}
        remaining_runs = {run.id: run.snapshot for run in db.query(Run).all()}
        check(
            "trial_users_and_runs_removed",
            not remaining_users.intersection(created_users)
            and prior_users <= remaining_users
            and remaining_runs == prior_runs,
        )

    return {
        "status": "passed",
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - clock, 6),
        "postgres_version": database[0],
        "python_version": sys.version.split()[0],
        "database_url": target.render_as_string(hide_password=True),
        "client_port": target.port,
        "server_port": database[3],
        "provider_calls": 0,
        "fixture_mode": "offline synthetic worker results; not research evidence",
        "checks_passed": len(checks),
        "checks": checks,
        "remaining_data": "Seeded grader and pre-existing records preserved; trial records removed",
        "existing_app_restarted": False,
        "other_databases_modified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--database-url", help="Overrides DATABASE_URL from the dotenv file")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "postgresql-trial.json")
    args = parser.parse_args()
    value = args.database_url or dotenv_values(args.env_file).get("DATABASE_URL")
    if not value:
        parser.error("Set DATABASE_URL in the dotenv file or pass --database-url")
    try:
        target = validate_target(value)
    except ValueError as exc:
        parser.error(str(exc))
    result = run_trial(target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
