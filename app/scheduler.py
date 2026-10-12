"""In-process scheduler: one daemon thread runs one connector at a time, on
its schedule or when asked ("Run now", a dry run, an upload, the API).
Startup never waits for it: the server listens right away, and the first
scheduled run comes TRIBUTARY_FIRST_RUN_DELAY seconds later, never at
startup itself (a slow first sync must not hold up a liveness probe).

Assumes a single process (one uvicorn worker on one SQLite file): a second
one would run every job a second time.

Every run is recorded as a SyncRun (app/runner.py).
"""
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional, Tuple

from app import connectors, runner
from app.config import settings
from app.database import SessionLocal
from app.models import SyncRun, utcnow

logger = logging.getLogger(__name__)

# What a request may ask for (app/runner.run's options).
OPTIONS = ("dry_run", "full", "file", "filename", "trigger")

_requests: "queue.Queue[Tuple[str, Tuple]]" = queue.Queue()
_pending: set = set()
_pending_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_cleaned = False


def configured():
    return [c for c in connectors.all_connectors() if c.is_configured()]


def start() -> bool:
    """Start the thread if any connector runs on a schedule. Returns whether
    it runs. Never blocks."""
    global _cleaned
    if not configured():
        if not _cleaned:
            # A run left "running" by a previous process must not show as
            # running forever. A thread of its own, never holding up the start.
            _cleaned = True
            threading.Thread(target=close_interrupted_runs, name="scheduler-cleanup", daemon=True).start()
        logger.info("Scheduler: no connector configured, not started")
        return False
    if _ensure_thread():
        logger.info("Scheduler started: %s",
                    ", ".join(f"{c.label} ({c.schedule_label()})" for c in configured()))
    return True


def request(key: str, **options: Any) -> bool:
    """Queue a run now (after the current one, if any). A run already queued
    with the same options is not queued twice. False when the connector is
    unknown, or not configured and no file is given."""
    connector = connectors.get(key)
    if connector is None or not (connector.is_configured() or options.get("file")):
        return False
    item = (key, tuple(sorted((k, v) for k, v in options.items() if k in OPTIONS and v not in (None, False))))
    with _pending_lock:
        if item in _pending:
            return True
        _pending.add(item)
    _requests.put(item)
    _ensure_thread()
    return True


def _ensure_thread() -> bool:
    """Start the thread unless it runs (an upload to a connector without a
    schedule needs it too). Returns whether it was started now."""
    global _thread
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


def _run(key: str, options: Tuple) -> None:
    connector = connectors.get(key)
    opts = dict(options)
    if connector is None or not (connector.is_configured() or opts.get("file")):
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


def _after_run(key: str, options: Tuple, due: Dict[str, float]) -> None:
    """A run that stands for the scheduled one (not a dry run, not an
    upload) restarts the connector's timer: "Run now" five minutes before
    the hour makes the hourly run come an hour later, not five minutes."""
    opts = dict(options)
    if opts.get("dry_run") or opts.get("file"):
        return
    connector = connectors.get(key)
    if connector is not None:
        due[key] = time.monotonic() + connector.interval_seconds()


def _next(due: Dict[str, float], first: float) -> Tuple[Optional[str], Optional[float]]:
    """The connector due next and how long until then (None: nothing
    scheduled, wait for a request). A connector configured after the start
    is due at the first run time, as the others were."""
    for connector in connectors.all_connectors():
        due.setdefault(connector.key, first)
    enabled = {key: at for key, at in due.items()
               if connectors.get(key) is not None and connectors.get(key).is_configured()}
    if not enabled:
        return None, None
    key = min(enabled, key=enabled.get)
    return key, max(0.0, enabled[key] - time.monotonic())


def _loop() -> None:
    global _cleaned
    _cleaned = True
    close_interrupted_runs()
    # Never at startup itself: the first scheduled run waits.
    first = time.monotonic() + max(0.0, settings.first_run_delay)
    due: Dict[str, float] = {}
    while True:
        next_key, timeout = _next(due, first)
        try:
            key, options = _requests.get(timeout=timeout)
            if key is None:
                return  # stop()
            with _pending_lock:
                _pending.discard((key, options))
        except queue.Empty:
            key, options = next_key, ()
        _run(key, options)
        _after_run(key, options, due)


def stop(timeout: float = 5.0) -> None:
    """End the thread after its current run (tests, shutdown)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        _requests.put((None, ()))
        _thread.join(timeout)
    _thread = None


def has_pending() -> bool:
    with _pending_lock:
        return bool(_pending)
