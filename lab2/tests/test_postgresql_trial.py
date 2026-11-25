"""Target guards run without importing the app or opening a database connection."""

import pytest

from tests.postgresql_trial import validate_target


@pytest.mark.parametrize("port", [5434, 5435])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_accepts_only_the_local_lab2_database_ports(port, host):
    target = validate_target(f"postgresql+psycopg://lab2@{host}:{port}/lab2")
    assert target.port == port
    assert target.database == target.username == "lab2"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://lab2@127.0.0.1:5432/lab2",
        "postgresql+psycopg://lab2@127.0.0.1/lab2",
        "postgresql+psycopg://lab2@db.example.org:5434/lab2",
        "postgresql+psycopg://lab1b@127.0.0.1:5434/lab2",
        "postgresql+psycopg://lab2@127.0.0.1:5434/lab1b",
        "postgresql+psycopg://lab2@127.0.0.1:5434/lab2?host=db.example.org",
        "postgresql+psycopg://lab2@127.0.0.1:5434/lab2?port=5432",
        "postgresql+psycopg://lab2@127.0.0.1:5434/lab2?service=other",
        "sqlite:///lab2.db",
        "not-a-database-url",
    ],
)
def test_rejects_other_databases_and_connection_overrides(url):
    with pytest.raises(ValueError):
        validate_target(url)
