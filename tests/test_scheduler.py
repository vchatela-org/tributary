import time

import pytest

from app import connectors, scheduler
from app.config import settings
from app.connectors.base import Connector, ConnectorSettings
from app.models import SyncRun


class Ticker(Connector):
    """A connector that is configured and counts its runs."""
    key = "ticker"
    label = "Ticker"
    required_env = ()

    def __init__(self, configured=True):
        super().__init__()
        self.configured = configured
        self.settings = ConnectorSettings(interval_minutes=60)

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


def test_a_run_restarts_the_timer_but_not_a_dry_run_or_an_upload(ticker):
    due = {"ticker": 10.0}
    scheduler._after_run("ticker", (("dry_run", True), ("trigger", "manual")), due)
    scheduler._after_run("ticker", (("file", "/tmp/x.csv"), ("trigger", "upload")), due)
    assert due == {"ticker": 10.0}
    scheduler._after_run("ticker", (("trigger", "manual"),), due)
    assert 3590 < due["ticker"] - time.monotonic() <= 3600
    due["ticker"] = 0.0
    scheduler._after_run("ticker", (), due)  # the scheduled run itself
    assert due["ticker"] > time.monotonic() + 3590


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
