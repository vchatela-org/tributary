from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.database import Base


def utcnow() -> datetime:
    """Naive UTC, as SQLite stores it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SyncRun(Base):
    """One run of a connector (app/runner.py), whatever its outcome: what
    the UI's history shows."""
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    connector = Column(String(50), nullable=False, index=True)
    # schedule / manual / upload
    trigger = Column(String(20), nullable=False, default="schedule")
    dry_run = Column(Boolean, nullable=False, default=False)
    # A full run ignores the cursor: everything the source has, rewritten.
    full = Column(Boolean, nullable=False, default=False)
    started_at = Column(DateTime, nullable=False, default=utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")  # running / ok / error
    message = Column(Text, nullable=True)
    points = Column(Integer, nullable=True)  # written, or that a dry run would write
    stats_json = Column(Text, nullable=True)  # {database: points}
    # Dry run: the line protocol it would have written (capped).
    preview = Column(Text, nullable=True)


class Cursor(Base):
    """Where a connector's last successful run stopped (its own JSON, e.g.
    the newest update it has seen), so the next one only reads what's new."""
    __tablename__ = "cursors"

    connector = Column(String(50), primary_key=True)
    value_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
