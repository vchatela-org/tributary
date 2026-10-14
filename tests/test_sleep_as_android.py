"""Sleep as Android: the exact series the one-off import script wrote
(docs/porting.md), from a backup imported in the UI."""
import zipfile
from pathlib import Path

import pytest

from app import runner
from app.config import settings
from tests.fakes import epoch, parse, sleep_record, write_sleep_export

# The `user` tag: the script's --user, TRIBUTARY_INFLUXDB_USER here.
USER = "alice"

# 2026-03-01 22:00 UTC = 23:00 in Paris (CET); the night ends 07:00 local.
START_TS = epoch(2026, 3, 1, 22, 0)
START_MS = START_TS * 1000
END_TS = epoch(2026, 3, 2, 6, 0)

# A whole night: an hour each of light and deep, then 30 min rem and 10 min
# awake, an hour of untracked tail, and three vitals samples.
EVENTS = [
    f"LIGHT_START-{START_MS}", f"LIGHT_END-{START_MS + 3_600_000}",
    f"DEEP_START-{START_MS + 3_600_000}", f"DEEP_END-{START_MS + 7_200_000}",
    f"REM_START-{START_MS + 7_200_000}", f"REM_END-{START_MS + 9_000_000}",
    f"AWAKE_START-{START_MS + 9_000_000}", f"AWAKE_END-{START_MS + 9_600_000}",
    f"HR-{START_MS + 600_000}-60", f"HR-{START_MS + 1_200_000}-58",
    f"SPO2-{START_MS + 600_000}-96", f"SDNN-{START_MS + 600_000}-40",
]


@pytest.fixture()
def connector(sleep_as_android_connector, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return sleep_as_android_connector


def _import(db, connector, tmp_path, records=None, name="sleep-export.zip", zipped=True, **options):
    """Import a backup as the UI's upload does."""
    if records is None:
        records = [sleep_record(rating="4", comment="#alcohol #late", events=EVENTS)]
    path = write_sleep_export(tmp_path / name, records, zipped=zipped)
    return runner.run(db, connector, file=str(path), filename=name, trigger="upload", **options)


def test_an_import_writes_the_scripts_exact_lines(connector, influx, db, tmp_path):
    """One point per sleep (at its end), stage segment (at its start) and vitals
    sample (at its time), with the script's fields and types: the session's
    integer seconds and minute-of-day, its floats, the app's SDNN (not RMSSD),
    a boolean `manual`, and the comment's hashtags as text."""
    run = _import(db, connector, tmp_path)
    assert run.status == "ok" and run.points == 7
    assert run.message == ("Imported sleep-export.zip, 1 session(s) (1 night(s), 0 nap(s)), "
                           "4 stage segment(s), 2 vitals sample(s): 7 point(s) written (SleepStats 7), "
                           "after dropping its own series.")
    assert sorted(influx.written_lines()) == sorted([
        f'sleep_session,kind=night,source=sleep_as_android,user={USER} '
        f'asleep_s=28800i,avg_hr=59.0,avg_spo2_pct=96.0,awake_s=600i,bedtime_min=-60i,cycles=3i,'
        f'deep_pct=20.0,deep_s=3600i,in_bed_s=28800i,light_s=3600i,manual=false,max_hr=60.0,'
        f'min_hr=58.0,rating=4.0,rem_s=1800i,sdnn_ms=40.0,start={START_TS}i,tags="#alcohol #late",'
        f'tz="Europe/Paris",tz_offset_min=60i,wake_min=420i {END_TS}',
        f'sleep_stage,source=sleep_as_android,user={USER} duration_s=3600i,level=1i,stage="light" {START_TS}',
        f'sleep_stage,source=sleep_as_android,user={USER} duration_s=3600i,level=0i,stage="deep" {START_TS + 3600}',
        f'sleep_stage,source=sleep_as_android,user={USER} duration_s=1800i,level=2i,stage="rem" {START_TS + 7200}',
        f'sleep_stage,source=sleep_as_android,user={USER} duration_s=600i,level=3i,stage="awake" {START_TS + 9000}',
        f'sleep_vitals,source=sleep_as_android,user={USER} hr_bpm=60.0,sdnn_ms=40.0,spo2_pct=96.0 {START_TS + 600}',
        f'sleep_vitals,source=sleep_as_android,user={USER} hr_bpm=58.0 {START_TS + 1200}',
    ])


def test_a_short_daytime_session_is_a_nap(connector, influx, db, tmp_path):
    """The app has no nap flag: a short session starting in the daytime is a nap
    (a 07:00-10:00 short one stays night)."""
    _import(db, connector, tmp_path,
            [sleep_record(record_id=epoch(2026, 3, 1, 13, 0) * 1000, hours="1.5", deepsleep="-1")])
    (_line,) = influx.written_lines()
    _measurement, tags, fields, ts = parse(_line)
    assert tags["kind"] == "nap"
    # 14:00 Paris (13:00 UTC) to 15:30, so the point is at 14:30 UTC.
    assert ts == epoch(2026, 3, 1, 14, 30) and fields["wake_min"] == "930i" and fields["bedtime_min"] == "-600i"


def test_a_hand_entered_session_is_marked_manual(connector, influx, db, tmp_path):
    """Cycles == -1 means the session was entered by hand: `manual` is true and
    no cycle count is written (the script wrote it that way)."""
    _import(db, connector, tmp_path, [sleep_record(cycles="-1", deepsleep="-1")])
    (_line,) = influx.written_lines()
    _measurement, _tags, fields, _ts = parse(_line)
    assert fields["manual"] == "true" and "cycles" not in fields and "deep_pct" not in fields


def test_the_tags_are_always_written_and_the_note_only_when_asked(connector, influx, db, tmp_path):
    """The comment's hashtags are a vocabulary the dashboard filters on, always
    written; its free text is `note`, only with TRIBUTARY_SLEEP_AS_ANDROID_NOTES."""
    _import(db, connector, tmp_path,
            [sleep_record(comment="#alcohol a private word", deepsleep="-1")], name="off.zip")
    (_off,) = influx.written_lines()
    assert parse(_off)[2]["tags"] == '"#alcohol"' and "note" not in parse(_off)[2]

    connector.settings.notes = True
    _import(db, connector, tmp_path,
            [sleep_record(comment="#alcohol a private word", deepsleep="-1")], name="on.zip")
    _on = influx.written_lines()[-1]
    assert parse(_on)[2]["note"] == '"a private word"' and parse(_on)[2]["tags"] == '"#alcohol"'


def test_a_bare_csv_is_read_too(connector, influx, db, tmp_path):
    """The export is a zip holding sleep-export.csv, but the CSV alone works too."""
    run = _import(db, connector, tmp_path, name="sleep-export.csv", zipped=False)
    assert run.status == "ok" and run.points == 7


def test_an_empty_backup_fails_rather_than_replace_the_series(connector, db, tmp_path):
    """An import that holds nothing to write back fails: a rebuild must never
    leave the connector's own series empty. (A dry run: it reads the file the
    same way without needing InfluxDB.)"""
    run = _import(db, connector, tmp_path, [], dry_run=True)
    assert run.status == "error" and "holds no session" in run.message


def test_a_zip_without_the_export_fails(connector, db, tmp_path):
    path = tmp_path / "not-a-backup.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("prefs.xml", "<x/>")
    run = runner.run(db, connector, file=str(path), filename="not-a-backup.zip", trigger="upload", dry_run=True)
    assert run.status == "error" and "sleep-export.csv" in run.message


def test_millisecond_times_are_floored_to_the_second(connector, influx, db, tmp_path):
    """The export stamps milliseconds; Tributary writes whole seconds, so an end
    at ...:00.900 lands on ...:00 (floored, not rounded)."""
    _import(db, connector, tmp_path, [sleep_record(record_id=START_MS + 900, deepsleep="-1")])
    (_line,) = influx.written_lines()
    assert parse(_line)[3] == END_TS


def test_two_vitals_in_one_second_keep_the_later_sample(connector, influx, db, tmp_path):
    """Two samples that fold into the same second take the later one's value,
    not their sum (last_wins), as InfluxDB's own write did."""
    events = [f"HR-{START_MS + 600_000}-60", f"HR-{START_MS + 600_900}-70"]
    _import(db, connector, tmp_path, [sleep_record(deepsleep="-1", events=events)])
    vitals = [line for line in influx.written_lines() if line.startswith("sleep_vitals")]
    assert len(vitals) == 1
    assert parse(vitals[0])[2] == {"hr_bpm": "70.0"}
