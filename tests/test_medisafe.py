"""Medisafe: the exact series the one-off import script wrote (docs/porting.md),
from an export imported in the UI."""
import csv
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import runner, schema
from app.config import settings
from app.connectors import medisafe
from app.connectors.base import ConnectorError
from app.main import app
from tests.fakes import MEDISAFE_COLUMNS, MEDISAFE_NOTE, epoch, medisafe_row, write_medisafe_export

# The `user` tag: the script's --user, TRIBUTARY_INFLUXDB_USER here.
USER = "alice"
# Where a file that isn't the export sends the user.
THE_EXPORT = "The doses export is Doses_<account>_000.csv, or the zip holding it."


@pytest.fixture()
def connector(medisafe_connector, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return medisafe_connector


def _import(db, connector, tmp_path, rows, name="Doses_00012345_000.csv", zipped=False, **options):
    """Import an export as the UI's upload does."""
    path = write_medisafe_export(tmp_path / name, rows, zipped=zipped)
    return runner.run(db, connector, file=str(path), filename=name, trigger="upload", **options)


def _drops(influx):
    return [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def _upload(db, connector, tmp_path, text, name="000 (1).csv"):
    """Upload a file that holds `text`, whatever it is, as the UI does."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return runner.run(db, connector, file=str(path), filename=name, trigger="upload")


def _resaved(path, rows, delimiter, header):
    """The export as a tool re-saves it: `header` -- (the name written, the
    export's column), in the order written -- over its rows, `delimiter`
    between the columns, behind a BOM."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=delimiter)
    writer.writerow([written for written, _column in header])
    writer.writerows([row[column] for _written, column in header] for row in rows)
    path.write_text(out.getvalue(), encoding="utf-8-sig")
    return path


def test_an_import_writes_the_scripts_exact_lines(connector, influx, db, tmp_path):
    """One point per dose at its scheduled time, UTC as the export gives it,
    with the script's fields and types; a dose not taken has no delay."""
    run = _import(db, connector, tmp_path, [
        medisafe_row(scheduled="2026-01-15 11:00:00", recorded="2026-01-15 11:05:00", strength="10 mg"),
        medisafe_row(scheduled="2026-01-15 19:30:00", recorded="2026-01-15 21:00:00", status="missed",
                     dosage="0.5", unit="ml"),
    ])
    assert run.status == "ok" and run.points == 2
    # The account number of the file's name is masked (stored, shown, logged).
    assert run.message == ("Imported Doses_<account>_000.csv: 2 point(s) written (BodyStats 2), "
                           "after dropping its own series from 2026-01-15 to 2026-01-15.")
    assert influx.written_lines() == [
        f'medication,med=Alpha\\ med,source=medisafe,status=taken,user={USER} app_status="taken",delay_min=5i,'
        f'dose=1.0,strength="10 mg",taken=1i,unit="pills" {epoch(2026, 1, 15, 11, 0)}',
        f'medication,med=Alpha\\ med,source=medisafe,status=missed,user={USER} app_status="missed",dose=0.5,'
        f'taken=0i,unit="ml" {epoch(2026, 1, 15, 19, 30)}',
    ]


def test_times_are_the_exports_utc_whatever_the_time_zone(connector, influx, db, tmp_path):
    """The export's times are UTC though it doesn't say so: a 12:00 Paris dose
    is 11:00 in winter and 10:00 in summer, and stays where the export puts
    it."""
    _import(db, connector, tmp_path, [medisafe_row(scheduled="2026-01-15 11:00:00", recorded="2026-01-15 11:00:00"),
                                      medisafe_row(scheduled="2026-07-15 10:00:00", recorded="2026-07-15 10:00:00")])
    assert [ts for _t, _f, ts in influx.stored("medication")] == [epoch(2026, 1, 15, 11, 0), epoch(2026, 7, 15, 10, 0)]


def test_statuses_map_to_the_contract_and_keep_the_apps_word(connector, influx, db, tmp_path):
    """dismissed -> skipped; pending, snooze and deleted (a reminder nobody
    answered, or a dose the user removed) -> unlogged; the app's word stays in
    app_status."""
    rows = [medisafe_row(scheduled=f"2026-01-1{i} 08:00:00", status=status)
            for i, status in enumerate(("taken", "dismissed", "missed", "pending", "snooze", "deleted"))]
    _import(db, connector, tmp_path, rows)
    stored = [(tags["status"], fields["app_status"], fields["taken"], "delay_min" in fields)
              for tags, fields, _ts in influx.stored("medication")]
    assert stored == [("taken", '"taken"', "1i", True), ("skipped", '"dismissed"', "0i", False),
                      ("missed", '"missed"', "0i", False), ("unlogged", '"pending"', "0i", False),
                      ("unlogged", '"snooze"', "0i", False), ("unlogged", '"deleted"', "0i", False)]


def test_a_deleted_dose_is_a_point_in_the_no_record_bucket(connector, influx, db, tmp_path):
    """A dose the app reports as deleted (removed in the app) is kept, not
    dropped: written exactly as a dose nobody answered, status=unlogged and
    taken=0, with the app's own word in app_status and no delay."""
    _import(db, connector, tmp_path, [medisafe_row(status="deleted")])
    assert influx.written_lines() == [
        f'medication,med=Alpha\\ med,source=medisafe,status=unlogged,user={USER} app_status="deleted",'
        f'dose=1.0,taken=0i,unit="pills" {epoch(2026, 1, 15, 11, 0)}',
    ]


def test_an_unknown_status_fails_the_import(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path, [medisafe_row(status="postponed")])
    assert run.status == "error" and "unknown dose_status 'postponed'" in run.message
    assert influx.stored("medication") == [] and _drops(influx) == []


def test_the_delay_is_floored_minutes_from_the_scheduled_time(connector, influx, db, tmp_path):
    """Recorded 30 s early is -1 min (floored, as the script's // did)."""
    _import(db, connector, tmp_path, [medisafe_row(scheduled="2026-01-15 08:00:00", recorded="2026-01-15 07:59:30")])
    (_tags, fields, _ts), = influx.stored("medication")
    assert fields["delay_min"] == "-1i"


def test_two_doses_in_one_second_are_a_second_apart(connector, influx, db, tmp_path):
    """A dose change makes a new schedule under the same name: its first dose
    can share the old one's slot. Both stay, a second apart, as the script
    wrote them; the delay is still from the scheduled time."""
    _import(db, connector, tmp_path, [
        medisafe_row(scheduled="2026-01-15 08:00:00", recorded="2026-01-15 08:10:00"),
        medisafe_row(scheduled="2026-01-15 08:00:00", recorded="2026-01-15 08:20:00"),
    ])
    stored = [(fields["delay_min"], ts) for _tags, fields, ts in influx.stored("medication")]
    assert stored == [("10i", epoch(2026, 1, 15, 8, 0)), ("20i", epoch(2026, 1, 15, 8, 0) + 1)]


def test_names_are_renamed_or_left_out_per_the_mapping(connector, influx, db, tmp_path, monkeypatch):
    """TRIBUTARY_MEDISAFE_NAMES: an export name -> the stored name, or null to
    leave it out; a name not in it is stored as the export spells it. A
    renamed medication that meets another's slot is a second apart."""
    monkeypatch.setattr(connector.settings, "names", json.dumps({"Old drops": "Alpha med", "Syrup": None}))
    run = _import(db, connector, tmp_path, [
        medisafe_row(name="Alpha med", scheduled="2026-01-15 08:00:00"),
        medisafe_row(name="Old drops", scheduled="2026-01-15 08:00:00"),
        medisafe_row(name="Syrup", scheduled="2026-01-16 08:00:00"),
        medisafe_row(name="Beta med", scheduled="2026-01-17 08:00:00"),
    ])
    assert run.points == 3
    assert [(tags["med"], ts) for tags, _f, ts in influx.stored("medication")] == [
        ("Alpha med", epoch(2026, 1, 15, 8, 0)), ("Alpha med", epoch(2026, 1, 15, 8, 0) + 1),
        ("Beta med", epoch(2026, 1, 17, 8, 0))]


@pytest.mark.parametrize("raw, message", [
    ("{nope", "TRIBUTARY_MEDISAFE_NAMES is not valid JSON"),
    ('["a"]', "must be a JSON object"),
    ('{"a": ""}', "each value is a name, or null"),
])
def test_a_bad_mapping_fails_the_import(connector, influx, db, tmp_path, monkeypatch, raw, message):
    monkeypatch.setattr(connector.settings, "names", raw)
    run = _import(db, connector, tmp_path, [medisafe_row()])
    assert run.status == "error" and message in run.message
    assert _drops(influx) == []


def test_the_note_is_written_only_when_asked_for_and_whole(connector, influx, db, tmp_path, monkeypatch):
    """A dose's note is free text the user typed: TRIBUTARY_MEDISAFE_NOTES
    opts in (the script wrote it, and the Medication dashboard shows it),
    and then whole, as the script wrote it."""
    rows = [medisafe_row(notes=MEDISAFE_NOTE + " " + "x" * 300)]
    _import(db, connector, tmp_path, rows)
    (_tags, fields, _ts), = influx.stored("medication")
    assert "note" not in fields
    monkeypatch.setattr(connector.settings, "notes", True)
    _import(db, connector, tmp_path, rows)
    (_tags, fields, _ts), = influx.stored("medication")
    assert fields["note"] == f'"{MEDISAFE_NOTE} {"x" * 300}"'


def test_an_import_replaces_its_own_series_over_the_days_it_covers_only(connector, influx, db, tmp_path):
    """Its first dose to its last (local days), source=medisafe and this user
    only, as the script's --replace was per user: a dose whose status changed
    goes; the days outside the export, MyTherapy's doses and another user's
    stay."""
    def point(source, user, ts, status="taken"):
        influx.points[("BodyStats", "medication", (("med", "Alpha med"), ("source", source), ("status", status),
                                                   ("user", user)), ts)] = {"taken": "0i"}
    before, inside, after = epoch(2026, 1, 9, 8, 0), epoch(2026, 1, 12, 8, 0), epoch(2026, 1, 16, 8, 0)
    for ts in (before, inside, after):
        point("medisafe", USER, ts, status="unlogged")
    point("mytherapy", USER, inside)
    point("medisafe", "bob", inside)
    run = _import(db, connector, tmp_path, [medisafe_row(scheduled="2026-01-10 08:00:00"),
                                            medisafe_row(scheduled="2026-01-15 08:00:00")])
    assert run.message.endswith("after dropping its own series from 2026-01-10 to 2026-01-15.")
    # Local midnights (Paris, CET in January).
    assert _drops(influx) == [f'DELETE FROM "medication" WHERE "source" = \'medisafe\' AND "user" = \'{USER}\' '
                              "AND time >= '2026-01-09T23:00:00Z' AND time < '2026-01-15T23:00:00Z'"]
    assert sorted((tags["source"], tags["user"], tags["status"], ts) for tags, _f, ts in influx.stored("medication")) == [
        ("medisafe", USER, "taken", epoch(2026, 1, 10, 8, 0)), ("medisafe", USER, "taken", epoch(2026, 1, 15, 8, 0)),
        ("medisafe", USER, "unlogged", before), ("medisafe", USER, "unlogged", after),
        ("medisafe", "bob", "taken", inside), ("mytherapy", USER, "taken", inside)]


def test_the_span_counts_the_doses_left_out_but_never_fails_on_them(connector, tmp_path, monkeypatch):
    """The export covers its left-out medications' days too; their rows are
    not read otherwise (as the script skipped them), so an odd one is no
    error."""
    monkeypatch.setattr(connector.settings, "names", json.dumps({"Syrup": None}))
    path = write_medisafe_export(tmp_path / "d.csv", [medisafe_row(name="Syrup", scheduled="2026-01-01 08:00:00"),
                                                      medisafe_row(name="Syrup", scheduled="", status="odd"),
                                                      medisafe_row(scheduled="2026-01-05 08:00:00")])
    imported = connector.import_file(path)
    assert [str(day) for day in imported.spans["medication"]] == ["2026-01-01", "2026-01-05"]


def test_doses_after_the_end_date_are_not_imported_nor_replaced(connector, influx, db, tmp_path, monkeypatch):
    """TRIBUTARY_MEDISAFE_END_DATE: the day another app took over. A later
    dose (a reminder left running) is not written, and the replace stops at
    that day, so the other app's doses are never doubled."""
    monkeypatch.setattr(connector.settings, "end_date", "2026-02-11")
    run = _import(db, connector, tmp_path, [
        medisafe_row(scheduled="2026-02-10 08:00:00"),
        medisafe_row(scheduled="2026-02-11 22:30:00"),               # 23:30 in Paris: still the 11th
        medisafe_row(scheduled="2026-02-11 23:30:00", status="missed"),  # 00:30 on the 12th in Paris
    ])
    assert run.points == 2
    assert run.message.endswith("after dropping its own series from 2026-02-10 to 2026-02-11.")


def test_a_bad_end_date_fails_the_import(connector, influx, db, tmp_path, monkeypatch):
    monkeypatch.setattr(connector.settings, "end_date", "the 11th of February")
    run = _import(db, connector, tmp_path, [medisafe_row()])
    assert run.status == "error" and "TRIBUTARY_MEDISAFE_END_DATE must be a date" in run.message


def test_a_blank_dosage_or_record_time_is_left_out_not_an_error(connector, influx, db, tmp_path):
    """The user can't fix the app's export: a blank dosage writes no dose, a
    taken dose without a record time no delay."""
    _import(db, connector, tmp_path, [medisafe_row(dosage=" ", recorded="")])
    (_tags, fields, _ts), = influx.stored("medication")
    assert "dose" not in fields and "delay_min" not in fields and fields["taken"] == "1i"


def test_without_a_user_tag_an_import_is_refused(medisafe_connector, influx, db, tmp_path):
    """The replace is per user: without TRIBUTARY_INFLUXDB_USER it would reach
    every user's doses, and rewrite them with no user tag."""
    path = write_medisafe_export(tmp_path / "d.csv", [medisafe_row()])
    run = runner.run(db, medisafe_connector, file=str(path), filename="d.csv", trigger="upload")
    assert run.status == "error" and "TRIBUTARY_INFLUXDB_USER is not set" in run.message
    assert _drops(influx) == [] and influx.written_lines() == []


def test_a_dry_run_drops_nothing(connector, influx, db, tmp_path):
    path = write_medisafe_export(tmp_path / "d.csv", [medisafe_row()])
    run = runner.run(db, connector, dry_run=True, file=str(path), filename="d.csv", trigger="upload")
    assert run.status == "ok" and run.points == 1
    assert "medication,med=Alpha\\ med,source=medisafe" in run.preview
    assert influx.written_lines() == [] and _drops(influx) == []


def test_a_zip_holding_the_csv_is_read(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path, [medisafe_row()], name="export.zip", zipped=True)
    assert run.status == "ok" and run.points == 1


def test_a_zip_of_parts_is_read_as_one_export(connector, influx, db, tmp_path):
    """Doses_<account>_000.csv, _001...: one export, read in order (the
    one-second shift depends on it), so a part never replaces another's
    days on its own."""
    first = write_medisafe_export(tmp_path / "a.csv", [medisafe_row(scheduled="2026-01-15 08:00:00",
                                                                    recorded="2026-01-15 08:10:00")])
    second = write_medisafe_export(tmp_path / "b.csv", [medisafe_row(scheduled="2026-01-15 08:00:00",
                                                                     recorded="2026-01-15 08:20:00")])
    path = tmp_path / "parts.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Doses_1_001.csv", second.read_text(encoding="utf-8"))
        archive.writestr("Doses_1_000.csv", first.read_text(encoding="utf-8"))
        archive.writestr("__MACOSX/._Doses_1_000.csv", b"\x00\x05")
    run = runner.run(db, connector, file=str(path), filename="parts.zip", trigger="upload")
    assert run.status == "ok" and run.points == 2, run.message
    assert [(f["delay_min"], ts) for _t, f, ts in influx.stored("medication")] == [
        ("10i", epoch(2026, 1, 15, 8, 0)), ("20i", epoch(2026, 1, 15, 8, 0) + 1)]


def test_a_zip_without_a_doses_csv_is_refused(connector, influx, db, tmp_path):
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Report.csv", "name\n")
    run = runner.run(db, connector, file=str(path), filename="other.zip", trigger="upload")
    assert run.status == "error" and "no Doses_*.csv in the archive" in run.message


def test_a_file_that_is_not_an_export_names_what_is_missing(connector, influx, db, tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("name,when\nx,2026-01-01\n", encoding="utf-8")
    run = runner.run(db, connector, file=str(path), filename="other.csv", trigger="upload")
    assert run.status == "error" and "not a Medisafe doses export: no column 'Scheduled for'" in run.message


A_DOSE = ",".join(medisafe_row(notes=MEDISAFE_NOTE).values()) + "\n"


@pytest.mark.parametrize("text, says", [
    ("", "the file is empty."),
    ("\r\n  \n", "the file is empty."),
    ("<!DOCTYPE html>\n<html><head><title>Link expired</title></head></html>\n",
     "the file is a web page (HTML or XML), not a CSV: a download that failed or expired? "
     "Download the export again."),
    ('<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code></Error>\n',
     "the file is a web page (HTML or XML), not a CSV"),
    ("Medication,Amount,Measure,Taken at,Outcome,Comment,Reason\n" + A_DOSE,
     f"its columns are 'Medication', 'Amount', 'Measure', 'Taken at', 'Outcome' and 2 more, none of them a "
     f"doses export's. {THE_EXPORT}"),
    ("|".join(MEDISAFE_COLUMNS) + "\n", f"its first line has no comma, semicolon or tab to separate columns. "
                                        f"{THE_EXPORT}"),
    (A_DOSE + A_DOSE, f"its first line (10 columns) doesn't read as column names. {THE_EXPORT}"),
])
def test_a_file_that_is_not_an_export_says_what_it_holds(connector, influx, db, tmp_path, text, says):
    """Not one of the export's columns: the run says what the file is instead
    -- empty, a web page (a download that failed), another CSV (its column
    names, capped) or a first line that isn't column names --, so the user can
    tell which file they picked, and where the right one is."""
    run = _upload(db, connector, tmp_path, text)
    assert run.status == "error" and run.message.startswith(f"000 (1).csv: not a Medisafe doses export: {says}")
    assert _drops(influx) == [] and influx.written_lines() == []


@pytest.mark.parametrize("first_line", [
    A_DOSE,                                               # the export without its header: a dose
    "Medication report of Alpha med\n",                   # a title
    "Medication,Alpha med\n",                             # a label and its value
    f"Alpha med,pills,{MEDISAFE_NOTE} taken with food\n",  # a note: longer than a column name
    "Alpha med,pills,alice@example.com\n",                # an address
    "Alpha med,pills,with food\nBeta med,drops,at night\n",  # a list of names, no header
])
def test_a_first_line_that_may_be_data_is_described_never_quoted(connector, influx, db, tmp_path, first_line):
    """A run's message is stored, shown and logged: the first line is quoted
    only when it reads as column names, never when it may be a row (a name, a
    note)."""
    run = _upload(db, connector, tmp_path, first_line + A_DOSE)
    assert run.status == "error" and "its first line" in run.message
    assert "Alpha" not in run.message and MEDISAFE_NOTE not in run.message and "@" not in run.message


@pytest.mark.parametrize("delimiter, header", [
    (";", [(column, column) for column in MEDISAFE_COLUMNS]),   # a spreadsheet's re-save, French locale
    ("\t", [(column, column) for column in MEDISAFE_COLUMNS]),
    (",", [("\ufeffName", "name")] + [(f" {column.upper()} ", column)  # a second BOM, padded, cased
                                        for column in MEDISAFE_COLUMNS if column != "name"]),
])
def test_a_resaved_export_is_read_as_the_export(connector, tmp_path, delimiter, header):
    """A semicolon or a tab between the columns, their names padded, cased
    or behind a second BOM: a tool's re-save, read as the export it was."""
    rows = [medisafe_row(scheduled="2026-01-15 11:00:00", recorded="2026-01-15 11:05:00", strength="10 mg",
                         notes=MEDISAFE_NOTE),
            medisafe_row(scheduled="2026-01-16 19:30:00", status="missed", dosage="0.5", unit="ml")]
    exported = connector.import_file(write_medisafe_export(tmp_path / "Doses_1_000.csv", rows))
    resaved = connector.import_file(_resaved(tmp_path / "resaved.csv", rows, delimiter, header))
    assert len(resaved.points) == 2
    assert resaved.points == exported.points and resaved.spans == exported.spans


def test_an_exact_name_is_read_first_and_a_doubtful_one_never_guessed(connector, tmp_path):
    """The export's own names win, so it is read as it always was; a column
    two names could be (`Name`, `NAME`) is missing rather than guessed."""
    rows = [medisafe_row()]
    exact = [("name", "name"), ("Name", "unit")] + [(c, c) for c in MEDISAFE_COLUMNS if c != "name"]
    imported = connector.import_file(_resaved(tmp_path / "exact.csv", rows, ",", exact))
    assert [p.tags["med"] for p in imported.points] == ["Alpha med"]
    doubtful = [("Name", "name"), ("NAME", "unit")] + [(c, c) for c in MEDISAFE_COLUMNS if c != "name"]
    with pytest.raises(ConnectorError, match=r"^not a Medisafe doses export: no column 'name'\.$"):
        connector.import_file(_resaved(tmp_path / "doubtful.csv", rows, ",", doubtful))


def test_an_export_holding_no_dose_is_refused_rather_than_empty_the_series(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path, [])
    assert run.status == "error" and "holds no dose to import" in run.message
    assert _drops(influx) == []


def test_the_declared_series_is_the_scripts(connector):
    assert medisafe.MedisafeConnector.key == "medisafe"
    assert medisafe.MEDICATION is schema.MEDICATION  # shared with MyTherapy
    assert medisafe.MEDICATION.tag_keys == ("source", "user", "med", "status")
    assert medisafe.MEDICATION.field_types == {"taken": int, "app_status": str, "dose": float, "unit": str,
                                               "delay_min": int, "strength": str, "note": str}
    assert medisafe.MedisafeConnector.rebuild_own_source is True
    assert connector.env_prefix == "TRIBUTARY_MEDISAFE_"


def test_it_takes_an_upload_in_the_ui_and_never_watches_a_folder(connector, tmp_path):
    """No API and no schedule; an import replaces the days its export covers,
    which a watched folder's whole-source rebuild doesn't do."""
    assert connector.is_configured() is False
    assert connector.accepts_files is True and connector.file_types == (".zip", ".csv")
    connector.settings.watch_dir = str(tmp_path)
    try:
        assert connector.watch_dir is None
    finally:
        connector.settings.watch_dir = None
    page = TestClient(app).get("/").text
    section, = [part for part in page.split("<section") if 'id="medisafe"' in part]
    assert 'action="/connectors/medisafe/check"' in section
