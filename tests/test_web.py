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
    category = "Productivity"
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
    assert "TRIBUTARY_ABS_URL" in page and "InfluxDB not configured" in page
    assert 'action="/connectors/audiobookshelf/run"' not in page
    assert "Renpho" in page and "TRIBUTARY_RENPHO_PASSWORD" in page
    # Grouped by category, every source in one of them.
    for category in ("Body &amp; health", "Medication", "Nutrition &amp; fitness", "Sleep",
                     "Reading &amp; media", "Productivity"):
        assert category in page, category


def test_a_configured_connector_has_its_buttons_and_runs(client, abs_api, abs_connector, db):
    runner.run(db, abs_connector, dry_run=True)
    page = client.get("/").text
    # Daily, not hourly: an hourly run against the server for nothing is waste.
    assert "Daily at 03:10" in page and 'action="/connectors/audiobookshelf/run"' in page
    # Run now, a dry run (the line protocol, nothing written), and Open.
    assert "Run now" in page and "Dry run" in page
    # The connector page adds the full resync.
    connector_page = client.get("/connectors/audiobookshelf").text
    assert "Dry run" in connector_page and "Full resync" in connector_page
    run_id = client.get("/api/runs").json()[0]["id"]
    detail = client.get(f"/runs/{run_id}").text
    assert "Line protocol" in detail and "reading_session,device=Google\\ Pixel\\ 7" in detail
    history = client.get("/runs?connector=audiobookshelf").text
    assert f'href="/runs/{run_id}"' in history
    assert client.get("/runs/999999").status_code == 404
    assert client.get("/runs?connector=nope").status_code == 404


def test_health_answers_without_anything_configured(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_an_upload_only_connector_reads_as_manual_not_not_configured(client):
    """A source you import into has nothing to configure and no schedule: its card
    and its page say "Manual: import a file", never the "not configured" of an API
    source missing its settings, and its page offers no editor whose value nothing
    would apply."""
    page = client.get("/").text
    assert "MyFitnessPal" in page and "Manual: import a file" in page
    connector_page = client.get("/connectors/myfitnesspal").text
    assert "Manual: import a file" in connector_page
    # No schedule editor: neither the form nor its fields are rendered.
    assert 'name="preset"' not in connector_page and "Custom (cron)" not in connector_page


def test_a_queued_run_is_shown_inline_named_by_its_label(client, steps, no_thread):
    """Several checks are queueable without the page reloading; the activity line
    says which source is queued, by its label rather than its key."""
    client.post("/connectors/csvsteps/check", files={"file": ("a.csv", b"2025-01-01,1\n")})
    activity = client.get("/").text.split('data-region="activity">', 1)[1].split("</div>", 1)[0]
    assert "CSV steps" in activity and "queued" in activity and "csvsteps" not in activity


def test_the_run_history_names_each_source_by_its_label(client, steps, influx, db, tmp_path):
    """The run history's Source column reads the connector's name, not its key."""
    upload = tmp_path / "a.csv"
    upload.write_text("2025-01-01,1\n")
    runner.run(db, steps, file=str(upload), filename="a.csv", trigger="upload")
    table = client.get("/runs").text.split('<div data-region="runs">', 1)[1]
    assert "CSV steps" in table and ">csvsteps<" not in table


# -- The overview's live regions ---------------------------------------------------------

def test_the_overview_re_reads_the_fragment_not_the_whole_page(client):
    """The page updates in place from the fragment: app.js swaps data-region nodes,
    which the fragment has at its top level and a whole page does not."""
    assert 'data-poll="/partials/status"' in client.get("/").text
    assert 'data-region="status:myfitnesspal"' in client.get("/partials/status").text


def test_the_full_resync_button_survives_a_poll(client, abs_api, no_thread):
    """The connector page renders Run now / Dry run / Full resync, and the polled
    fragment must render the same buttons or the first swap drops Full resync."""
    assert "Full resync" in client.get("/connectors/audiobookshelf").text
    assert "Full resync" in client.get("/partials/status").text


def test_a_compatible_tool_shows_what_it_covers(client):
    """The note is what tells a reader what the external tool does."""
    assert "from the Garmin API" in client.get("/").text


def test_no_show_hidden_link_before_anything_is_hidden(client):
    """Nothing hidden on a fresh install: the page does not offer to show nothing."""
    assert "Show the hidden sources" not in client.get("/").text


# -- The schedule editor -----------------------------------------------------------------

def test_the_editor_saves_a_custom_expression(client, abs_api, no_thread):
    """Choosing Custom (cron) and typing an expression saves it -- the field the
    form posts is the one build_cron expands."""
    response = client.post("/connectors/audiobookshelf/schedule",
                           data={"preset": "custom", "custom": "0 5 * * *"}, follow_redirects=False)
    assert response.status_code == 303
    body = client.get("/api/connectors/audiobookshelf/schedule").json()
    assert (body["cron"], body["source"]) == ("0 5 * * *", "ui")


def test_a_source_set_to_manual_only_says_so(client):
    """Manual only is a stored choice, told apart from a source that has no
    schedule at all."""
    assert client.post("/connectors/renpho/schedule", data={"preset": "manual"},
                       follow_redirects=False).status_code == 303
    assert "Manual only" in client.get("/connectors/renpho").text


# -- Settings ----------------------------------------------------------------------------

def test_the_settings_page_shows_a_hidden_source_unchecked(client):
    """A hidden source stays unchecked, so a later Save does not silently bring it
    back."""
    assert client.post("/settings", data={"visible": ["audiobookshelf", "renpho", "todoist"]},
                       follow_redirects=False).status_code == 303
    page = client.get("/settings").text
    assert 'id="show-myfitnesspal"' in page
    line = next(line for line in page.splitlines() if 'id="show-myfitnesspal"' in line)
    assert "checked" not in line
    assert "checked" in next(line for line in page.splitlines() if 'id="show-audiobookshelf"' in line)


def test_a_staged_check_reports_the_points_it_would_write(client, steps, db, no_thread):
    """The API says how many points the waiting check produced, so a script need
    not go and match the dry run."""
    client.post("/connectors/csvsteps/check", files={"file": ("a.csv", b"2025-01-01,1\n")})
    _key, options = scheduler._requests.get_nowait()
    runner.run(db, steps, **dict(options))
    staged = next(c["staged"] for c in client.get("/api/connectors").json() if c["key"] == "csvsteps")
    assert staged is not None and staged["points"] == 1


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

def test_the_form_checks_the_file_first_and_stays_on_the_page(client, steps, no_thread):
    """The overview's form always checks first (nothing is written), and it ends
    back on the page it came from -- never on a separate one, so the next card's
    file picker is right there."""
    response = client.post("/connectors/csvsteps/check", files={"file": ("my steps.csv", b"2025-01-01,8000\n")},
                           follow_redirects=False, headers={"referer": "/"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    key, options = scheduler._requests.get_nowait()
    options = dict(options)
    assert key == "csvsteps" and options["filename"] == "my steps.csv"
    assert options["trigger"] == "upload" and options["dry_run"] is True
    saved = options.pop("file")
    assert Path(saved).parent == uploads_dir() and os.path.exists(saved)


def test_a_check_is_kept_and_its_import_needs_no_second_upload(client, steps, influx, db, no_thread):
    """Check, read, import: the file is kept between the two."""
    client.post("/connectors/csvsteps/check", files={"file": ("my steps.csv", b"2025-01-01,8000\n")},
                follow_redirects=False, headers={"referer": "/"})
    _key, options = scheduler._requests.get_nowait()
    options = dict(options)
    saved = options["file"]
    checked = runner.run(db, steps, **options)
    assert checked.status == "ok" and checked.dry_run
    assert os.path.exists(saved)  # kept for the import
    assert influx.stored() == []
    staged = runner.staged_upload(db, steps)
    assert staged is not None and Path(staged.path) == Path(saved) and staged.dry_run_id == checked.id

    imported = runner.promote(db, steps)
    assert imported.status == "ok" and not imported.dry_run
    assert imported.promoted_from == checked.id
    assert imported.points == 1
    (tags, fields, _ts), = influx.stored("steps")
    assert tags == {"source": "csvsteps", "device": "watch"} and fields == {"steps": "8000i"}
    # The file and its row are gone once it is imported.
    assert not os.path.exists(saved) and runner.staged_upload(db, steps) is None


def test_an_upload_without_a_check_is_imported_and_deleted_at_once(client, steps, influx, db, no_thread):
    """The API's own upload (no check) still imports straight away and leaves
    nothing behind."""
    response = client.post("/api/connectors/csvsteps/upload", files={"file": ("my steps.csv", b"2025-01-01,8000\n")})
    assert response.status_code == 202 and response.json()["dry_run"] is False
    _key, options = scheduler._requests.get_nowait()
    options = dict(options)
    saved = options["file"]
    run = runner.run(db, steps, **options)
    assert run.status == "ok" and not os.path.exists(saved)
    assert runner.staged_upload(db, steps) is None


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


def test_the_import_endpoint_replays_a_fresh_check_and_refuses_a_stale_one(client, steps, influx, db, no_thread):
    """POST .../import imports what a check kept, without a second upload; a check
    that no longer stands is a 409, nothing queued, and the check dropped."""
    path = uploads_dir() / "a.csv"
    path.write_text("2025-01-01,1\n")
    checked = runner.run(db, steps, trigger="upload", dry_run=True, file=str(path), filename="a.csv")
    fresh = client.post("/api/connectors/csvsteps/import")
    assert fresh.status_code == 202 and fresh.json()["promoted_from"] == checked.id
    while not scheduler._requests.empty():
        scheduler._requests.get_nowait()
    # The check no longer stands: the staged file was replaced meanwhile.
    path.write_text("2025-01-01,2\n")
    stale = client.post("/api/connectors/csvsteps/import")
    assert stale.status_code == 409 and "checked" in stale.json()["detail"]
    assert scheduler._requests.empty() and runner.staged_upload(db, steps) is None


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
