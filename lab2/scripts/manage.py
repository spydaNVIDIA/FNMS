"""Local lifecycle management for the three isolated Lab 2 processes.

Only PIDs launched here, with the same command and process creation timestamp,
are stopped. Database volumes and saved reports are never deleted.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
PYTHON = ROOT / ".venv" / "bin" / "python"
SERVICES = {"researcher": 8011, "verifier": 8012, "orchestrator": 8010}
BASE_ENV = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
MODEL_ENV = {
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "MODEL_MIN_INTERVAL_SECONDS",
    "MODEL_MAX_OUTPUT_TOKENS",
    "LIVE_CALLS_APPROVED",
    "FETCH_MAX_REDIRECTS",
    "FETCH_MAX_BYTES",
    "FETCH_TIMEOUT_SECONDS",
}
SERVICE_ENV = {
    "researcher": MODEL_ENV
    | {
        "RESEARCHER_TOKEN",
        "TAVILY_API_KEY",
        "SEARCH_URL",
        "SEARCH_MAX_CALLS",
        "SEARCH_MAX_RESULTS",
    },
    "verifier": MODEL_ENV | {"VERIFIER_TOKEN"},
    "orchestrator": {
        "DATABASE_URL",
        "JWT_SECRET",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "CORS_ORIGINS",
        "RESEARCHER_URL",
        "VERIFIER_URL",
        "RESEARCHER_TOKEN",
        "VERIFIER_TOKEN",
        "LAB2_CONFIG",
    },
}


def configuration() -> dict[str, str]:
    """Read dotenv only in the launcher, never inside workers."""
    from dotenv import dotenv_values
    import yaml

    if not (ROOT / ".env").exists():
        raise RuntimeError("Run ./scripts/setup.sh before starting the application")
    values = {
        key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None
    }
    # Explicit shell values may override local configuration, e.g. a test SQLite DB.
    allowed = set().union(*SERVICE_ENV.values()) | {"TRACKER_USERNAME", "TRACKER_PASSWORD"}
    values.update({key: value for key, value in os.environ.items() if key in allowed})
    policy = yaml.safe_load(Path(values.get("LAB2_CONFIG", str(ROOT / "config.yaml"))).read_text())
    values.setdefault("SEARCH_MAX_CALLS", str(policy["search"]["max_calls"]))
    values.setdefault("SEARCH_MAX_RESULTS", str(policy["search"]["max_results"]))
    values.setdefault("MODEL_MAX_OUTPUT_TOKENS", str(policy["model"]["max_output_tokens"]))
    values.setdefault("FETCH_MAX_REDIRECTS", str(policy["fetch"]["max_redirects"]))
    values.setdefault("FETCH_MAX_BYTES", str(policy["fetch"]["max_bytes"]))
    values.setdefault("FETCH_TIMEOUT_SECONDS", str(policy["fetch"]["timeout_seconds"]))
    return values


def service_environment(service: str, config: dict[str, str], inherited=None) -> dict[str, str]:
    """Give each service only the secrets it actually uses."""
    inherited = os.environ if inherited is None else inherited
    environment = {key: value for key, value in inherited.items() if key in BASE_ENV}
    environment.update({key: value for key, value in config.items() if key in SERVICE_ENV[service]})
    environment.update(
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        LOG_DIR=str(STATE / "traces"),
        TRACE_DIR=str(STATE / "traces"),
    )
    return environment


def process_identity(pid: int) -> dict | None:
    """Read OS process identity without signalling it."""
    try:
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True
        ).strip()
        started = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return None
    return {"pid": pid, "command": command, "started": started} if command else None


def owns_process(record: dict, service: str, identity: dict | None) -> bool:
    """Reject stale/reused PIDs and records for a different program or project."""
    if not identity or any(record.get(key) != identity.get(key) for key in ("pid", "started")):
        return False
    command = identity["command"]
    arguments = shlex.split(command)
    original_arguments = shlex.split(record["command"])
    if arguments[1:] != original_arguments[1:]:
        return False
    # macOS may replace the venv executable with Python.app's real path. Keep
    # an explicit project path in argv so ownership survives that replacement.
    project_matches = str(PYTHON) in arguments or any(
        arguments[index : index + 2] == ["--app-dir", str(ROOT)]
        for index in range(len(arguments) - 1)
    )
    return project_matches and f"-m uvicorn {service}.app:app " in command


def pid_path(service: str) -> Path:
    return STATE / "pids" / f"{service}.json"


def load_record(service: str) -> dict | None:
    try:
        return json.loads(pid_path(service).read_text())
    except (FileNotFoundError, ValueError):
        return None


def port_available(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=1) as response:
            return json.load(response).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def local_services(config: dict) -> list[str]:
    services = ["researcher", "orchestrator"]
    if config.get("VERIFIER_URL", "http://127.0.0.1:8012").rstrip("/") in {
        "http://127.0.0.1:8012",
        "http://localhost:8012",
    }:
        services.insert(1, "verifier")
    return services


def start(config: dict, *, no_database=False) -> None:
    if not no_database and "127.0.0.1:5434/lab2" in config.get("DATABASE_URL", ""):
        subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=ROOT, check=True)
    for service in local_services(config):
        record = load_record(service)
        if record and owns_process(record, service, process_identity(record["pid"])):
            print(f"{service}: already running (PID {record['pid']})")
            continue
        port = SERVICES[service]
        if not port_available(port):
            raise RuntimeError(f"Port {port} is occupied; not replacing the existing process")
        command = [
            str(PYTHON),
            "-m",
            "uvicorn",
            f"{service}.app:app",
            "--app-dir",
            str(ROOT),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--timeout-graceful-shutdown",
            "5",
        ]
        log_path = STATE / "process-logs" / f"{service}.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=service_environment(service, config),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        # Wait briefly for exec so the process record reflects uvicorn, not the launcher.
        identity = None
        for _ in range(30):
            identity = process_identity(process.pid)
            if identity and f"-m uvicorn {service}.app:app " in identity["command"]:
                break
            if process.poll() is not None:
                raise RuntimeError(f"{service} exited; inspect {log_path}")
            time.sleep(0.1)
        if identity is None:
            raise RuntimeError(f"Could not record process identity for {service}")
        pid_path(service).write_text(json.dumps(identity))
        for _ in range(100):
            if healthy(f"http://127.0.0.1:{port}"):
                current_identity = process_identity(process.pid)
                if current_identity is not None:
                    pid_path(service).write_text(json.dumps(current_identity))
                print(f"{service}: ready at http://127.0.0.1:{port}")
                break
            if process.poll() is not None:
                raise RuntimeError(f"{service} exited; inspect {log_path}")
            time.sleep(0.2)
        else:
            raise RuntimeError(f"{service} did not become healthy; inspect {log_path}")
    if "verifier" not in local_services(config):
        print(f"verifier: remote at {config['VERIFIER_URL']} (not warmed by startup)")
    print("Open http://localhost:8010 — seeded login: NYUgrader / Courant2026!")


def stop(config: dict, *, database=False) -> None:
    for service in reversed(SERVICES):
        record = load_record(service)
        if not record:
            continue
        if not owns_process(record, service, process_identity(record["pid"])):
            print(f"{service}: stale PID record; no process was signalled")
            pid_path(service).unlink(missing_ok=True)
            continue
        os.kill(record["pid"], signal.SIGTERM)
        for _ in range(100):
            if not owns_process(record, service, process_identity(record["pid"])):
                break
            time.sleep(0.1)
        else:
            print(f"{service}: still shutting down; retaining PID record (no forced kill)")
            continue
        pid_path(service).unlink(missing_ok=True)
        print(f"{service}: stopped")
    if database:
        subprocess.run(["docker", "compose", "stop", "db"], cwd=ROOT, check=True)
        print("Database stopped; its volume and all data are preserved")


def status(config: dict) -> None:
    for service, port in SERVICES.items():
        if service == "verifier" and service not in local_services(config):
            print(f"{service}: remote {config['VERIFIER_URL']} (not probed, preserving cold start)")
            continue
        record = load_record(service)
        owned = bool(record and owns_process(record, service, process_identity(record["pid"])))
        state = (
            "healthy" if owned and healthy(f"http://127.0.0.1:{port}") else "not running/healthy"
        )
        print(f"{service}: {state}")


def setup(args) -> None:
    if not PYTHON.exists():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    requirements = "requirements.txt" if args.runtime_only else "requirements-dev.txt"
    subprocess.run(
        [str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / requirements)], check=True
    )
    # Re-enter using installed dependencies; no ambient dotenv package is needed.
    subprocess.run(
        [
            str(PYTHON),
            str(Path(__file__).resolve()),
            "configure",
            *(["--reuse-provider-env", args.reuse_provider_env] if args.reuse_provider_env else []),
            *(["--approve-free-tier"] if args.approve_free_tier else []),
        ],
        check=True,
    )


def configure(args) -> None:
    from dotenv import dotenv_values, set_key

    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text((ROOT / ".env.example").read_text())
    env_path.chmod(0o600)
    values = dotenv_values(env_path)
    for key in ["JWT_SECRET", "RESEARCHER_TOKEN", "VERIFIER_TOKEN"]:
        if not values.get(key):
            set_key(env_path, key, secrets.token_urlsafe(32))
    if args.reuse_provider_env:
        source = Path(args.reuse_provider_env).expanduser().resolve()
        if not source.is_file():
            raise RuntimeError("Provider dotenv source does not exist")
        providers = dotenv_values(source)
        for key in ["MODEL_API_KEY", "TAVILY_API_KEY"]:
            if providers.get(key):
                set_key(env_path, key, providers[key])
        print("Imported provider credentials into ignored .env; values were not printed")
    if args.approve_free_tier:
        set_key(env_path, "LIVE_CALLS_APPROVED", "true")
    print("Setup complete. Edit .env if needed, then run ./scripts/start.sh")


@contextlib.contextmanager
def lifecycle_lock():
    STATE.mkdir(exist_ok=True)
    with (STATE / "lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another setup/start/stop command is already running") from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "configure", "start", "stop", "status"])
    parser.add_argument("--reuse-provider-env")
    parser.add_argument("--approve-free-tier", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument(
        "--no-database", action="store_true", help="Use an already running database"
    )
    parser.add_argument("--database", action="store_true", help="Also stop the Lab 2 database")
    args = parser.parse_args()
    try:
        if args.action == "configure":
            configure(args)
            return
        with lifecycle_lock():
            for name in ["pids", "process-logs", "traces", "reports"]:
                (STATE / name).mkdir(exist_ok=True)
            if args.action == "setup":
                setup(args)
            elif args.action == "start":
                start(configuration(), no_database=args.no_database)
            elif args.action == "stop":
                stop(configuration(), database=args.database)
            else:
                status(configuration())
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"Lab 2: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
