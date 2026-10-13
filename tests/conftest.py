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
    "TRIBUTARY_TODOIST_TOKEN": "",
    "TRIBUTARY_MYTHERAPY_WATCH_DIR": "",
    "TRIBUTARY_MYTHERAPY_NAMES": "",
    "TRIBUTARY_MYTHERAPY_START_DATE": "",
    "TRIBUTARY_MEDISAFE_NAMES": "",
    "TRIBUTARY_MEDISAFE_NOTES": "false",
    "TRIBUTARY_MEDISAFE_END_DATE": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])  # templates/ and static/ are relative

from app import connectors, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402,F401 -- creates the tables; scheduler off (nothing configured)
from tests.fakes import MYTHERAPY_NAMES, THRESHOLDS, TODOIST_TOKEN  # noqa: E402
from tests.fakes import FakeAbs, FakeInflux, FakeRenpho, FakeTodoist  # noqa: E402


@pytest.fixture()
def db():
    from app.database import SessionLocal

    with SessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no run, no cursor, no schedule set in the UI, no
    checked upload and no preference, and nothing queued."""
    from app.database import SessionLocal
    from app.models import ConnectorSchedule, Cursor, Preference, StagedUpload, SyncRun

    with SessionLocal() as session:
        for table in (SyncRun, Cursor, ConnectorSchedule, StagedUpload, Preference):
            session.query(table).delete()
        session.commit()
    yield
    # A test that saved a schedule starts the thread (app/scheduler.reschedule);
    # stop it, so it cannot consume the next test's queue.
    scheduler.stop()
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


@pytest.fixture()
def todoist_connector():
    return connectors.get("todoist")


@pytest.fixture()
def myfitnesspal_connector():
    return connectors.get("myfitnesspal")


@pytest.fixture()
def medisafe_connector(monkeypatch):
    """Medisafe as configured by nothing: no mapping, no notes, no end date."""
    connector = connectors.get("medisafe")
    for name, value in (("names", None), ("notes", False), ("end_date", None)):
        monkeypatch.setattr(connector.settings, name, value)
    return connector


@pytest.fixture()
def mytherapy_connector(monkeypatch):
    """MyTherapy with a mapping (invented aliases): it takes an export upload."""
    import json

    from app.connectors import mytherapy

    connector = connectors.get("mytherapy")
    monkeypatch.setattr(connector, "settings",
                        mytherapy.MyTherapySettings(names=json.dumps(MYTHERAPY_NAMES)))
    return connector


@pytest.fixture()
def todoist_api(monkeypatch, todoist_connector):
    """Todoist configured (task text off, the default), its API served
    in-process over HTTP; retries don't wait (`fake.slept` records them)."""
    from pydantic import SecretStr

    from app.connectors import todoist

    fake = FakeTodoist()
    fake.slept = []
    monkeypatch.setattr(todoist, "API_URL", fake.url)
    monkeypatch.setattr(todoist, "_sleep", fake.slept.append)
    monkeypatch.setattr(todoist_connector.settings, "token", SecretStr(TODOIST_TOKEN))
    monkeypatch.setattr(todoist_connector.settings, "task_text", False)
    monkeypatch.setattr(todoist_connector.settings, "lookback_days", 3)
    yield fake
    fake.close()
