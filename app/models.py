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
    # The dry run whose checked file this import applied ("Import for real"):
    # the two stay distinguishable in the history, and the import's counts are
    # read next to the diff the user saw (app/runner.py).
    promoted_from = Column(Integer, nullable=True)


class Cursor(Base):
    """Where a connector's last successful run stopped (its own JSON, e.g.
    the newest update it has seen), so the next one only reads what's new."""
    __tablename__ = "cursors"

    connector = Column(String(50), primary_key=True)
    value_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ConnectorSchedule(Base):
    """When a connector runs, as set in the UI: a 5-field cron expression in
    TRIBUTARY_TZ. Absent: the environment or the connector's default decides
    (app/schedule.py, which also says where the value came from)."""
    __tablename__ = "connector_schedules"

    connector = Column(String(50), primary_key=True)
    cron = Column(String(120), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class StagedUpload(Base):
    """An uploaded export that was *checked* (a dry run) and kept, so "Import
    for real" does not upload it a second time.

    The file stays in the data dir for `staged_upload_ttl_hours` and is deleted
    on the import, on discard, or when it expires. Everything needed to refuse
    a stale promote is here: the hashes must still match, or the row is dropped
    and the user is asked to check the file again (app/runner.py).
    """
    __tablename__ = "staged_uploads"

    connector = Column(String(50), primary_key=True)
    # Absolute path in the data dir; the file is this row's only owner.
    path = Column(Text, nullable=False)
    # The name the run shows (a connector may mask what is personal in it).
    filename = Column(String(255), nullable=False)
    # sha256 of the file's bytes: a replaced file invalidates the check.
    file_sha256 = Column(String(64), nullable=False)
    # sha256 of the line protocol the check produced: the import writes exactly
    # this, or it is refused. This is what binds the button to what was
    # reviewed, whatever changed in between (the file, the settings, the
    # readings already in InfluxDB).
    points_sha256 = Column(String(64), nullable=False)
    # sha256 of the settings the import read (Connector.import_fingerprint):
    # told apart from a changed file, for the message.
    config_sha256 = Column(String(64), nullable=False)
    # The `user` a span replace would be scoped to (None: the whole series).
    user = Column(String(255), nullable=True)
    # The dry run this check recorded, for the run history's "promoted from".
    dry_run_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Preference(Base):
    """One UI preference of this install, as JSON. Never a runtime setting: a
    hidden connector keeps running and keeps filling its series, it is only
    left out of the overview (app/views.py, the settings page)."""
    __tablename__ = "preferences"

    key = Column(String(60), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class OAuthToken(Base):
    """What an OAuth connector keeps of its grant (app/oauth.py): the access and
    refresh tokens, sealed together (app/secretbox.py, bound to the connector's
    key), never in plain text. Separate from the cursors on purpose: a cursor is
    saved only after a run that wrote everything and forgotten by a full resync,
    while a refresh token that rotates must be saved the moment it arrives --
    in a dry run, and in a run that fails afterwards -- or the one kept is
    already dead."""
    __tablename__ = "oauth_tokens"

    connector = Column(String(50), primary_key=True)
    # secretbox.seal of {"access_token", "refresh_token"} as JSON.
    ciphertext = Column(Text, nullable=False)
    # When the access token expires (naive UTC), and the scopes granted: not
    # secrets, so the page can show them without opening the box.
    expires_at = Column(DateTime, nullable=True)
    scope = Column(String(255), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class OAuthState(Base):
    """A `state` sent to an authorization page, waiting for the browser to come
    back with it: single use, 10 minutes. Only its SHA-256 is kept, so the
    database alone can't complete someone's authorization."""
    __tablename__ = "oauth_states"

    state_sha256 = Column(String(64), primary_key=True)
    connector = Column(String(50), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
