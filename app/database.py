"""Tributary's own state: run history and connector cursors, in SQLite, and
an OAuth connector's tokens, encrypted (app/oauth.py). The measurements
themselves live in InfluxDB, and the source (the app, or the user's export
files) stays the source of truth."""
import logging
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

if settings.db_url.startswith("sqlite:///"):
    Path(settings.db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.db_url,
    # The scheduler thread and the request handlers share the engine.
    # timeout: wait on a lock rather than fail with "database is locked"
    # (SQLite on network storage can be slow to release one).
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """WAL: a page load reads while a run writes its outcome."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# A column a later version added to an existing table: `create_all` adds a
# missing *table*, never a missing *column* of one already there, so an install
# from before it needs the ALTER, or the first query that names the column breaks.
# (A new table, like the OAuth ones, needs nothing here: create_all makes it.)
_ADDED_COLUMNS = (
    ("sync_runs", "promoted_from", "INTEGER"),
)


def _ensure_columns(connection) -> None:
    """Add the columns a later version introduced, idempotently (SQLite)."""
    for table, column, sql_type in _ADDED_COLUMNS:
        present = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
        if present and column not in present:
            logger.info("Database: adding %s.%s to an existing install", table, column)
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def init_db() -> None:
    from app import models  # noqa: F401 - registers the tables
    Base.metadata.create_all(bind=engine)
    if settings.db_url.startswith("sqlite:///"):
        with engine.begin() as connection:
            _ensure_columns(connection)


def uploads_dir() -> Path:
    path = Path(settings.data_dir) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path
