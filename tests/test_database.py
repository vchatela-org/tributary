"""The database grows with the app.

`Base.metadata.create_all` adds a missing *table*, never a missing *column* of a
table already there. An install from before a column was introduced needs an
idempotent ALTER at startup, or the first query that names the column breaks
every page and the scheduler with it.
"""
from sqlalchemy import create_engine

from app.database import _ensure_columns

# sync_runs as it was before `promoted_from` (and before the tables task 08 adds).
OLD_SYNC_RUNS = """
CREATE TABLE sync_runs (
    id INTEGER NOT NULL PRIMARY KEY,
    connector VARCHAR(50) NOT NULL,
    trigger VARCHAR(20) NOT NULL,
    dry_run BOOLEAN NOT NULL,
    full BOOLEAN NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    status VARCHAR(20) NOT NULL,
    message TEXT,
    points INTEGER,
    stats_json TEXT,
    preview TEXT
)
"""


def _columns(engine, table="sync_runs"):
    with engine.begin() as connection:
        return {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_an_existing_database_gains_a_later_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(OLD_SYNC_RUNS)
    assert "promoted_from" not in _columns(engine)

    with engine.begin() as connection:
        _ensure_columns(connection)
    assert "promoted_from" in _columns(engine)

    # Idempotent: a second start adds nothing and does not raise.
    with engine.begin() as connection:
        _ensure_columns(connection)
    assert "promoted_from" in _columns(engine)


def test_migrating_leaves_an_untouched_database_alone(tmp_path):
    """No table yet (a fresh install): create_all makes them, and this is a no-op."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with engine.begin() as connection:
        _ensure_columns(connection)
    assert _columns(engine) == set()
