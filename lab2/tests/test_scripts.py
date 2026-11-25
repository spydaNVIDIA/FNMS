"""Regression checks for launcher secret isolation and non-destructive PID ownership."""

import argparse

from dotenv import dotenv_values

from scripts import manage


def test_verifier_receives_no_database_or_other_service_credentials():
    config = {
        "DATABASE_URL": "postgresql://private",
        "JWT_SECRET": "jwt-secret",
        "RESEARCHER_TOKEN": "research-secret",
        "VERIFIER_TOKEN": "verify-secret",
        "TAVILY_API_KEY": "search-secret",
        "MODEL_API_KEY": "model-secret",
    }
    inherited = {"PATH": "/usr/bin", **config, "UNRELATED_SECRET": "must-not-leak"}
    actual = manage.service_environment("verifier", config, inherited)
    assert actual["MODEL_API_KEY"] == "model-secret"
    assert actual["VERIFIER_TOKEN"] == "verify-secret"
    assert not (
        {"DATABASE_URL", "JWT_SECRET", "RESEARCHER_TOKEN", "TAVILY_API_KEY", "UNRELATED_SECRET"}
        & actual.keys()
    )
    assert actual["LOG_DIR"] == actual["TRACE_DIR"]


def test_researcher_receives_no_database_or_verifier_token():
    config = {
        "DATABASE_URL": "postgresql://private",
        "JWT_SECRET": "jwt-secret",
        "RESEARCHER_TOKEN": "research-secret",
        "VERIFIER_TOKEN": "verify-secret",
        "TAVILY_API_KEY": "search-secret",
        "MODEL_API_KEY": "model-secret",
    }
    actual = manage.service_environment("researcher", config, {})
    assert actual["RESEARCHER_TOKEN"] == "research-secret"
    assert actual["TAVILY_API_KEY"] == "search-secret"
    assert not ({"DATABASE_URL", "JWT_SECRET", "VERIFIER_TOKEN"} & actual.keys())


def test_orchestrator_receives_no_provider_keys():
    config = {
        "DATABASE_URL": "postgresql://local",
        "MODEL_API_KEY": "secret",
        "TAVILY_API_KEY": "secret",
        "LAB2_CONFIG": "/tmp/policy.yaml",
    }
    actual = manage.service_environment("orchestrator", config, {})
    assert actual["DATABASE_URL"] == "postgresql://local"
    assert actual["LAB2_CONFIG"] == "/tmp/policy.yaml"
    assert "MODEL_API_KEY" not in actual
    assert "TAVILY_API_KEY" not in actual


def test_launcher_reads_io_caps_from_selected_config(tmp_path, monkeypatch):
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    selected = tmp_path / "selected.yaml"
    (tmp_path / ".env").write_text(f"LAB2_CONFIG={selected}\n")
    selected.write_text(
        "model: {max_output_tokens: 111}\n"
        "search: {max_calls: 2, max_results: 3}\n"
        "fetch: {max_redirects: 1, max_bytes: 1024, timeout_seconds: 4}\n"
    )
    keys = {
        "MODEL_MAX_OUTPUT_TOKENS": "111",
        "SEARCH_MAX_RESULTS": "3",
        "FETCH_TIMEOUT_SECONDS": "4",
        "SEARCH_MAX_CALLS": "2",
        "FETCH_MAX_REDIRECTS": "1",
        "FETCH_MAX_BYTES": "1024",
    }
    for key in [*keys, "LAB2_CONFIG"]:
        monkeypatch.delenv(key, raising=False)
    values = manage.configuration()
    researcher = manage.service_environment("researcher", values, {})
    assert all(researcher[key] == value for key, value in keys.items())
    verifier = manage.service_environment("verifier", values, {})
    assert verifier["MODEL_MAX_OUTPUT_TOKENS"] == "111"
    assert verifier["FETCH_TIMEOUT_SECONDS"] == "4"
    assert "SEARCH_MAX_RESULTS" not in verifier


def test_pid_record_must_match_command_and_creation_time():
    record = {
        "pid": 123,
        "started": "Mon Nov 23 12:00:00 2026",
        "command": f"{manage.PYTHON} -m uvicorn verifier.app:app --port 8012",
    }
    assert manage.owns_process(record, "verifier", record.copy())
    assert not manage.owns_process(record, "verifier", None)
    assert not manage.owns_process(record, "verifier", {**record, "started": "a newer process"})
    assert not manage.owns_process(record, "researcher", record.copy())
    other = {**record, "command": "/usr/bin/python unrelated_server.py"}
    assert not manage.owns_process(other, "verifier", other.copy())


def test_remote_verifier_is_not_started_locally():
    assert manage.local_services({"VERIFIER_URL": "https://example.onrender.com"}) == [
        "researcher",
        "orchestrator",
    ]


def test_macos_resolved_python_still_belongs_to_explicit_project():
    identity = {
        "pid": 123,
        "started": "Wed Nov 25 00:01:18 2026",
        "command": f"/opt/homebrew/Python.app/Python -m uvicorn verifier.app:app --app-dir {manage.ROOT} --port 8012",
    }
    assert manage.owns_process(identity.copy(), "verifier", identity)
    before_exec = {
        **identity,
        "command": identity["command"].replace(
            "/opt/homebrew/Python.app/Python", str(manage.PYTHON)
        ),
    }
    assert manage.owns_process(before_exec, "verifier", identity)
    other = {
        **identity,
        "command": identity["command"].replace(str(manage.ROOT), "/another/project"),
    }
    assert not manage.owns_process(other.copy(), "verifier", other)
    assert manage.local_services({"VERIFIER_URL": "http://127.0.0.1:8012"}) == [
        "researcher",
        "verifier",
        "orchestrator",
    ]


def test_setup_imports_only_provider_keys_and_preserves_existing_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "JWT_SECRET=\nRESEARCHER_TOKEN=\nVERIFIER_TOKEN=\nMODEL_API_KEY=\nTAVILY_API_KEY=\nLIVE_CALLS_APPROVED=false\n"
    )
    source = tmp_path / "prior.env"
    source.write_text(
        "MODEL_API_KEY=old-model-key\nTAVILY_API_KEY=old-search-key\nDATABASE_URL=do-not-copy\nJWT_SECRET=do-not-copy\n"
    )
    args = argparse.Namespace(reuse_provider_env=str(source), approve_free_tier=True)
    manage.configure(args)
    values = dotenv_values(tmp_path / ".env")
    assert values["MODEL_API_KEY"] == "old-model-key"
    assert values["TAVILY_API_KEY"] == "old-search-key"
    assert "DATABASE_URL" not in values
    assert values["JWT_SECRET"] != "do-not-copy"
    assert values["LIVE_CALLS_APPROVED"] == "true"
    assert len({values[key] for key in ["JWT_SECRET", "RESEARCHER_TOKEN", "VERIFIER_TOKEN"]}) == 3
    manage.configure(argparse.Namespace(reuse_provider_env=None, approve_free_tier=False))
    assert dotenv_values(tmp_path / ".env") == values
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_shell_entrypoints_are_executable_and_independent_of_working_directory():
    for name in ["setup", "start", "stop", "status", "run", "export", "evaluate"]:
        script = manage.ROOT / "scripts" / f"{name}.sh"
        assert script.stat().st_mode & 0o111
        assert 'dirname -- "$0"' in script.read_text()


def test_render_blueprint_does_not_deploy_database_or_researcher():
    import yaml

    blueprint = yaml.safe_load((manage.ROOT / "render.yaml").read_text())
    assert len(blueprint["services"]) == 1
    service = blueprint["services"][0]
    assert service["plan"] == "free"
    assert service["rootDir"] == "lab2"
    assert "verifier.app:app" in service["startCommand"]
    names = {entry["key"] for entry in service["envVars"]}
    assert "DATABASE_URL" not in names
    assert "RESEARCHER_TOKEN" not in names
    assert {"VERIFIER_TOKEN", "MODEL_API_KEY", "LIVE_CALLS_APPROVED"} <= names
