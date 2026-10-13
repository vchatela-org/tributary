import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import connectors, runner, scheduler
from app.connectors.base import Connector, ConnectorError, ConnectorSettings
from app.database import uploads_dir
from app.main import app
from app.points import Measurement, Point
from tests.fakes import parse

STEPS = Measurement("ActivityStats", "steps", tags=("device",), fields={"steps": int})


class CsvSteps(Connector):
    """A file connector: `date,steps` lines."""
    key = "csvsteps"
    label = "CSV steps"
    file_types = (".csv",)
    measurements = (STEPS,)

    def __init__(self, watch_dir=None, rebuild=False):
        super().__init__()
        self.settings = ConnectorSettings(watch_dir=watch_dir)
        self.rebuild_own_source = rebuild

    def import_file(self, path):
        from datetime import datetime

        for line in Path(path).read_text().splitlines():
            if line.strip() == "boom":
                raise ConnectorError("not a steps export")
            day, steps = line.split(",")
            yield Point(STEPS, datetime.fromisoformat(day), {"device": "watch"}, {"steps": int(steps)})


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def steps():
    connector = connectors.register(CsvSteps())
    yield connector
    connectors.unregister("csvsteps")


# -- Pages ----------------------------------------------------------------------------------

def test_integrations_page_lists_connectors_and_what_they_need(client):
    page = client.get("/").text
    assert "Audiobookshelf" in page and "Not configured" in page
    assert "<code>TRIBUTARY_ABS_URL</code>" in page and "InfluxDB not configured" in page
    assert 'action="/connectors/audiobookshelf/run"' not in page
    assert "Renpho" in page and "<code>TRIBUTARY_RENPHO_PASSWORD</code>" in page


def test_a_configured_connector_has_its_buttons_and_runs(client, abs_api, abs_connector, db):
    runner.run(db, abs_connector, dry_run=True)
    page = client.get("/").text
    assert "every hour" in page and 'action="/connectors/audiobookshelf/run"' in page
    assert "Dry run" in page and "Full resync" in page
    run_id = client.get("/api/runs").json()[0]["id"]
    detail = client.get(f"/runs/{run_id}").text
    assert "Line protocol" in detail and "reading_session,device=Google\\ Pixel\\ 7" in detail
    history = client.get("/runs?connector=audiobookshelf").text
    assert f'href="/runs/{run_id}"' in history
    assert client.get("/runs/999999").status_code == 404
    assert client.get("/runs?connector=nope").status_code == 404


def test_health_answers_without_anything_configured(client):
    assert client.get("/api/health").json()["status"] == "ok"


# -- Run buttons --------------------------------------------------------------------------

def test_run_buttons_queue_and_return_to_their_page(client, abs_api, no_thread):
    response = client.post("/connectors/audiobookshelf/run", data={"dry_run": "true"}, follow_redirects=False,
                           headers={"referer": "https://tributary.example/runs?connector=audiobookshelf"})
    assert response.status_code == 303 and response.headers["location"] == "/runs?connector=audiobookshelf"
    response = client.post("/connectors/audiobookshelf/run", data={"full": "true"}, follow_redirects=False,
                           headers={"referer": "https://evil.example"})
    assert response.headers["location"] == "/"
    queued = [scheduler._requests.get_nowait() for _ in range(scheduler._requests.qsize())]
    assert queued == [("audiobookshelf", (("dry_run", True), ("trigger", "manual"))),
                      ("audiobookshelf", (("full", True), ("trigger", "manual")))]


@pytest.mark.parametrize("referer", ["https://x//evil.example/phish", "https://x/\\evil.example"])
def test_never_redirects_off_site(client, abs_api, no_thread, referer):
    response = client.post("/connectors/audiobookshelf/run", follow_redirects=False, headers={"referer": referer})
    assert response.headers["location"] == "/"


def test_cross_site_requests_are_refused(client, abs_api, no_thread):
    for site in ("cross-site", "same-site"):
        assert client.post("/connectors/audiobookshelf/run", headers={"sec-fetch-site": site}).status_code == 403
        assert client.post("/api/connectors/audiobookshelf/run", headers={"sec-fetch-site": site}).status_code == 403
    assert scheduler._requests.empty()
    assert client.post("/api/connectors/audiobookshelf/run", headers={"sec-fetch-site": "same-origin"}).status_code == 202


def test_without_sec_fetch_site_the_origin_must_be_this_host(client, abs_api, no_thread):
    """Browsers too old for Sec-Fetch-Site still send Origin with a POST."""
    url = "/api/connectors/audiobookshelf/run"
    assert client.post(url, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post(url, headers={"origin": "null"}).status_code == 403
    assert client.post(url, headers={"origin": "http://testserver"}).status_code == 202
    # Behind a proxy, the host the browser used.
    assert client.post(url, headers={"origin": "https://tributary.example",
                                     "x-forwarded-host": "tributary.example"}).status_code == 202


def test_api_run_unknown_or_unconfigured(client):
    assert client.post("/api/connectors/nope/run").status_code == 404
    assert client.post("/api/connectors/audiobookshelf/run").status_code == 409


# -- Files ---------------------------------------------------------------------------------

def test_upload_is_queued_imported_then_deleted(client, steps, influx, db, no_thread):
    response = client.post("/connectors/csvsteps/upload", files={"file": ("my steps.csv", b"2025-01-01,8000\n")},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/runs?connector=csvsteps"
    key, options = scheduler._requests.get_nowait()
    options = dict(options)
    assert key == "csvsteps" and options["filename"] == "my steps.csv" and options["trigger"] == "upload"
    saved = options.pop("file")
    assert Path(saved).parent == uploads_dir() and os.path.exists(saved)
    run = runner.run(db, steps, file=saved, **options)
    assert run.status == "ok" and run.message == "Imported my steps.csv: 1 point(s) written (ActivityStats 1)."
    assert not os.path.exists(saved)
    (tags, fields, _ts), = influx.stored("steps")
    assert tags == {"source": "csvsteps", "device": "watch"} and fields == {"steps": "8000i"}


def test_upload_checks_the_file_type_and_connector(client, steps, no_thread):
    assert client.post("/api/connectors/csvsteps/upload", files={"file": ("x.zip", b"x")}).status_code == 400
    assert client.post("/api/connectors/audiobookshelf/upload", files={"file": ("x.csv", b"x")}).status_code == 400
    response = client.post("/api/connectors/csvsteps/upload", params={"dry_run": "true"},
                           files={"file": ("x.CSV", b"2025-01-01,1\n")})
    assert response.status_code == 202 and response.json()["dry_run"] is True
    assert "csvsteps" in client.get("/").text and 'enctype="multipart/form-data"' in client.get("/").text


def test_a_bad_file_fails_its_run_with_its_name(steps, influx, db, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("boom\n")
    run = runner.run(db, steps, file=str(bad), filename="bad.csv", trigger="upload")
    assert run.status == "error" and run.message == "bad.csv: not a steps export"


def test_watched_folder_imports_new_and_changed_files_only(influx, db, tmp_path):
    connector = connectors.register(CsvSteps(watch_dir=str(tmp_path)))
    try:
        (tmp_path / "a.csv").write_text("2025-01-01,100\n")
        (tmp_path / "notes.txt").write_text("ignored")
        assert connector.is_configured()
        run = runner.run(db, connector)
        assert run.points == 1 and "1 new or changed file(s)" in run.message
        assert runner.run(db, connector).message == "Nothing new."
        (tmp_path / "b.csv").write_text("2025-01-02,200\n")
        run = runner.run(db, connector)
        assert run.points == 1
        assert not [q for _db, q in influx.queries if q.startswith("DROP")]
    finally:
        connectors.unregister("csvsteps")


def test_rebuild_own_source_drops_only_its_own_series(influx, db, tmp_path):
    connector = connectors.register(CsvSteps(watch_dir=str(tmp_path), rebuild=True))
    try:
        (tmp_path / "a.csv").write_text("2025-01-01,100\n2025-01-02,150\n")
        # Another source in the same measurement.
        influx.points[("ActivityStats", "steps", (("source", "other"),), 1)] = {"steps": "5i"}
        runner.run(db, connector)
        assert len(influx.stored("steps")) == 3
        # The export no longer has 2025-01-02: rebuilt from the folder, it's gone.
        (tmp_path / "a.csv").write_text("2025-01-01,100\n")
        run = runner.run(db, connector)
        assert run.status == "ok" and "own series rebuilt" in run.message
        drops = [q for _db, q in influx.queries if q.startswith("DROP")]
        assert drops == ["DROP SERIES FROM \"steps\" WHERE \"source\" = 'csvsteps'"] * 2
        assert sorted(t["source"] for t, _f, _ts in influx.stored("steps")) == ["csvsteps", "other"]
        assert runner.run(db, connector).message == "Nothing new."
        assert len([q for _db, q in influx.queries if q.startswith("DROP")]) == 2
    finally:
        connectors.unregister("csvsteps")


class RangeSteps(CsvSteps):
    """A file connector whose files cover 2025-01-02 to 2025-01-03."""

    def import_file(self, path):
        from datetime import date

        from app.connectors.base import FileImport, Span

        return FileImport(list(super().import_file(path)), {"steps": Span(date(2025, 1, 2), date(2025, 1, 3))})


def test_an_upload_with_a_span_replaces_those_days_only(influx, db, tmp_path, monkeypatch):
    """FileImport.spans: an export of a date range replaces the connector's
    own series over those days (local midnights, TRIBUTARY_TZ), for this
    user, and nothing outside them."""
    from app.config import settings

    monkeypatch.setattr(settings, "influxdb_user", "alice")
    connector = connectors.register(RangeSteps(rebuild=True))
    try:
        for day in (1, 2, 3, 4):  # midnight UTC, i.e. 01:00 in Paris
            influx.points[("ActivityStats", "steps", (("device", "watch"), ("source", "csvsteps"), ("user", "alice")),
                           int(datetime(2025, 1, day, tzinfo=timezone.utc).timestamp()))] = {"steps": "1i"}
        upload = tmp_path / "range.csv"
        upload.write_text("2025-01-03T12:00:00+00:00,300\n")
        run = runner.run(db, connector, file=str(upload), filename="range.csv", trigger="upload")
        assert run.message == ("Imported range.csv: 1 point(s) written (ActivityStats 1), "
                               "after dropping its own series from 2025-01-02 to 2025-01-03.")
        assert [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))] == [
            "DELETE FROM \"steps\" WHERE \"source\" = 'csvsteps' AND \"user\" = 'alice' "
            "AND time >= '2025-01-01T23:00:00Z' AND time < '2025-01-03T23:00:00Z'"]
        assert [(ts, f["steps"]) for _t, f, ts in influx.stored("steps")] == [
            (int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()), "1i"),
            (int(datetime(2025, 1, 3, 12, tzinfo=timezone.utc).timestamp()), "300i"),
            (int(datetime(2025, 1, 4, tzinfo=timezone.utc).timestamp()), "1i")]
    finally:
        connectors.unregister("csvsteps")


def test_a_span_without_a_user_is_refused(influx, db, tmp_path):
    """Without TRIBUTARY_INFLUXDB_USER a span would reach every user's points,
    and the rewritten ones would carry no user tag: the import fails first,
    dry run included."""
    connector = connectors.register(RangeSteps(rebuild=True))
    try:
        upload = tmp_path / "range.csv"
        for dry_run in (True, False):
            upload.write_text("2025-01-03T12:00:00+00:00,300\n")
            run = runner.run(db, connector, dry_run=dry_run, file=str(upload), filename="range.csv",
                             trigger="upload")
            assert run.status == "error" and "TRIBUTARY_INFLUXDB_USER is not set" in run.message
        assert influx.written_lines() == []
        assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]
    finally:
        connectors.unregister("csvsteps")


def test_a_connector_may_mask_an_uploads_name(influx, db, tmp_path, client):
    """upload_label: what a run calls the file (stored, shown, logged), for a
    name that holds an account number."""
    class Masked(CsvSteps):
        def upload_label(self, filename):
            return filename.replace("12345", "<account>")

    connectors.register(Masked())
    try:
        response = client.post("/api/connectors/csvsteps/upload",
                               files={"file": ("steps_12345.csv", b"2025-01-03T12:00:00+00:00,300\n")})
        assert response.json()["file"] == "steps_<account>.csv"
        upload = tmp_path / "steps_12345.csv"
        upload.write_text("2025-01-03T12:00:00+00:00,300\n")
        run = runner.run(db, connectors.get("csvsteps"), file=str(upload), filename=upload.name, trigger="upload")
        assert run.message.startswith("Imported steps_<account>.csv") and "12345" not in run.message
    finally:
        connectors.unregister("csvsteps")


def test_an_upload_spanning_no_measurement_drops_nothing(influx, db, tmp_path):
    """A file whose spans cover none of the series (an empty export of a
    rebuild-by-span connector) adds its points and drops nothing."""
    from app.connectors.base import FileImport

    class NoSpan(CsvSteps):
        def import_file(self, path):
            return FileImport(list(super().import_file(path)), {})

    connector = connectors.register(NoSpan(rebuild=True))
    try:
        upload = tmp_path / "a.csv"
        upload.write_text("2025-01-03T12:00:00+00:00,300\n")
        run = runner.run(db, connector, file=str(upload), filename="a.csv", trigger="upload")
        assert run.message == "Imported a.csv: 1 point(s) written (ActivityStats 1)."
        assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]
    finally:
        connectors.unregister("csvsteps")


def test_a_missing_watched_folder_is_an_error(influx, db, tmp_path):
    connector = connectors.register(CsvSteps(watch_dir=str(tmp_path / "missing")))
    try:
        run = runner.run(db, connector)
        assert run.status == "error" and "Watched folder not found" in run.message
    finally:
        connectors.unregister("csvsteps")


def test_line_protocol_parser_sanity():
    assert parse("m,a=b f=1i 2") == ("m", {"a": "b"}, {"f": "1i"}, 2)
