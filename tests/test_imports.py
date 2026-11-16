from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import connectors, preferences, runner, scheduler, views
from app.connectors.base import Connector, ConnectorError, ConnectorSettings
from app.main import app
from app.models import MonthlyImportAcknowledgment, StagedUpload, SyncRun
from app.points import Measurement, Point

NOW = datetime(2026, 11, 16, 12, 0)  # naive UTC, like the database
DEFAULTS = {"app_usage", "mytherapy", "myfitnesspal", "whoop"}
STEPS = Measurement("ActivityStats", "steps", tags=("device",), fields={"steps": int})


class CsvSteps(Connector):
    key = "monthlysteps"
    label = "Synthetic steps"
    category = "Productivity"
    file_types = (".csv",)
    measurements = (STEPS,)

    def __init__(self):
        super().__init__()
        self.settings = ConnectorSettings(watch_dir=None)

    def import_file(self, path):
        for line in Path(path).read_text().splitlines():
            if line.strip() == "boom":
                raise ConnectorError("not a steps export")
            day, steps = line.split(",")
            yield Point(STEPS, datetime.fromisoformat(day), {"device": "synthetic"}, {"steps": int(steps)})


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def steps():
    connector = connectors.register(CsvSteps())
    yield connector
    connectors.unregister(connector.key)


def record(db, connector, *, trigger="upload", dry_run=False, status="ok", points=3,
           started=NOW, finished=None, message="synthetic run"):
    if finished is None and status != "running":
        finished = started + timedelta(seconds=1)
    run = SyncRun(connector=connector, trigger=trigger, dry_run=dry_run, status=status, points=points,
                  started_at=started, finished_at=finished, message=message)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def item(checklist, key):
    return next(item for item in checklist["items"] if item["key"] == key)


def main_content(page):
    return page.split('<main class="tb-content">', 1)[1].split("</main>", 1)[0]


def selected(db):
    allowed = [c.key for c in connectors.all_connectors() if c.accepts_files]
    return preferences.monthly_import_connectors(db, allowed)


def test_defaults_navigation_and_settings_choices(client, db):
    checklist = views.import_checklist(db, NOW)
    assert [entry["key"] for entry in checklist["items"]] == [
        "app_usage", "mytherapy", "myfitnesspal", "whoop"]
    assert len(checklist["remaining"]) == 4 and checklist["completed"] == []
    assert all(entry["state"] == "never" for entry in checklist["items"])

    response = client.get("/imports")
    assert response.status_code == 200
    page = response.text
    # Desktop sidebar and mobile top bar (not the noscript refresh link).
    assert '<a class="tb-side-link active" href="/imports">' in page
    assert '<a class="nav-link active" href="/imports">Imports</a>' in page
    assert 'data-poll="/partials/imports"' in page
    content = main_content(page)
    assert "4 remaining" in content and "0 completed" in content
    for key in DEFAULTS:
        assert f'data-region="import:{key}"' in content
        assert f'action="/connectors/{key}/check"' in content
    for key in ("medisafe", "sleep_as_android", "stryd"):
        assert f'data-region="import:{key}"' not in content

    settings = client.get("/settings").text
    for key in DEFAULTS:
        field = settings.split(f'id="monthly-{key}"', 1)[1].split("/>", 1)[0]
        assert "checked" in field
    for key in ("medisafe", "sleep_as_android", "stryd"):
        field = settings.split(f'id="monthly-{key}"', 1)[1].split("/>", 1)[0]
        assert "checked" not in field
    assert 'id="monthly-todoist"' not in settings


def test_membership_is_configurable_and_independent_of_visibility(client, db):
    known = [c.key for c in connectors.all_connectors()]
    preferences.set_hidden_connectors(db, {"whoop"}, known)
    db.commit()

    response = client.post("/settings/imports", data={
        "monthly": ["stryd", "mytherapy", "todoist", "unknown"]},
        headers={"referer": "http://testserver/settings"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    assert selected(db) == {"stryd", "mytherapy"}
    assert preferences.hidden_connectors(db) == {"whoop"}

    content = main_content(client.get("/imports").text)
    assert 'data-region="import:stryd"' in content
    assert 'data-region="import:mytherapy"' in content
    assert 'data-region="import:whoop"' not in content

    # The existing Show all control changes only overview visibility.
    client.post("/settings", data={"reset": "true"})
    assert preferences.hidden_connectors(db) == set()
    assert selected(db) == {"stryd", "mytherapy"}

    client.post("/settings/imports", data={"reset": "true"})
    assert selected(db) == DEFAULTS


def test_an_empty_checklist_is_intentional_and_valid(client, db):
    client.post("/settings/imports", data={})
    assert selected(db) == set()
    checklist = views.import_checklist(db, NOW)
    assert checklist["items"] == []

    page = client.get("/imports").text
    assert 'data-poll=""' in page
    assert "No sources are in the monthly checklist" in main_content(page)
    partial = client.get("/partials/imports")
    assert partial.status_code == 200 and "0 remaining" in partial.text
    assert 'data-region="import:' not in partial.text


def test_only_a_successful_real_upload_completes_the_month(db):
    key = "mytherapy"
    record(db, key, dry_run=True)
    record(db, key, status="error")
    record(db, key, trigger="schedule")
    record(db, key, trigger="manual")
    assert item(views.import_checklist(db, NOW), key)["state"] == "never"

    imported = record(db, key, points=0)
    current = item(views.import_checklist(db, NOW), key)
    assert current["state"] == "done" and current["done"]
    assert current["last_import"].id == imported.id
    assert current["last_import"].points == 0

    # Later checks and failures are visible, but cannot erase that success.
    record(db, key, dry_run=True, status="error", message="synthetic bad check",
           started=NOW + timedelta(minutes=1))
    current = item(views.import_checklist(db, NOW), key)
    assert current["state"] == "done" and current["activity"]["label"] == "Check failed"
    failed = record(db, key, status="error", message="synthetic failed import",
                    started=NOW + timedelta(minutes=2))
    current = item(views.import_checklist(db, NOW), key)
    assert current["state"] == "done" and current["last_import"].id == imported.id
    assert current["activity"]["label"] == "Import failed"
    assert current["activity"]["run"].id == failed.id


def test_api_success_and_previous_import_leave_whoop_and_other_sources_outstanding(db):
    record(db, "whoop", trigger="schedule", status="ok")
    assert item(views.import_checklist(db, NOW), "whoop")["state"] == "never"

    previous = datetime(2026, 10, 31, 12, 0)
    record(db, "myfitnesspal", started=previous)
    entry = item(views.import_checklist(db, NOW), "myfitnesspal")
    assert entry["state"] == "due" and not entry["done"]


@pytest.mark.parametrize("moment, finished, month", [
    # 23:30 UTC on 31 October is already 1 November in Europe/Paris.
    (datetime(2026, 11, 1, 0, 30), datetime(2026, 10, 31, 23, 30), "November 2026"),
    # Also exercise the December/January boundary calculation.
    (datetime(2026, 12, 31, 23, 30), datetime(2026, 12, 31, 23, 0), "January 2027"),
])
def test_completion_uses_local_month_boundaries(db, moment, finished, month):
    record(db, "mytherapy", started=finished, finished=finished)
    checklist = views.import_checklist(db, moment)
    assert checklist["month"] == month
    assert item(checklist, "mytherapy")["state"] == "done"


def test_a_timestamp_before_the_local_month_is_due(db):
    # 22:30 UTC is still 23:30 on 31 October in Europe/Paris.
    record(db, "mytherapy", started=datetime(2026, 10, 31, 22, 30))
    assert item(views.import_checklist(db, datetime(2026, 11, 1, 0, 30)), "mytherapy")["state"] == "due"


def test_checked_failed_and_running_activity_are_distinct(db, tmp_path):
    checked = record(db, "mytherapy", dry_run=True)
    upload = tmp_path / "synthetic.zip"
    upload.write_bytes(b"synthetic")
    staged = StagedUpload(connector="mytherapy", path=str(upload), filename="synthetic.zip",
                          file_sha256="file", points_sha256="points", config_sha256="config",
                          dry_run_id=checked.id, created_at=NOW)
    db.add(staged)
    db.commit()

    current = item(views.import_checklist(db, NOW), "mytherapy")
    assert current["activity"]["state"] == "checked"
    assert current["activity"]["label"] == "Checked, not imported"
    assert current["staged_expires"] == NOW + timedelta(hours=24)

    record(db, "mytherapy", dry_run=True, status="error", message="synthetic bad check",
           started=NOW + timedelta(minutes=1))
    current = item(views.import_checklist(db, NOW), "mytherapy")
    assert current["activity"]["label"] == "Check failed" and current["staged"] is not None

    running = record(db, "mytherapy", status="running", started=NOW + timedelta(minutes=2))
    current = item(views.import_checklist(db, NOW), "mytherapy")
    assert current["activity"]["state"] == "running" and current["activity"]["label"] == "Importing"
    assert current["activity"]["run"].id == running.id

    record(db, "myfitnesspal", dry_run=True, status="running")
    current = item(views.import_checklist(db, NOW), "myfitnesspal")
    assert current["activity"]["label"] == "Checking file"


def test_whoop_always_has_the_manual_file_action(client, monkeypatch):
    whoop = connectors.get("whoop")
    monkeypatch.setattr(whoop, "api_configured", lambda: True)
    content = main_content(client.get("/imports").text)
    row = content.split('data-region="import:whoop"', 1)[1].split("</li>", 1)[0]
    assert 'data-expand="whoop"' in row
    assert 'action="/connectors/whoop/check"' in row
    assert 'action="/connectors/whoop/run"' not in row


def test_inline_check_import_and_later_failure(client, steps, influx, db, no_thread):
    client.post("/settings/imports", data={"monthly": [steps.key]})
    response = client.post("/connectors/monthlysteps/check",
                           files={"file": ("synthetic.csv", b"2026-11-01,42\n")},
                           headers={"referer": "http://testserver/imports"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/imports"
    # The scheduler queue is consumed explicitly; no live thread runs in tests.
    key, options = scheduler._requests.get_nowait()
    assert key == steps.key
    checked = runner.run(db, steps, **dict(options))
    assert checked.status == "ok" and checked.dry_run

    page = client.get("/imports").text
    content = main_content(page)
    assert "Checked, not imported" in content and "Import for real" in content and "Discard" in content
    partial = client.get("/partials/imports").text
    assert 'data-region="import:monthlysteps"' in partial
    assert 'type="file"' not in partial  # polling can never clear the picker

    response = client.post("/connectors/monthlysteps/import", headers={"referer": "http://testserver/imports"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/imports"
    key, options = scheduler._requests.get_nowait()
    imported = runner.run(db, steps, **dict(options))
    assert imported.status == "ok" and not imported.dry_run
    assert imported.promoted_from == checked.id

    content = main_content(client.get("/imports").text)
    assert "Done this month" in content and "0 remaining" in content
    assert "Checked, not imported" not in content
    assert influx.stored("steps")

    # A later bad check says so without moving the completed item back to To import.
    client.post("/connectors/monthlysteps/check", files={"file": ("bad.csv", b"boom\n")},
                headers={"referer": "http://testserver/imports"})
    key, options = scheduler._requests.get_nowait()
    failed = runner.run(db, steps, **dict(options))
    assert failed.status == "error" and failed.dry_run
    content = main_content(client.get("/imports").text)
    assert "Done this month" in content and "Check failed" in content
    assert "not a steps export" in content and "0 remaining" in content


def test_no_data_acknowledgment_and_undo_do_not_create_an_import(client, db):
    key = "mytherapy"
    response = client.post(f"/imports/{key}/no-data", headers={"referer": "http://testserver/imports"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/imports"

    month = views.monthly_import_period()["key"]
    rows = db.query(MonthlyImportAcknowledgment).all()
    assert [(row.connector, row.month) for row in rows] == [(key, month)]
    assert rows[0].created_at is not None
    assert db.query(SyncRun).count() == 0 and scheduler._requests.empty()

    checklist = views.import_checklist(db)
    current = item(checklist, key)
    assert current["state"] == "no_data" and current["done"]
    assert current["last_import"] is None and current["acknowledgment"] is not None
    assert key not in {entry["key"] for entry in checklist["remaining"]}
    assert key in {entry["key"] for entry in checklist["completed"]}
    assert all(item(checklist, other)["state"] == "never"
               for other in DEFAULTS - {key})

    # A repeated click is idempotent: one source/month acknowledgment only.
    client.post(f"/imports/{key}/no-data")
    assert db.query(MonthlyImportAcknowledgment).count() == 1

    response = client.post(f"/imports/{key}/no-data/undo", headers={"referer": "http://testserver/imports"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/imports"
    assert db.query(MonthlyImportAcknowledgment).count() == 0
    assert item(views.import_checklist(db), key)["state"] == "never"
    assert db.query(SyncRun).count() == 0 and scheduler._requests.empty()


def test_no_data_actions_agree_in_the_page_and_polled_fragment(client, db):
    key = "myfitnesspal"
    acknowledge = f'action="/imports/{key}/no-data"'
    undo = f'action="/imports/{key}/no-data/undo"'

    for path in ("/imports", "/partials/imports"):
        page = client.get(path).text
        assert acknowledge in page and "No data to import" in page
        assert undo not in page

    client.post(f"/imports/{key}/no-data")
    for path in ("/imports", "/partials/imports"):
        page = client.get(path).text
        assert undo in page and "Undo no data" in page
        assert "No data this month" in page and "No file or measurement was changed" in page
        if path == "/partials/imports":
            assert 'type="file"' not in page

    client.post(f"/imports/{key}/no-data/undo")
    for path in ("/imports", "/partials/imports"):
        page = client.get(path).text
        assert acknowledge in page and undo not in page


def test_no_data_acknowledgment_is_per_connector_and_local_month(db):
    db.add(MonthlyImportAcknowledgment(connector="mytherapy", month="2026-11", created_at=NOW))
    db.add(MonthlyImportAcknowledgment(connector="myfitnesspal", month="2026-12", created_at=NOW))
    db.commit()

    november = views.import_checklist(db, datetime(2026, 11, 1, 0, 30))
    assert item(november, "mytherapy")["state"] == "no_data"
    assert item(november, "myfitnesspal")["state"] == "never"
    # The November row does not reach backward into the previous local month.
    october = views.import_checklist(db, datetime(2026, 10, 31, 22, 30))
    assert item(october, "mytherapy")["state"] == "never"

    december = views.import_checklist(db, datetime(2026, 12, 1, 0, 30))
    assert item(december, "mytherapy")["state"] == "never"
    assert item(december, "myfitnesspal")["state"] == "no_data"
    # 23:30 UTC on 31 December is already January 2027 in Europe/Paris.
    january = views.import_checklist(db, datetime(2026, 12, 31, 23, 30))
    assert item(january, "myfitnesspal")["state"] == "never"
    assert db.query(MonthlyImportAcknowledgment).count() == 2


def test_real_import_is_separate_from_no_data_acknowledgment(client, db):
    key = "mytherapy"
    month = views.monthly_import_period()["key"]
    db.add(MonthlyImportAcknowledgment(connector=key, month=month))
    db.commit()
    assert item(views.import_checklist(db), key)["state"] == "no_data"

    imported = record(db, key, points=0)
    current = item(views.import_checklist(db), key)
    assert current["state"] == "done" and current["last_import"].id == imported.id
    assert current["acknowledgment"] is not None

    client.post(f"/imports/{key}/no-data/undo")
    current = item(views.import_checklist(db), key)
    assert current["state"] == "done" and current["last_import"].id == imported.id
    assert current["acknowledgment"] is None

    # A failed check does not revoke an acknowledgment-only completion either.
    db.add(MonthlyImportAcknowledgment(connector="myfitnesspal", month=month))
    record(db, "myfitnesspal", dry_run=True, status="error", message="synthetic bad check")
    current = item(views.import_checklist(db), "myfitnesspal")
    assert current["state"] == "no_data" and current["done"]
    assert current["activity"]["label"] == "Check failed"


def test_no_data_write_validation_and_origin_protection(client, db):
    assert client.post("/imports/unknown/no-data").status_code == 404
    for key in ("todoist", "medisafe"):
        response = client.post(f"/imports/{key}/no-data", headers={"referer": "http://testserver/imports"},
                               follow_redirects=False)
        assert response.status_code == 303 and "notice=" in response.headers["location"]
    assert db.query(MonthlyImportAcknowledgment).count() == 0

    assert client.post("/imports/mytherapy/no-data",
                       headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert db.query(MonthlyImportAcknowledgment).count() == 0
    assert client.post("/imports/mytherapy/no-data", headers={"sec-fetch-site": "same-origin"},
                       follow_redirects=False).status_code == 303
    assert db.query(MonthlyImportAcknowledgment).count() == 1
