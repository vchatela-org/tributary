import time
from datetime import timedelta

import pytest

from app import connectors, schedule, scheduler
from app.config import settings
from app.connectors.base import Connector
from app.models import SyncRun, utcnow


class Ticker(Connector):
    """A connector that is configured, runs daily at 03:10."""
    key = "ticker"
    label = "Ticker"
    category = "Productivity"
    default_schedule = "10 3 * * *"
    required_env = ()

    def __init__(self, configured=True):
        super().__init__()
        self.configured = configured

    def api_configured(self):
        return self.configured


@pytest.fixture()
def ticker():
    connector = connectors.register(Ticker())
    yield connector
    connectors.unregister("ticker")


def test_off_without_configuration():
    assert scheduler.configured() == []
    assert scheduler.request("audiobookshelf") is False
    assert scheduler.request("unknown") is False


def test_the_first_run_waits_never_at_startup(ticker, monkeypatch):
    monkeypatch.setattr(settings, "first_run_delay", 60.0)
    due = {}
    key, timeout = scheduler._next(due, time.monotonic() + settings.first_run_delay)
    assert key == "ticker" and 59 < timeout <= 60


def test_unconfigured_connectors_are_not_scheduled(ticker):
    ticker.configured = False
    assert scheduler._next({}, time.monotonic()) == (None, None)


def test_a_fresh_connector_runs_once_after_the_start_delay(ticker, db):
    """A fresh install sees data shortly after it starts, never at the very
    start (a slow first sync must not hold up the liveness probe)."""
    db.query(SyncRun).delete()
    db.commit()
    assert scheduler.plan(db, ticker, 100.0, 60.0) == 160.0


def test_a_scheduled_run_lands_on_the_next_occurrence_not_a_countdown(ticker, db):
    """The schedule is a time of day: a run does not push tomorrow's run away by
    a day the way an interval did."""
    db.add(SyncRun(connector="ticker", trigger="schedule", status="ok", started_at=utcnow()))
    db.commit()
    following = schedule.to_utc(schedule.next_run_after("10 3 * * *", "Europe/Paris"))
    deadline = scheduler.plan(db, ticker, 0.0, 60.0)
    assert deadline is not None and abs(deadline - (following - utcnow()).total_seconds()) < 1


def test_a_missed_run_happens_once_after_the_delay_never_several_times(ticker, db):
    """The app was down when it should have run: one catch-up, then the cron."""
    db.add(SyncRun(connector="ticker", trigger="schedule", status="ok",
                   started_at=utcnow() - timedelta(days=5)))
    db.commit()
    assert scheduler.plan(db, ticker, 100.0, 60.0) == 160.0


def test_a_manual_run_does_not_move_the_deadline(ticker, db):
    """Only a run that stands for the scheduled one re-plans; a check, an import
    or "Run now" leaves the time of day alone."""
    due = {"ticker": 0.0}
    scheduler._after_run(db, "ticker", (("dry_run", True), ("trigger", "manual")), due)
    scheduler._after_run(db, "ticker", (("file", "/tmp/x.csv"), ("trigger", "upload")), due)
    scheduler._after_run(db, "ticker", (("trigger", "manual"),), due)
    assert due == {"ticker": 0.0}


def test_a_schedule_a_connector_set_to_manual_only_is_not_scheduled(ticker, db):
    from app.models import ConnectorSchedule

    db.add(ConnectorSchedule(connector="ticker", cron=""))
    db.commit()
    assert scheduler.plan(db, ticker, 100.0, 60.0) is None
    assert scheduler.scheduled(db, ticker) is None


def test_requests_are_queued_once(ticker, no_thread):
    assert scheduler.request("ticker", trigger="manual")
    assert scheduler.request("ticker", trigger="manual")
    assert scheduler.request("ticker", trigger="manual", dry_run=True)
    assert scheduler._requests.qsize() == 2
    assert scheduler.has_pending()


def test_a_run_that_raises_does_not_stay_running(ticker, monkeypatch, db):
    def boom(session, connector, **options):
        session.add(SyncRun(connector="ticker", status="running"))
        session.commit()
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.runner.run", boom)
    scheduler._run("ticker", ())
    run = db.query(SyncRun).filter(SyncRun.connector == "ticker").one()
    assert run.status == "error" and "database is locked" in run.message


def test_runs_interrupted_by_a_restart_are_closed(db):
    db.add(SyncRun(connector="audiobookshelf", status="running"))
    db.commit()
    scheduler.close_interrupted_runs()
    db.expire_all()
    run = db.query(SyncRun).one()
    assert run.status == "error" and "restart" in run.message and run.finished_at


def test_the_thread_runs_requests_and_schedules(ticker, monkeypatch, db):
    """End to end, with a real thread: a request runs right away, the
    scheduled run only after the first-run delay (a dry run doesn't stand
    for it)."""
    ran = []
    monkeypatch.setattr("app.runner.run", lambda session, connector, **options: ran.append(
        (time.monotonic(), options)) or SyncRun(connector=connector.key, status="ok", message=""))
    monkeypatch.setattr(settings, "first_run_delay", 0.6)
    started = time.monotonic()
    try:
        assert scheduler.request("ticker", trigger="manual", dry_run=True)
        deadline = time.monotonic() + 5
        while len(ran) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        scheduler.stop()
    (first_at, first), (second_at, second) = ran
    assert first == {"trigger": "manual", "dry_run": True} and first_at - started < 0.5
    assert second == {} and second_at - started >= 0.6
