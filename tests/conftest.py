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
    "TRIBUTARY_RENPHO_EMAIL": "",
    "TRIBUTARY_RENPHO_PASSWORD": "",
    "TRIBUTARY_RENPHO_USER": "",
    "TRIBUTARY_RENPHO_THRESHOLDS": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])  # templates/ and static/ are relative

from app import connectors, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402,F401 -- creates the tables; scheduler off (nothing configured)
from tests.fakes import THRESHOLDS, FakeAbs, FakeInflux, FakeRenpho  # noqa: E402


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
    monkeypatch.setattr(abs_connector, "_post", fake.post)
    return fake


@pytest.fixture()
def renpho_connector():
    return connectors.get("renpho")


@pytest.fixture()
def renpho_cloud(monkeypatch, renpho_connector):
    """Renpho configured with one account (alice, who has level boundaries),
    its cloud served in-process over HTTP to the real renpho-api client."""
    import json

    from pydantic import SecretStr

    fake = FakeRenpho()
    monkeypatch.setattr("renpho.client.API_BASE_URL", fake.url)
    settings_ = renpho_connector.settings
    monkeypatch.setattr(settings_, "email", "alice@example.com")
    monkeypatch.setattr(settings_, "password", SecretStr("alice-password"))
    monkeypatch.setattr(settings_, "user", "alice")
    monkeypatch.setattr(settings_, "thresholds", json.dumps(THRESHOLDS))
    monkeypatch.setattr(settings_, "_numbered", {})
    yield fake
    fake.close()


@pytest.fixture()
def bob(monkeypatch, renpho_cloud, renpho_connector):
    """A second Renpho account, bob's (no level boundaries)."""
    monkeypatch.setattr(renpho_connector.settings, "_numbered", {
        "TRIBUTARY_RENPHO_EMAIL_2": "bob@example.com", "TRIBUTARY_RENPHO_PASSWORD_2": "bob-password",
        "TRIBUTARY_RENPHO_USER_2": "bob"})
    return renpho_cloud


@pytest.fixture()
def no_thread(monkeypatch):
    """Requests are queued, never run by a live scheduler thread."""
    monkeypatch.setattr(scheduler, "_ensure_thread", lambda: False)
