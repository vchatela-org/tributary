"""In-process scheduler: one daemon thread runs one connector at a time, on
its schedule or when asked ("Run now", a check, an import, the API).
Startup never waits for it: the server listens right away, and the first
scheduled run comes TRIBUTARY_FIRST_RUN_DELAY seconds later, never at startup
itself (a slow first sync must not hold up a liveness probe).

A schedule is a cron expression in TRIBUTARY_TZ (app/schedule.py), so a run
lands at a time of day rather than at "N minutes after the start": a restart
no longer drifts the schedule. A run that was missed while the app was down is
run **once**, after the same start delay -- never several catch-ups.

Assumes a single process (one uvicorn worker on one SQLite file): a second
one would run every job a second time.

Every run is recorded as a SyncRun (app/runner.py).
"""
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app import connectors, runner, schedule
from app.config import settings
from app.database import SessionLocal
from app.models import ConnectorSchedule, SyncRun, utcnow

logger = logging.getLogger(__name__)

# What a request may ask for (app/runner.run's options).
OPTIONS = ("dry_run", "full", "file", "filename", "trigger", "promoted_from")

# A message to the thread: replan a connector ("__replan__", (key,)) after the
# UI saved another schedule; None stops it.
_REPLAN = "__replan__"

_requests: "queue.Queue[Tuple[str, Tuple]]" = queue.Queue()
_pending: set = set()
_pending_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_cleaned = False


def stored_crons(db: Session) -> Dict[str, str]:
    """What the UI saved, per connector (app/schedule.py resolves it against
    the environment and the connector's default)."""
    return {row.connector: row.cron for row in db.query(ConnectorSchedule).all()}


def resolved_for(db: Session, connector) -> schedule.Resolved:
    return schedule.resolve(connector, stored_crons(db).get(connector.key), settings.tz)


def scheduled(db: Session, connector) -> Optional[schedule.Resolved]:
    """The schedule this connector runs on, or None when it has none: no API
    and no watched folder (an upload-only source), or *Manual only*."""
    resolved = resolved_for(db, connector)
    return resolved if resolved.scheduled else None


def configured() -> list:
    """The connectors that run on a schedule, for the startup log."""
    with SessionLocal() as db:
        return [c for c in connectors.all_connectors() if scheduled(db, c) is not None]


def describe_all(db: Session) -> List[str]:
    """'Audiobookshelf (Daily at 03:10, from the connector's default)' per
    scheduled connector, for the startup log."""
    out = []
    for connector in connectors.all_connectors():
        resolved = scheduled(db, connector)
        if resolved is not None:
            out.append(f"{connector.label} ({resolved.label}, {resolved.source_label})")
    return out


def start() -> bool:
    """Start the thread if any connector runs on a schedule. Returns whether
    it runs. Never blocks."""
    global _cleaned
    if not configured():
        if not _cleaned:
            # A run left "running" by a previous process must not show as
            # running forever, and a checked upload past its TTL must not stay
            # importable. A thread of its own, never holding up the start.
            _cleaned = True
            threading.Thread(target=_startup_cleanup, name="scheduler-cleanup", daemon=True).start()
        logger.info("Scheduler: no connector configured, not started")
        return False
    if _ensure_thread():
        with SessionLocal() as db:
            logger.info("Scheduler started: %s", ", ".join(describe_all(db)))
    return True


def _startup_cleanup() -> None:
    close_interrupted_runs()
    try:
        with SessionLocal() as db:
            dropped = runner.purge_staged(db)
            db.commit()
            if dropped:
                logger.info("Scheduler: %d expired checked file(s) removed", dropped)
    except Exception:
        logger.exception("Scheduler: could not purge the checked files")


def request(key: str, **options: Any) -> bool:
    """Queue a run now (after the current one, if any). A run already queued
    with the same options is not queued twice. False when the connector is
    unknown, or not configured and nothing to run (no file, nothing checked)."""
    connector = connectors.get(key)
    if connector is None or not (connector.is_configured() or options.get("file")
                                 or options.get("promoted_from") is not None):
        return False
    item = (key, tuple(sorted((k, v) for k, v in options.items() if k in OPTIONS and v not in (None, False))))
    with _pending_lock:
        if item in _pending:
            return True
        _pending.add(item)
    _requests.put(item)
    _ensure_thread()
    return True


def reschedule(key: str) -> None:
    """The UI saved a schedule for `key`: drop its deadline so the thread plans
    it again (and wake the thread)."""
    _requests.put((_REPLAN, (key,)))


def _ensure_thread() -> bool:
    """Start the thread unless it runs (a run to a connector without a schedule
    needs it too). Returns whether it was started now."""
    global _thread
    # Two requests at once (the server's thread pool) must not start two.
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
        _thread.start()
        return True


def close_interrupted_runs(key: Optional[str] = None,
                           message: str = "Interrupted by a restart of the application.") -> None:
    """A run still "running" at startup died with the previous process; one
    of `key` still "running" after its run raised could not record its end."""
    try:
        with SessionLocal() as db:
            stale = db.query(SyncRun).filter(SyncRun.status == "running")
            if key:
                stale = stale.filter(SyncRun.connector == key)
            rows = stale.all()
            for row in rows:
                row.status, row.message = "error", message
                row.finished_at = row.finished_at or utcnow()
            if rows:
                db.commit()
    except Exception:
        logger.exception("Scheduler: could not close interrupted runs")


# ────────────────────────────────────────────────
# Planning: when each connector is due
# ────────────────────────────────────────────────

def _last_scheduled(db: Session, key: str) -> Optional[datetime]:
    """When this connector last ran on its schedule (naive UTC), whatever the
    outcome: a run that failed counts, it is not replayed on top of the next
    occurrence."""
    row = (db.query(SyncRun)
           .filter(SyncRun.connector == key, SyncRun.trigger == "schedule")
           .order_by(SyncRun.started_at.desc()).first())
    return row.started_at if row is not None else None


def plan(db: Session, connector, mono_now: float, first_delay: float,
         now: Optional[datetime] = None) -> Optional[float]:
    """The monotonic deadline for one connector, or None when it has no
    schedule.

    A connector that never ran is due after `first_delay` (a fresh install sees
    data shortly after it starts, never at the start itself). Otherwise the next
    occurrence after its last scheduled run decides: in the past -- it was
    missed while the app was down -- it runs **once** after `first_delay`; in
    the future, it waits until then.
    """
    resolved = scheduled(db, connector)
    if resolved is None:
        return None
    now = now or utcnow()
    last = _last_scheduled(db, connector.key)
    if last is None:
        return mono_now + max(0.0, first_delay)
    following = schedule.to_utc(schedule.next_run_after(resolved.cron, settings.tz, after=last))
    if following is None:
        return None
    if following <= now:
        logger.info("Scheduler: %s missed its %s run, running it once now",
                    connector.label, following.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"))
        return mono_now + max(0.0, first_delay)
    return mono_now + (following - now).total_seconds()


def _replan(db: Session, due: Dict[str, Optional[float]], key: Optional[str] = None) -> None:
    """(Re)compute the deadlines: every connector, or just one."""
    mono = time.monotonic()
    first = max(0.0, settings.first_run_delay)
    for connector in connectors.all_connectors():
        if key is not None and connector.key != key:
            continue
        due[connector.key] = plan(db, connector, mono, first)


def _run(key: str, options: Tuple) -> None:
    connector = connectors.get(key)
    opts = dict(options)
    if connector is None or not (connector.is_configured() or opts.get("file")
                                 or opts.get("promoted_from") is not None):
        return
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            record = runner.run(db, connector, **opts)
            # Read while the session is open: attributes expire on commit.
            logger.info("Scheduler: %s %s in %.1fs: %s", connector.label, record.status,
                        time.monotonic() - started, record.message)
    except Exception as exc:
        logger.exception("Scheduler: %s failed", connector.label)
        close_interrupted_runs(key, f"Unexpected error: {type(exc).__name__}: {exc}"[:2000])


def _after_run(db: Session, key: str, options: Tuple, due: Dict[str, Optional[float]]) -> None:
    """A run that stands for the scheduled one moves the deadline to the next
    occurrence of the cron. A check, an upload or a manual "Run now" does not:
    the schedule is a time of day, not a countdown (it does not push tomorrow's
    run away)."""
    opts = dict(options)
    if opts.get("dry_run") or opts.get("file") or opts.get("promoted_from") is not None:
        return
    if opts.get("trigger") not in (None, "schedule"):
        return
    connector = connectors.get(key)
    if connector is not None:
        _replan(db, due, key)


def _next(due: Dict[str, Optional[float]], first: float) -> Tuple[Optional[str], Optional[float]]:
    """The connector due next and how long until then (None: nothing
    scheduled, wait for a request). A connector with no deadline yet (registered
    after the start) is planned like the others, at the first-run delay."""
    for connector in connectors.all_connectors():
        due.setdefault(connector.key, None)
    pending = [key for key, at in due.items() if at is None and connectors.get(key) is not None]
    if pending:
        # The cron needs a session; only the connectors without a deadline do
        # (the first pass, and a connector registered after the start).
        mono = time.monotonic()
        try:
            with SessionLocal() as db:
                for key in pending:
                    due[key] = plan(db, connectors.get(key), mono, max(0.0, first - mono))
        except Exception:
            logger.exception("Scheduler: could not plan the new connectors")
    enabled = {key: at for key, at in due.items()
               if at is not None and connectors.get(key) is not None}
    if not enabled:
        return None, None
    key = min(enabled, key=enabled.get)
    return key, max(0.0, enabled[key] - time.monotonic())



def _loop() -> None:
    global _cleaned
    _cleaned = True
    close_interrupted_runs()
    try:
        with SessionLocal() as db:
            dropped = runner.purge_staged(db)
            if dropped:
                logger.info("Scheduler: %d expired checked file(s) removed", dropped)
            db.commit()
    except Exception:
        logger.exception("Scheduler: could not purge the checked files")
    first = time.monotonic() + max(0.0, settings.first_run_delay)
    due: Dict[str, Optional[float]] = {}
    while True:
        next_key, timeout = _next(due, first)
        try:
            key, options = _requests.get(timeout=timeout)
        except queue.Empty:
            key, options = next_key, ()
        if key is None:
            return  # stop()
        if key == _REPLAN:
            with SessionLocal() as db:
                _replan(db, due, options[0] if options else None)
            continue
        with _pending_lock:
            _pending.discard((key, options))
        _run(key, options)
        with SessionLocal() as db:
            _after_run(db, key, options, due)


def stop(timeout: float = 5.0) -> None:
    """End the thread after its current run (tests, shutdown)."""
    global _thread
    with _thread_lock:
        thread, _thread = _thread, None
    if thread is not None and thread.is_alive():
        _requests.put((None, ()))
        thread.join(timeout)


def has_pending() -> bool:
    with _pending_lock:
        return bool(_pending)


def pending_keys() -> set:
    """The connectors with a run queued, so a card can say so instead of the
    request being silently deduplicated."""
    with _pending_lock:
        return {key for key, _options in _pending}


def running(db: Session) -> List[SyncRun]:
    """The runs going on right now, for the pages that say so."""
    return db.query(SyncRun).filter(SyncRun.status == "running").order_by(SyncRun.id.desc()).all()
