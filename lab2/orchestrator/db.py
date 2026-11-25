"""SQLAlchemy session boundary for the copied A1 app and Lab 2 run snapshots."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

options = (
    {"connect_args": {"check_same_thread": False}}
    if settings.database_url.startswith("sqlite")
    else {}
)
engine = create_engine(settings.database_url, pool_pre_ping=True, **options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
