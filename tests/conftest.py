import os
import sys
import tempfile
from pathlib import Path

import pytest

# Before the app is imported: its own data folder, nothing configured
# (environment variables win over a developer's .env file).
_DATA = tempfile.mkdtemp(prefix="tributary-tests-")
os.environ.update({
    "TRIBUTARY_DATA_DIR": _DATA,
    "TRIBUTARY_INFLUXDB_URL": "",
    "TRIBUTARY_INFLUXDB_USER": "",
    "TRIBUTARY_TZ": "Europe/Paris",
    "TRIBUTARY_ABS_URL": "",
    "TRIBUTARY_ABS_TOKEN": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])  # templates/ and static/ are relative

from app import connectors, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402,F401 -- creates the tables; scheduler off (nothing configured)
from tests.fakes import FakeAbs, FakeInflux  # noqa: E402


@pytest.fixture()
def db():
    from app.database import SessionLocal

    with SessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no run, no cursor, no queued request."""
    from app.database import SessionLocal
    from app.models import Cursor, SyncRun

    with SessionLocal() as session:
        session.query(SyncRun).delete()
        session.query(Cursor).delete()
        session.commit()
    yield
    while not scheduler._requests.empty():
        scheduler._requests.get_nowait()
    scheduler._pending.clear()


@pytest.fixture()
def influx(monkeypatch):
    fake = FakeInflux()
    monkeypatch.setattr(settings, "influxdb_url", fake.url)
    yield fake
    fake.close()


@pytest.fixture()
def abs_connector():
    return connectors.get("audiobookshelf")


@pytest.fixture()
def abs_api(monkeypatch, abs_connector):
    """Audiobookshelf configured, its API answered in-process."""
    from pydantic import SecretStr

    monkeypatch.setattr(abs_connector.settings, "url", "https://abs.example/")
    monkeypatch.setattr(abs_connector.settings, "token", SecretStr("the-api-key"))
    fake = FakeAbs()
    monkeypatch.setattr(abs_connector, "_get", fake)
    return fake


@pytest.fixture()
def no_thread(monkeypatch):
    """Requests are queued, never run by a live scheduler thread."""
    monkeypatch.setattr(scheduler, "_ensure_thread", lambda: False)
