"""MyTherapy: the exact series the daily import job wrote (docs/porting.md),
from an export imported in the UI."""
import json

import pytest
from fastapi.testclient import TestClient

from app import runner
from app.connectors import mytherapy
from app.config import settings
from app.main import app
from tests.fakes import MYTHERAPY_NAMES, epoch, mytherapy_row, parse, write_mytherapy_export

# What the job wrote for a dose (the `user` tag: one account).
USER = "alice"


@pytest.fixture()
def connector(mytherapy_connector, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return mytherapy_connector


def _import(db, connector, tmp_path, rows, name="export.csv", zipped=False, **options):
    """Import an export as the UI's upload does."""
    path = write_mytherapy_export(tmp_path / name, rows, zipped=zipped)
    return runner.run(db, connector, file=str(path), filename=name, trigger="upload", **options)


def test_an_import_writes_the_jobs_exact_line(connector, influx, db, tmp_path):
    """One point per scheduled dose, at the dose's local time, with the job's
    fields and types: taken (int), app_status, dose (float), unit, delay_min.
    The export replaces the series, as the job's own run did."""
    run = _import(db, connector, tmp_path,
                  [mytherapy_row(scheduled="2026-03-01 12:00:00", actual="2026-03-01 12:04:00")])
    assert run.status == "ok" and run.points == 1
    assert run.message == ("Imported export.csv: 1 point(s) written (BodyStats 1), "
                           "after dropping its own series.")
    # 12:00 in Paris is 11:00 UTC in March (CET).
    assert influx.written_lines() == [
        f'medication,med=Alpha\\ med,source=mytherapy,status=taken,user={USER} '
        f'app_status="confirmed",delay_min=4i,dose=2.0,taken=1i,unit="pills" {epoch(2026, 3, 1, 11, 0)}']


def test_the_export_is_read_from_a_zip_too(connector, influx, db, tmp_path):
    """The app saves its CSV in an archive: the job read either."""
    _import(db, connector, tmp_path, [mytherapy_row()], name="MyTherapy Archive.zip", zipped=True)
    (_tags, fields, _ts), = influx.stored("medication")
    assert fields["taken"] == "1i"


def test_a_dose_keeps_its_local_time_across_a_dst_change(connector, influx, db, tmp_path):
    """The export's times are local, unlike Medisafe's UTC: a 12:00 dose stays
    12:00, so the same clock time is an hour apart across the change."""
    _import(db, connector, tmp_path, [
        mytherapy_row(scheduled="2026-03-28 12:00:00"),  # CET (+01:00)
        mytherapy_row(scheduled="2026-03-30 12:00:00"),  # CEST (+02:00): the clocks went forward on the 29th
    ])
    assert [ts for _tags, _fields, ts in influx.stored("medication")] == [
        epoch(2026, 3, 28, 11, 0), epoch(2026, 3, 30, 10, 0)]


def test_an_import_replaces_its_own_series_and_nothing_else(connector, influx, db, tmp_path):
    """The export is the app's whole history: a dose it no longer has (deleted
    in the app) goes with it. Another source of the same measurement is never
    touched."""
    influx.points[("BodyStats", "medication", (("source", "medisafe"),), 1)] = {"taken": "1i"}
    _import(db, connector, tmp_path, [mytherapy_row(scheduled="2026-03-01 12:00:00"),
                                      mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    assert len(influx.stored("medication")) == 3
    # The next export, the morning after: the 03-02 dose is gone from the app.
    _import(db, connector, tmp_path, [mytherapy_row(scheduled="2026-03-01 12:00:00")])
    assert [query for _db, query in influx.queries if query.startswith("DROP")] == [
        "DROP SERIES FROM \"medication\" WHERE \"source\" = 'mytherapy'"] * 2
    assert [dict(tags)["source"] for tags, _fields, _ts in influx.stored("medication")] == [
        "medisafe", "mytherapy"]


def test_a_dry_run_of_an_import_drops_nothing(connector, influx, db, tmp_path):
    """*Dry run* shows the line protocol and writes nothing at all, the drop
    included."""
    path = write_mytherapy_export(tmp_path / "export.csv", [mytherapy_row()])
    run = runner.run(db, connector, dry_run=True, file=str(path), filename="export.csv", trigger="upload")
    assert run.status == "ok" and run.points == 1
    assert run.message == ("Dry run. Imported export.csv: 1 point(s) would be written (BodyStats 1), "
                           "after dropping its own series.")
    assert "medication,med=Alpha" in run.preview
    assert influx.written_lines() == []
    assert not [query for _db, query in influx.queries if query.startswith("DROP")]


def test_two_doses_of_one_medication_in_the_same_second_are_a_second_apart(connector, influx, db, tmp_path):
    """The job bumped the second one: two points, the same key otherwise. The
    delay stays the row's own (the job computed it from the row's scheduled
    time, not from the bumped one): the second dose was not answered a minute
    early."""
    late = mytherapy_row(actual="2026-03-01 12:04:00")
    _import(db, connector, tmp_path, [late, late])
    points = influx.stored("medication")
    assert [ts for _tags, _fields, ts in points] == [epoch(2026, 3, 1, 11, 0), epoch(2026, 3, 1, 11, 0) + 1]
    assert [fields["delay_min"] for _tags, fields, _ts in points] == ["4i", "4i"]


def test_a_dose_without_a_value_or_unit_and_an_untaken_dose(connector, influx, db, tmp_path):
    """The job wrote neither a zero nor an empty text, and `delay_min` only
    for a dose that was taken."""
    _import(db, connector, tmp_path,
            [mytherapy_row(status="rejected", value="", unit=""),
             mytherapy_row(name="Second", status="auto skipped", value="1.5", unit="drop(s)")])
    by_status = {tags["status"]: fields for tags, fields, _ts in influx.stored("medication")}
    assert by_status["skipped"] == {"taken": "0i", "app_status": '"rejected"'}
    assert by_status["missed"] == {"taken": "0i", "app_status": '"auto skipped"', "dose": "1.5", "unit": '"drops"'}


def test_weight_reminders_and_doses_before_the_start_date_are_not_imported(connector, influx, db, tmp_path):
    """The job skipped the app's weight reminders (a scale records weight) and
    every dose before its overlap with the other import."""
    connector.settings.start_date = "2026-03-01"
    run = _import(db, connector, tmp_path, [
        mytherapy_row(scheduled="2026-02-27 07:45:00", kind="measurement", unit="kg"),
        mytherapy_row(scheduled="2026-02-28 07:45:00"),
        mytherapy_row(scheduled="2026-03-03 12:00:00"),
    ])
    assert run.points == 1
    (tags, _fields, ts), = influx.stored("medication")
    assert tags["status"] == "taken" and ts == epoch(2026, 3, 3, 11, 0)


def test_an_export_holding_no_dose_is_refused_rather_than_empty_the_series(connector, influx, db, tmp_path):
    """Only weight reminders, or an empty file: importing it would drop the
    series with nothing to write back, so the run fails instead."""
    run = _import(db, connector, tmp_path, [mytherapy_row(kind="measurement", unit="kg")],
                  name="reminders.csv")
    assert run.status == "error" and "holds no dose to import" in run.message
    assert not [query for _db, query in influx.queries if query.startswith("DROP")]
    # An export with its header and nothing else is the same case.
    (tmp_path / "empty.csv").write_text(",".join(mytherapy.COLUMNS) + "\n", encoding="utf-8")
    run = runner.run(db, connector, file=str(tmp_path / "empty.csv"), filename="empty.csv")
    assert run.status == "error" and "holds no dose to import" in run.message


def test_a_file_that_is_not_an_export_names_the_file(connector, influx, db, tmp_path):
    (tmp_path / "shopping.csv").write_text("item,price\nmilk,1.20\n", encoding="utf-8")
    run = runner.run(db, connector, file=str(tmp_path / "shopping.csv"), filename="shopping.csv")
    assert run.status == "error"
    assert run.message == "shopping.csv: not a MyTherapy export: columns ['item', 'price']."


def test_a_row_with_more_columns_than_the_header_is_still_read(connector, influx, db, tmp_path):
    """A stray comma is not worth refusing a whole export over: the job read
    the row for the columns it had."""
    run = _import(db, connector, tmp_path, [mytherapy_row() + ","])
    assert run.status == "ok" and run.points == 1


@pytest.mark.parametrize("row, message", [
    (mytherapy_row(kind="note"), "unknown type 'note'"),
    (mytherapy_row(status="postponed"), "unknown status 'postponed'"),
    (mytherapy_row(name="Third"), "aliases missing from TRIBUTARY_MYTHERAPY_NAMES"),
])
def test_what_the_job_refused_to_guess_fails_the_import(connector, influx, db, tmp_path, row, message):
    """An unknown type, an unknown status and an alias with no name to map it
    to: the job failed the run, and so does the connector. Nothing is dropped
    or written."""
    run = _import(db, connector, tmp_path, [row])
    assert run.status == "error" and message in run.message
    assert influx.stored("medication") == []
    assert not [query for _db, query in influx.queries if query.startswith("DROP")]


def test_the_unmapped_aliases_are_listed_with_their_dose_count(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path,
                  [mytherapy_row(name="Third"), mytherapy_row(name="Third"), mytherapy_row(name="Fourth")])
    assert "'Fourth' (1 dose(s)), 'Third' (2 dose(s))" in run.message


def test_a_bad_mapping_stops_the_import_not_the_app():
    """The mapping and the start date are read at each import, never at the
    start: an empty value (a missing setting) is simply no mapping, and a
    mistake fails the import, naming the setting."""
    assert mytherapy.parse_names(None) == {} and mytherapy.parse_names("  ") == {}
    assert mytherapy.parse_start_date("") is None and mytherapy.parse_start_date(None) is None
    with pytest.raises(mytherapy.ConnectorError, match="TRIBUTARY_MYTHERAPY_NAMES is not valid JSON"):
        mytherapy.parse_names("{not json")
    with pytest.raises(mytherapy.ConnectorError, match="must be a JSON object"):
        mytherapy.parse_names('["First"]')
    with pytest.raises(mytherapy.ConnectorError, match="TRIBUTARY_MYTHERAPY_START_DATE must be a date"):
        mytherapy.parse_start_date("the 12th of February")


def test_the_declared_series_is_the_jobs():
    """The tags and types the job wrote, and the alias -> name mapping that
    comes from the settings: never from this repository."""
    assert mytherapy.parse_names(json.dumps(MYTHERAPY_NAMES)) == MYTHERAPY_NAMES
    assert set(mytherapy.STATUS) == {"confirmed", "rejected", "auto skipped"}
    assert mytherapy.MEDICATION.tag_keys == ("source", "user", "med", "status")
    assert mytherapy.MEDICATION.field_types == {"taken": int, "app_status": str, "dose": float,
                                                "unit": str, "delay_min": int}
    assert mytherapy.MyTherapyConnector.required_env == ("TRIBUTARY_MYTHERAPY_NAMES",)
    assert mytherapy.MyTherapyConnector.rebuild_own_source is True


def test_it_takes_an_upload_in_the_ui(connector):
    """It has no API and no schedule: the export comes through the UI's
    *Import file* form (or POST /api/connectors/mytherapy/upload)."""
    from app import connectors

    assert connectors.get("mytherapy") is not None
    assert connector.is_configured() is False  # nothing to schedule
    assert connector.accepts_files is True and connector.file_types == (".zip", ".csv")
    page = TestClient(app).get("/").text
    section, = [part for part in page.split("<section") if 'id="mytherapy"' in part]
    assert 'action="/connectors/mytherapy/upload"' in section
    assert "TRIBUTARY_MYTHERAPY_NAMES" in section
    assert not connector.watch_dir  # a folder is not read: the export is imported
