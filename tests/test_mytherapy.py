"""MyTherapy: the exact series the daily import job wrote (docs/porting.md),
from a folder of exports that is the source of truth."""
import json
import os

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


def test_a_dose_writes_the_jobs_exact_line(connector, influx, db, tmp_path):
    """One point per scheduled dose, at the dose's local time, with the job's
    fields and types: taken (int), app_status, dose (float), unit, delay_min."""
    write_mytherapy_export(tmp_path / "export.csv",
                           [mytherapy_row(scheduled="2026-03-01 12:00:00", actual="2026-03-01 12:04:00")])
    run = runner.run(db, connector)
    assert run.status == "ok" and run.points == 1
    assert run.message == ("1 file(s) re-read from the watched folder, own series rebuilt: "
                           "1 point(s) written (BodyStats 1).")
    # 12:00 in Paris is 11:00 UTC in March (CET).
    assert influx.written_lines() == [
        f'medication,med=Alpha\\ med,source=mytherapy,status=taken,user={USER} '
        f'app_status="confirmed",delay_min=4i,dose=2.0,taken=1i,unit="pills" {epoch(2026, 3, 1, 11, 0)}']


def test_the_export_is_read_from_a_zip_too(connector, influx, db, tmp_path):
    """The app saves its CSV in an archive: the job read either."""
    write_mytherapy_export(tmp_path / "MyTherapy Archive.zip", [mytherapy_row()], zipped=True)
    assert runner.run(db, connector).points == 1
    (_tags, fields, _ts), = influx.stored("medication")
    assert fields["taken"] == "1i"


def test_a_dose_keeps_its_local_time_across_a_dst_change(connector, influx, db, tmp_path):
    """The export's times are local, unlike Medisafe's UTC: a 12:00 dose stays
    12:00, so the same clock time is an hour apart across the change."""
    write_mytherapy_export(tmp_path / "export.csv", [
        mytherapy_row(scheduled="2026-03-28 12:00:00"),  # CET (+01:00)
        mytherapy_row(scheduled="2026-03-30 12:00:00"),  # CEST (+02:00): the clocks went forward on the 29th
    ])
    runner.run(db, connector)
    assert [ts for _tags, _fields, ts in influx.stored("medication")] == [
        epoch(2026, 3, 28, 11, 0), epoch(2026, 3, 30, 10, 0)]


def test_the_folder_is_the_source_of_truth_and_nothing_else_is_touched(connector, influx, db, tmp_path):
    """A change in the folder drops the connector's own series and rewrites it
    from every export in it; an unchanged folder writes nothing at all."""
    export = write_mytherapy_export(tmp_path / "export.csv",
                                    [mytherapy_row(scheduled="2026-03-01 12:00:00"),
                                     mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    # Another source of the same measurement: never dropped, never rewritten.
    influx.points[("BodyStats", "medication", (("source", "medisafe"),), 1)] = {"taken": "1i"}
    run = runner.run(db, connector)
    assert run.status == "ok" and run.points == 2
    assert [query for _db, query in influx.queries if query.startswith("DROP")] == [
        "DROP SERIES FROM \"medication\" WHERE \"source\" = 'mytherapy'"]
    assert len(influx.stored("medication")) == 3

    assert runner.run(db, connector).message == "Nothing new."
    assert len([query for _db, query in influx.queries if query.startswith("DROP")]) == 1

    # A dose deleted in the app: gone from the export, gone from the series.
    write_mytherapy_export(export, [mytherapy_row(scheduled="2026-03-01 12:00:00")])
    run = runner.run(db, connector)
    assert run.points == 1 and "own series rebuilt" in run.message
    assert [dict(tags)["source"] for tags, _fields, _ts in influx.stored("medication")] == ["medisafe", "mytherapy"]


def test_a_later_export_replaces_what_the_earlier_one_said(connector, influx, db, tmp_path):
    """The exports are applied oldest first and each replaces the rows inside
    its own date range: a dose that went from unanswered to taken follows the
    newest export."""
    write_mytherapy_export(tmp_path / "morning.csv",
                           [mytherapy_row(scheduled="2026-03-01 12:00:00", status="auto skipped"),
                            mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    evening = write_mytherapy_export(tmp_path / "evening.csv",
                                     [mytherapy_row(scheduled="2026-03-01 12:00:00", actual="2026-03-01 12:30:00"),
                                      mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    os.utime(tmp_path / "morning.csv", (1, 1))  # saved first, so applied first
    os.utime(evening, (2, 2))
    assert runner.run(db, connector).points == 2
    by_ts = {ts: (dict(tags), fields) for tags, fields, ts in influx.stored("medication")}
    assert by_ts[epoch(2026, 3, 1, 11, 0)][0]["status"] == "taken"
    assert by_ts[epoch(2026, 3, 1, 11, 0)][1]["delay_min"] == "30i"
    assert by_ts[epoch(2026, 3, 2, 19, 0)][0]["status"] == "taken"


def test_two_doses_of_one_medication_in_the_same_second_are_a_second_apart(connector, influx, db, tmp_path):
    """The job bumped the second one: two points, the same key otherwise. The
    delay stays the row's own (the job computed it from the row's scheduled
    time, not from the bumped one): the second dose was not answered a minute
    early."""
    late = mytherapy_row(actual="2026-03-01 12:04:00")
    write_mytherapy_export(tmp_path / "export.csv", [late, late])
    runner.run(db, connector)
    points = influx.stored("medication")
    assert [ts for _tags, _fields, ts in points] == [epoch(2026, 3, 1, 11, 0), epoch(2026, 3, 1, 11, 0) + 1]
    assert [fields["delay_min"] for _tags, fields, _ts in points] == ["4i", "4i"]


def test_a_dose_without_a_value_or_unit_and_an_untaken_dose(connector, influx, db, tmp_path):
    """The job wrote neither a zero nor an empty text, and `delay_min` only
    for a dose that was taken."""
    write_mytherapy_export(tmp_path / "export.csv",
                           [mytherapy_row(status="rejected", value="", unit=""),
                            mytherapy_row(name="Second", status="auto skipped", value="1.5", unit="drop(s)")])
    runner.run(db, connector)
    by_status = {tags["status"]: fields for tags, fields, _ts in influx.stored("medication")}
    assert by_status["skipped"] == {"taken": "0i", "app_status": '"rejected"'}
    assert by_status["missed"] == {"taken": "0i", "app_status": '"auto skipped"', "dose": "1.5", "unit": '"drops"'}


def test_weight_reminders_and_doses_before_the_start_date_are_not_imported(connector, influx, db, tmp_path):
    """The job skipped the app's weight reminders (a scale records weight) and
    every dose before its overlap with the other import."""
    connector.settings.start_date = "2026-03-01"
    write_mytherapy_export(tmp_path / "export.csv", [
        mytherapy_row(scheduled="2026-02-27 07:45:00", kind="measurement", unit="kg"),
        mytherapy_row(scheduled="2026-02-28 07:45:00"),
        mytherapy_row(scheduled="2026-03-03 12:00:00"),
    ])
    run = runner.run(db, connector)
    assert run.points == 1
    (tags, _fields, ts), = influx.stored("medication")
    assert tags["status"] == "taken" and ts == epoch(2026, 3, 3, 11, 0)


def test_a_folder_with_no_export_never_empties_the_series(connector, influx, db, tmp_path):
    """The job refused to drop the series with nothing to write back; so does
    the connector, rather than rewrite it empty."""
    export = write_mytherapy_export(tmp_path / "export.csv", [mytherapy_row()])
    assert runner.run(db, connector).points == 1
    export.unlink()
    run = runner.run(db, connector)
    assert run.status == "error" and "holds no MyTherapy export" in run.message
    assert run.message.endswith("its series is left as it is.")
    assert len(influx.stored("medication")) == 1
    assert len([query for _db, query in influx.queries if query.startswith("DROP")]) == 1


def test_an_export_holding_no_dose_is_refused_too(connector, influx, db, tmp_path):
    write_mytherapy_export(tmp_path / "reminders.csv", [mytherapy_row(kind="measurement", unit="kg")])
    run = runner.run(db, connector)
    assert run.status == "error" and "hold no dose to import" in run.message
    assert not [query for _db, query in influx.queries if query.startswith("DROP")]


def test_a_file_that_is_not_an_export_names_the_file(connector, influx, db, tmp_path):
    (tmp_path / "shopping.csv").write_text("item,price\nmilk,1.20\n", encoding="utf-8")
    run = runner.run(db, connector)
    assert run.status == "error"
    assert run.message == "shopping.csv: not a MyTherapy export: columns ['item', 'price']."


@pytest.mark.parametrize("row, message", [
    (mytherapy_row(kind="note"), "unknown type 'note'"),
    (mytherapy_row(status="postponed"), "unknown status 'postponed'"),
    (mytherapy_row(name="Third"), "aliases missing from TRIBUTARY_MYTHERAPY_NAMES"),
])
def test_what_the_job_refused_to_guess_fails_the_run(connector, influx, db, tmp_path, row, message):
    """An unknown type, an unknown status and an alias with no name to map it
    to: the job failed the run, and so does the connector. Nothing is dropped
    or written."""
    write_mytherapy_export(tmp_path / "export.csv", [row])
    run = runner.run(db, connector)
    assert run.status == "error" and message in run.message
    assert influx.stored("medication") == []
    assert not [query for _db, query in influx.queries if query.startswith("DROP")]


def test_the_unmapped_aliases_are_listed_with_their_dose_count(connector, influx, db, tmp_path):
    write_mytherapy_export(tmp_path / "export.csv",
                           [mytherapy_row(name="Third"), mytherapy_row(name="Third"), mytherapy_row(name="Fourth")])
    run = runner.run(db, connector)
    assert "'Fourth' (1 dose(s)), 'Third' (2 dose(s))" in run.message


def test_a_bad_mapping_stops_the_run_not_the_app():
    """The mapping and the start date are read at each run, never at the
    start: an empty value (a missing secret key) is simply no mapping, and a
    mistake fails the runs, naming the setting."""
    assert mytherapy.parse_names(None) == {} and mytherapy.parse_names("  ") == {}
    assert mytherapy.parse_start_date("") is None and mytherapy.parse_start_date(None) is None
    with pytest.raises(mytherapy.ConnectorError, match="TRIBUTARY_MYTHERAPY_NAMES is not valid JSON"):
        mytherapy.parse_names("{not json")
    with pytest.raises(mytherapy.ConnectorError, match="must be a JSON object"):
        mytherapy.parse_names('["First"]')
    with pytest.raises(mytherapy.ConnectorError, match="TRIBUTARY_MYTHERAPY_START_DATE must be a date"):
        mytherapy.parse_start_date("the 12th of February")


def test_the_folder_is_the_only_input(connector):
    """One file alone can't be merged with the exports around it, and the
    series is rebuilt from the folder: no upload form, and the upload route
    refuses it."""
    assert connector.watch_dir is not None and connector.is_configured()
    assert connector.accepts_files is False
    assert connector.rebuild_own_source is True
    section, = [part for part in TestClient(app).get("/").text.split("<section")
                if 'id="mytherapy"' in part]
    assert 'action="/connectors/mytherapy/upload"' not in section
    assert "Watching\n      <code>" in section
    assert f"<code>{connector.watch_dir}</code>: the folder is the source of truth" in section
    assert TestClient(app).post("/api/connectors/mytherapy/upload",
                                files={"file": ("x.csv", b"a")}).status_code == 400


def test_the_declared_series_is_the_jobs():
    """The tags and types the job wrote, and the alias -> name mapping that
    comes from the settings: never from this repository."""
    assert mytherapy.parse_names(json.dumps(MYTHERAPY_NAMES)) == MYTHERAPY_NAMES
    assert set(mytherapy.STATUS) == {"confirmed", "rejected", "auto skipped"}
    assert mytherapy.MEDICATION.tag_keys == ("source", "user", "med", "status")
    assert mytherapy.MEDICATION.field_types == {"taken": int, "app_status": str, "dose": float,
                                                "unit": str, "delay_min": int}
    assert mytherapy.MyTherapyConnector.required_env == ("TRIBUTARY_MYTHERAPY_WATCH_DIR",
                                                         "TRIBUTARY_MYTHERAPY_NAMES")


def test_a_run_reads_every_export_even_a_full_one(connector, influx, db, tmp_path):
    """A full run ignores where the last one stopped: the folder is read whole
    either way, and the same key overwrites itself with the same values."""
    write_mytherapy_export(tmp_path / "export.csv", [mytherapy_row()])
    write_mytherapy_export(tmp_path / "other.csv", [mytherapy_row(name="Second", scheduled="2026-03-04 20:00:00")])
    runner.run(db, connector)
    lines = influx.written_lines()
    assert len(lines) == 2
    runner.run(db, connector, full=True)
    assert influx.written_lines()[-2:] == lines
    assert len(influx.stored("medication")) == 2
    assert {parse(line)[1]["med"] for line in influx.written_lines()} == set(MYTHERAPY_NAMES.values())
