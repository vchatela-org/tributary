"""The Whoop connector: the API's mapping (written by hand from WHOOP's public
samples and checked line by line), score_state, the cursor and its pins, the
lookback, rate limits, and the export merged onto the API's points. OAuth and
the secret key are tests/test_oauth.py's."""
import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from app import oauth, runner, schedule
from app.config import settings
from app.connectors import whoop, whoop_api, whoop_export
from app.connectors.base import ConnectorError
from app.points import encode
from app.schema import DAILY_ACTIVITY, EXERCISE, SLEEP_SESSION
from tests.fakes import (WHOOP_CYCLES_HEADER, WHOOP_EXPORT, WHOOP_JOURNAL_HEADER, WHOOP_NOTE, WHOOP_SLEEPS_HEADER,
                         parse, write_whoop_export)

PARIS = ZoneInfo("Europe/Paris")

# Written by hand from the fixtures (tests/fakes.py), WHOOP's own sample
# numbers: times to the second, ms -> s rounded, kJ / 4.184 to 0.1 kcal, the
# local minutes in the record's own offset (+02:00), sorted fields.
NIGHT = ("sleep_session,kind=night,source=whoop,user=alice asleep_s=27416i,avg_spo2_pct=95.6875,awake_s=1404i,"
         "awakenings=12i,bedtime_min=-50i,calibrating=0i,consistency_pct=90.0,cycles=3i,deep_s=6630i,"
         "efficiency_pct=91.69533848,hrv_rmssd_ms=31.813562,in_bed_s=30273i,light_s=14906i,need_baseline_s=27396i,"
         "need_debt_s=352i,need_nap_s=-12i,need_strain_s=209i,no_data_s=0i,recovery_score=44.0,rem_s=5880i,"
         "respiration_rate=16.11328125,resting_hr=64.0,score=98.0,skin_temp_c=33.7,start=1790889000i,"
         "tz_offset_min=120i,wake_min=445i 1790918744")
NAP = ("sleep_session,kind=nap,source=whoop,user=alice asleep_s=1600i,awake_s=230i,awakenings=1i,bedtime_min=-570i,"
       "cycles=0i,deep_s=400i,efficiency_pct=87.4,in_bed_s=1830i,light_s=1200i,need_baseline_s=0i,need_debt_s=0i,"
       "need_nap_s=0i,need_strain_s=0i,no_data_s=0i,rem_s=0i,respiration_rate=15.5,score=30.0,start=1790944200i,"
       "tz_offset_min=120i,wake_min=900i 1790946030")
UNSCORABLE = ("sleep_session,kind=night,source=whoop,user=alice bedtime_min=0i,start=1790632800i,tz_offset_min=120i,"
              "wake_min=360i 1790654400")
CYCLE = ("daily_activity,source=whoop,user=alice avg_hr=68.0,duration_s=87600i,energy_kcal=1981.0,max_hr=141.0,"
         "steps=8234i,strain=12.4 1790889000")
OPEN_CYCLE = ("daily_activity,source=whoop,user=alice avg_hr=61.0,energy_kcal=1000.0,max_hr=120.0,steps=3021i,"
              "strain=5.2951527 1790976600")
RUN = ('exercise,category=run,source=whoop,user=alice activity="running",altitude_change_m=-0.781372010707855,'
       "altitude_gain_m=46.64384460449,avg_hr=123.0,distance_m=1772.77035916,duration_s=2730i,energy_kcal=375.1,"
       "max_hr=146.0,percent_recorded=100.0,strain=8.2463,zone0_s=300i,zone1_s=600i,zone2_s=900i,zone3_s=900i,"
       "zone4_s=600i,zone5_s=300i 1790956800")
YOGA = 'exercise,category=other,source=whoop,user=alice activity="yoga",duration_s=1800i 1790838000'

# The export of tests/fakes.py, merged onto the points above.
NIGHT_FROM_EXPORT = "sleep_session,kind=night,source=whoop,user=alice sleep_debt_s=360i,sleep_need_s=27900i 1790918744"
RUN_FROM_EXPORT = ("exercise,category=run,source=whoop,user=alice gps_enabled=1i,zone1_pct=20.0,zone2_pct=30.0,"
                   "zone3_pct=30.0,zone4_pct=15.0,zone5_pct=5.0 1790956800")
OLD_NIGHT = ("sleep_session,kind=night,source=whoop,user=alice asleep_s=25200i,avg_spo2_pct=97.5,awake_s=2700i,"
             "bedtime_min=-60i,consistency_pct=81.0,deep_s=6600i,efficiency_pct=90.0,hrv_rmssd_ms=48.0,"
             "in_bed_s=27900i,light_s=13200i,recovery_score=61.0,rem_s=5400i,respiration_rate=14.2,resting_hr=50.0,"
             "score=85.0,skin_temp_c=34.5,sleep_debt_s=1200i,sleep_need_s=28800i,start=1773093600i,tz_offset_min=60i,"
             "wake_min=405i 1773121500")
OLD_CYCLE = ("daily_activity,source=whoop,user=alice avg_hr=66.0,duration_s=85500i,energy_kcal=2100.0,max_hr=150.0,"
             "strain=9.8 1773093600")
OLD_RIDE = ('exercise,category=bike,source=whoop,user=alice activity="Cycling",avg_hr=135.0,duration_s=2400i,'
            "energy_kcal=450.0,gps_enabled=0i,max_hr=160.0,strain=10.1,zone1_pct=10.0,zone2_pct=40.0,"
            "zone3_pct=30.0,zone4_pct=15.0,zone5_pct=5.0 1773162000")
JOURNAL = ["journal,question=Have\\ any\\ alcoholic\\ drinks?,source=whoop,user=alice answered_yes=0i,count=1i "
           "1773093600",
           "journal,question=Have\\ any\\ alcoholic\\ drinks?,source=whoop,user=alice answered_yes=1i,count=1i "
           "1790889000",
           # A question the member wrote: written too (the owner's decision).
           "journal,question=Read\\ in\\ bed\\ before\\ sleeping?,source=whoop,user=alice answered_yes=0i,count=1i "
           "1790889000"]


def lines_of(points, user="alice"):
    return [line for lines in encode(points, "whoop", user).values() for line in lines]


def stored_line(influx, measurement, ts):
    for tags, fields, at in influx.stored(measurement):
        if at == ts:
            return tags, fields
    return None


@pytest.fixture()
def alice(monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", "alice")


# -- Mapping ----------------------------------------------------------------------------

def test_a_run_writes_each_record_as_the_contract_says(whoop_connected, whoop_connector):
    result = whoop_connector.sync(None)
    assert sorted(lines_of(result.points)) == sorted([NIGHT, NAP, UNSCORABLE, CYCLE, OPEN_CYCLE, RUN, YOGA])
    assert result.summary == ["2 night(s), 1 nap(s), 1 recovery score(s), 2 cycle(s) and 2 workout(s) over the "
                              "whole history", "2 awaiting their score (read again by the next run)"]


def test_score_state_scored_pending_and_unscorable():
    """SCORED carries its score; PENDING_SCORE is not written yet; UNSCORABLE
    writes what needs no score, never a score field."""
    sleep = copy.deepcopy(whoop_sleep("sleep-night-1"))
    assert "score" in whoop.sleep_fields(sleep)
    sleep["score_state"] = "UNSCORABLE"  # a score left in by mistake is still ignored
    assert set(whoop.sleep_fields(sleep)) == {"start", "tz_offset_min", "bedtime_min", "wake_min"}
    points, cursor, counts = whoop.api_points([whoop_sleep("sleep-night-2")], [], [], [], whoop._state(None))
    assert points == [] and counts["pending"] == 1
    # The cursor stays at the pending sleep: the next run reads it again.
    assert cursor["since"]["sleep"] == 1790976600 and cursor["pinned"]["sleep"] == {}
    assert whoop.recovery_fields({"score_state": "PENDING_SCORE", "score": {"recovery_score": 1}}) == {}
    assert whoop.workout_fields({"start": "2026-10-01T07:00:00Z", "end": "2026-10-01T07:10:00Z",
                                 "sport_name": "yoga", "score_state": "UNSCORABLE"}) == \
        {"activity": "yoga", "duration_s": 600}


def test_a_pending_record_is_written_once_scored(whoop_connected, whoop_connector, influx, db, alice):
    runner.run(db, whoop_connector)
    assert stored_line(influx, "sleep_session", 1791006000) is None  # tonight's, pending
    night = next(s for s in whoop_connected.records["sleep"] if s["id"] == "sleep-night-2")
    night.update(score_state="SCORED", score=copy.deepcopy(whoop_sleep("sleep-night-1")["score"]))
    record = runner.run(db, whoop_connector)
    assert record.status == "ok", record.message
    tags, fields = stored_line(influx, "sleep_session", 1791006000)
    assert tags["kind"] == "night" and fields["score"] == "98.0" and fields["start"] == "1790976600i"


def test_a_missing_value_is_no_field_never_a_zero():
    cycle = {"id": 7, "start": "2026-10-01T21:00:00Z", "end": "2026-10-02T21:00:00Z", "score_state": "SCORED",
             "score": {"strain": 3.5, "kilojoule": None, "average_heart_rate": 60}}
    assert whoop.cycle_fields(cycle) == {"duration_s": 86400, "strain": 3.5, "avg_hr": 60.0}
    # No step_count on a cycle: no steps (an old cycle's, maybe), never 0.
    assert "steps" not in whoop.cycle_fields(cycle)
    assert whoop.cycle_fields({**cycle, "step_count": 0})["steps"] == 0
    sleep = whoop_sleep("sleep-night-1")
    partial = copy.deepcopy(sleep)
    del partial["score"]["stage_summary"]["total_rem_sleep_time_milli"]
    fields = whoop.sleep_fields(partial)
    assert "rem_s" not in fields and "asleep_s" not in fields and fields["light_s"] == 14906


def test_naps_and_nights_kj_to_kcal_zones_and_steps():
    assert whoop._kcal(8288.297) == 1981.0 and whoop._kcal(4184) == 1000.0 and whoop._kcal(None) is None
    run = whoop.workout_fields(whoop_record("workout", "workout-run-1"))
    assert [run[f"zone{n}_s"] for n in range(6)] == [300, 600, 900, 900, 600, 300]
    points, _cursor, _counts = whoop.api_points(
        [whoop_sleep("sleep-night-1"), whoop_sleep("sleep-nap-1")], [], [whoop_record("cycle", 1001)], [],
        whoop._state(None))
    assert sorted(p.tags.get("kind") for p in points if p.measurement is SLEEP_SESSION) == ["nap", "night"]
    assert [p.fields["steps"] for p in points if p.measurement is DAILY_ACTIVITY] == [8234]


def test_sport_names_get_myfitnesspal_s_categories():
    assert [whoop.category(name) for name in (
        "running", "Running", "cycling", "Mountain Biking", "spin", "weightlifting", "functional-fitness",
        "walking", "hiking/rucking", "swimming", "rock climbing", "rowing", "hiit", "elliptical", "yoga", None)] \
        == ["run", "run", "bike", "bike", "bike", "strength", "strength", "walk", "hike", "swim", "climb",
            "paddle", "aerobic", "elliptical", "other", "other"]


def test_local_minutes_follow_the_record_s_own_offset():
    start = datetime(2026, 10, 1, 21, 10, tzinfo=timezone.utc)
    assert whoop.local_fields(start, start + timedelta(hours=8), 120) == \
        {"tz_offset_min": 120, "bedtime_min": -50, "wake_min": 430}
    assert whoop.local_fields(start, None, -300) == {"tz_offset_min": -300, "bedtime_min": -470}
    assert whoop.local_fields(start, None, None) == {}
    assert [whoop.offset_minutes(x) for x in ("+02:00", "-05:30", "Z", None, "+2")] == [120, -330, None, None, None]


# -- Cursor, pins and lookback ----------------------------------------------------------

def test_a_first_run_reads_the_whole_history_25_a_page(whoop_connected, whoop_connector):
    whoop_connector.sync(None)
    reads = [(path, params) for path, params in whoop_connected.params]
    assert {path for path, _ in reads} == {"/developer/v2/activity/sleep", "/developer/v2/recovery",
                                           "/developer/v2/cycle", "/developer/v2/activity/workout"}
    assert all(params.get("limit") == "25" and "start" not in params for _path, params in reads)


def test_later_runs_read_from_the_newest_start_minus_the_lookback(whoop_connected, whoop_connector, monkeypatch):
    cursor = whoop_connector.sync(None).cursor
    whoop_connected.params.clear()
    monkeypatch.setattr(whoop_connector.settings, "lookback_days", 2)
    whoop_connector.sync(cursor)
    starts = {path.rsplit("/", 1)[1]: params.get("start") for path, params in whoop_connected.params}
    assert starts == {
        "sleep": "2026-09-30T21:30:00.000Z",     # the pending night's start - 2 days
        "recovery": "2026-09-29T21:30:00.000Z",  # the sleeps' window, a day wider
        "cycle": "2026-09-26T22:00:00.000Z",     # the pending cycle stays in the window
        "workout": "2026-09-30T16:00:00.000Z",   # the newest workout - 2 days
    }


def test_paged_collections_are_read_to_the_end(whoop_connected, whoop_connector):
    whoop_connected.page_size = 1
    assert sorted(lines_of(whoop_connector.sync(None).points)) == \
        sorted([NIGHT, NAP, UNSCORABLE, CYCLE, OPEN_CYCLE, RUN, YOGA])
    assert whoop_connected.data_paths().count("/developer/v2/activity/sleep") == 4


def test_a_paging_loop_fails_the_run(whoop_connected, whoop_connector, monkeypatch):
    monkeypatch.setattr(whoop_connected, "page", lambda resource, params: {"records": [], "next_token": "again"})
    with pytest.raises(ConnectorError, match="paged /developer/v2/activity/sleep in a loop"):
        whoop_connector.sync(None)


def test_a_point_keeps_the_time_and_tags_it_was_first_written_with(whoop_connected, whoop_connector):
    """A sleep edited in the app moves its end, and may become a nap: the point
    stays where it was, or a second one would be left behind. A full resync
    keeps the pins too."""
    cursor = whoop_connector.sync(None).cursor
    night = next(s for s in whoop_connected.records["sleep"] if s["id"] == "sleep-night-1")
    night.update(end="2026-10-02T06:00:00.000Z", nap=True)
    workout = next(w for w in whoop_connected.records["workout"] if w["id"] == "workout-run-1")
    workout.update(start="2026-10-02T16:05:00.000Z", sport_name="walking")
    for state in (cursor, whoop_connector.full_cursor(cursor)):
        lines = lines_of(whoop_connector.sync(state).points)
        moved = [parse(line) for line in lines if "start=1790889000i" in line]
        assert [(m[1]["kind"], m[3], m[2]["wake_min"]) for m in moved] == [("night", 1790918744, "480i")]
        ran = [parse(line) for line in lines if line.startswith("exercise,category=run")]
        assert [(r[3], r[2]["activity"]) for r in ran] == [(1790956800, '"walking"')]
    assert whoop_connector.full_cursor(cursor) == {"pinned": cursor["pinned"]}


def test_a_recovery_scored_after_its_sleep_lands_on_that_sleep_s_point():
    state = whoop._state(None)
    _points, cursor, _ = whoop.api_points([whoop_sleep("sleep-night-1")], [], [], [], state)
    late = copy.deepcopy(next(r for r in whoop_records("recovery") if r["sleep_id"] == "sleep-night-1"))
    points, _cursor, counts = whoop.api_points([], [late], [], [], whoop._state(cursor))
    assert lines_of(points) == ["sleep_session,kind=night,source=whoop,user=alice avg_spo2_pct=95.6875,"
                                "calibrating=0i,hrv_rmssd_ms=31.813562,recovery_score=44.0,resting_hr=64.0,"
                                "skin_temp_c=33.7 1790918744"]
    assert counts["recovery"] == 1


def test_a_record_read_twice_is_one_point(whoop_connected, whoop_connector):
    """Two points sharing a key would be summed by the core: never."""
    whoop_connected.records["sleep"].append(copy.deepcopy(whoop_sleep("sleep-night-1")))
    lines = lines_of(whoop_connector.sync(None).points)
    assert lines.count(NIGHT) == 1


# -- HTTP -------------------------------------------------------------------------------

def test_rate_limits_wait_for_the_reset_and_server_errors_are_retried(whoop_connected, whoop_connector):
    whoop_connected.fail["/developer/v2/cycle"] = [(429, {"X-RateLimit-Reset": "30"}, {}), (503, {}, {}),
                                                   (429, {"X-RateLimit-Reset": "1"}, {})]
    whoop_connector.sync(None)
    assert whoop_connected.slept == [30, 4, 8]  # the reset when longer than the backoff


def test_a_daily_rate_limit_stops_the_run_rather_than_wait_hours(whoop_connected, whoop_connector, influx, db):
    whoop_connected.fail["/developer/v2/recovery"] = [(429, {"X-RateLimit-Reset": "7200"}, {})]
    record = runner.run(db, whoop_connector)
    assert record.status == "error" and "rate limit is reached on /developer/v2/recovery for 120" in record.message
    assert whoop_connected.slept == [] and influx.writes == []
    assert runner.load_cursor(db, "whoop").get("sync") is None


def test_a_client_error_is_not_retried_and_a_forbidden_scope_is_named(whoop_connected, whoop_connector):
    whoop_connected.fail["/developer/v2/cycle"] = [(404, {}, {})]
    with pytest.raises(ConnectorError, match="WHOOP answered 404 on /developer/v2/cycle"):
        whoop_connector.sync(None)
    whoop_connected.fail["/developer/v2/activity/workout"] = [(403, {}, {})]
    with pytest.raises(ConnectorError, match="refused /developer/v2/activity/workout \\(403\\): was its scope"):
        whoop_connector.sync(None)
    assert whoop_connected.slept == []


def test_a_redirect_is_refused_never_followed_with_the_token(whoop_connected, whoop_connector):
    from tests.fakes import FakeWhoop

    elsewhere = FakeWhoop()
    try:
        whoop_connected.fail["/developer/v2/activity/sleep"] = [
            (302, {"Location": elsewhere.url + "developer/v2/activity/sleep"}, {})]
        with pytest.raises(ConnectorError, match="WHOOP answered 302"):
            whoop_connector.sync(None)
        assert elsewhere.events == []
    finally:
        elsewhere.close()


def test_requests_name_tributary(whoop_connected, whoop_connector):
    from app.__version__ import __version__

    whoop_connector.sync(None)
    assert whoop_connected.user_agents == {f"Tributary/{__version__}"}


def test_an_unreachable_api(whoop_connected, whoop_connector, monkeypatch):
    whoop_connector.sync(None)  # the token is fresh: no token request below
    monkeypatch.setattr(whoop_api, "API_ENDPOINT", "http://127.0.0.1:9/")
    with pytest.raises(ConnectorError, match="WHOOP unreachable"):
        whoop_connector.sync(None)
    assert whoop_connected.slept == [2, 4, 8]


# -- The export -------------------------------------------------------------------------

def test_the_export_lands_on_the_api_s_points_and_fills_the_history_before_it(
        whoop_connected, whoop_connector, influx, db, alice, tmp_path):
    assert runner.run(db, whoop_connector).status == "ok"
    path = write_whoop_export(tmp_path / "my_whoop_data.zip")
    record = runner.run(db, whoop_connector, file=str(path), filename=path.name, trigger="upload", dry_run=True)
    assert record.status == "ok", record.message
    preview = [line for line in record.preview.splitlines() if not line.startswith("#")]
    assert sorted(preview) == sorted([NIGHT_FROM_EXPORT, RUN_FROM_EXPORT, OLD_NIGHT, OLD_CYCLE, OLD_RIDE, *JOURNAL])
    assert ("sleeps: 1 on the API's points, 1 at their own time, cycles: 1 on the API's points, 1 at their own "
            "time, workouts: 1 on the API's points, 1 at their own time, 3 journal answer(s)") in record.message

    record = runner.promote(db, whoop_connector)
    assert record.status == "ok", record.message
    assert record.message.endswith("after dropping its own journal from 2026-03-09 to 2026-10-01.")
    # The API's fields are still the API's; the export's are added beside them.
    _tags, night = stored_line(influx, "sleep_session", 1790918744)
    assert (night["score"], night["asleep_s"], night["sleep_need_s"]) == ("98.0", "27416i", "27900i")
    _tags, run = stored_line(influx, "exercise", 1790956800)
    assert (run["activity"], run["duration_s"], run["zone1_pct"], run["gps_enabled"]) == \
        ('"running"', "2730i", "20.0", "1i")
    # Nothing but the journal was dropped first, over the days it covers.
    deletes = [q for _db, q in influx.queries if q.startswith(("DELETE", "DROP"))]
    assert deletes == ["DELETE FROM \"journal\" WHERE \"source\" = 'whoop' AND \"user\" = 'alice' "
                       "AND time >= '2026-03-08T23:00:00Z' AND time < '2026-10-01T22:00:00Z'"]


def test_a_row_is_the_api_s_record_within_60_seconds_only(tmp_path):
    pins = {"sleep": {"s": [5000, 1000, 0]}, "cycle": {}, "workout": {"w": [2000, 2000, "run"]}}
    matcher = whoop.Matcher(pins)
    at = datetime.fromtimestamp
    assert matcher.match("sleep", at(1060, timezone.utc)) == [5000, 1000, 0]
    assert matcher.match("sleep", at(940, timezone.utc)) == [5000, 1000, 0]
    assert matcher.match("sleep", at(1061, timezone.utc)) is None
    assert matcher.match("workout", at(1939, timezone.utc)) is None
    assert matcher.match("cycle", at(1000, timezone.utc)) is None


def test_an_export_before_any_api_run_is_written_whole_at_its_own_times(whoop_connector, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", "alice")
    result = whoop_connector.import_file(write_whoop_export(tmp_path / "export.zip"))
    lines = lines_of(result.points)
    assert OLD_NIGHT in lines and OLD_RIDE in lines and len([x for x in lines if x.startswith("journal")]) == 3
    night = [parse(line) for line in lines if "sleep_need_s=27900i" in line]
    # The first night, from sleeps.csv and the cycle's columns: one point, at its wake.
    assert len(night) == 1 and night[0][3] == 1790918744 and night[0][2]["recovery_score"] == "44.0"
    assert result.spans == {"journal": whoop.Span(datetime(2026, 3, 9).date(), datetime(2026, 10, 1).date())}


def test_the_journal_keeps_custom_questions_and_never_a_note(whoop_connector, tmp_path):
    result = whoop_connector.import_file(write_whoop_export(
        tmp_path / "journal_entries.csv", {"journal_entries.csv": WHOOP_EXPORT["journal_entries.csv"]},
        zipped=False))
    lines = lines_of(result.points)
    assert sorted(lines) == sorted(JOURNAL)
    assert all(WHOOP_NOTE not in line and "glasses" not in line for line in lines)
    assert whoop.JOURNAL.field_types == {"answered_yes": int, "count": int}
    assert whoop.JOURNAL.tag_keys == ("source", "user", "question")


def test_a_new_export_replaces_the_journal_only(whoop_connected, whoop_connector, influx, db, alice, tmp_path):
    runner.run(db, whoop_connector)
    path = write_whoop_export(tmp_path / "export.zip")
    runner.run(db, whoop_connector, file=str(path), filename=path.name, trigger="upload")
    files = dict(WHOOP_EXPORT)
    # The member deleted a journal answer since: the next export, the whole
    # history again, has it no more.
    rows = WHOOP_EXPORT["journal_entries.csv"].splitlines()
    files["journal_entries.csv"] = "\n".join(row for row in rows if "Read in bed" not in row) + "\n"
    path = write_whoop_export(tmp_path / "export.zip", files)
    assert runner.run(db, whoop_connector, file=str(path), filename=path.name, trigger="upload").status == "ok"
    assert [(t["question"], ts) for t, _f, ts in influx.stored("journal")] == \
        [("Have any alcoholic drinks?", 1773093600), ("Have any alcoholic drinks?", 1790889000)]
    # The API's series and the export's other points are all still there.
    assert stored_line(influx, "sleep_session", 1790918744) and stored_line(influx, "sleep_session", 1773121500)
    assert stored_line(influx, "daily_activity", 1773093600) and stored_line(influx, "exercise", 1773162000)


@pytest.mark.parametrize(("content", "message"), [
    ("Cycle start time;Cycle end time;Cycle timezone;Question text;Answered yes;Notes\n",
     "not a WHOOP export file"),
    (WHOOP_JOURNAL_HEADER.replace(",Answered yes", "") + "\n", "no column 'Answered yes'"),
    (WHOOP_JOURNAL_HEADER + "\n2026-10-01 23:10:00,,UTC+02:00,Question?,maybe,\n",
     "line 2: column 'Answered yes': not true or false"),
    (WHOOP_SLEEPS_HEADER + "\n2026-10-01 23:10:00,,somewhere,,,,,,,,,,,,,,,false\n",
     "line 2: column 'Cycle timezone'"),
])
def test_what_is_not_an_export_is_named(whoop_connector, tmp_path, content, message):
    path = tmp_path / "upload.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConnectorError, match=message):
        whoop_connector.import_file(path)


def test_a_spreadsheet_s_re_save_and_a_non_utf8_file(whoop_connector, tmp_path):
    zipped = write_whoop_export(tmp_path / "a.zip", {"journal_entries.csv": WHOOP_JOURNAL_HEADER.replace(",", ";")})
    with pytest.raises(ConnectorError, match="re-saved by a spreadsheet app"):
        whoop_connector.import_file(zipped)
    latin = tmp_path / "b.zip"
    write_whoop_export(latin, {"sleeps.csv": "x"})
    import zipfile
    with zipfile.ZipFile(latin, "w") as archive:
        archive.writestr("journal_entries.csv", (WHOOP_JOURNAL_HEADER + "\n").encode("utf-8") + b"\xe9\xff\n")
    with pytest.raises(ConnectorError, match="not a UTF-8 CSV"):
        whoop_connector.import_file(latin)
    empty = write_whoop_export(tmp_path / "c.zip", {"README.txt": "nothing"})
    with pytest.raises(ConnectorError, match="no WHOOP export file in the zip"):
        whoop_connector.import_file(empty)


def test_an_export_with_nothing_to_write_fails_rather_than_drop(whoop_connector, tmp_path):
    path = write_whoop_export(tmp_path / "x.zip", {"journal_entries.csv": WHOOP_JOURNAL_HEADER + "\n",
                                                  "physiological_cycles.csv": WHOOP_CYCLES_HEADER + "\n"})
    with pytest.raises(ConnectorError, match="holds nothing to import"):
        whoop_connector.import_file(path)


def test_the_parser_keeps_whoops_converters():
    tz = whoop_export.parse_timezone("UTC-05:30")
    assert tz.utcoffset(None) == -timedelta(hours=5, minutes=30)
    assert whoop_export.parse_timezone("UTCZ") == timezone.utc
    row = whoop_export.from_csv_row(whoop_export.WORKOUTS, {
        "Cycle timezone": "UTC+01:00", "Cycle start time": "2026-03-09 23:00:00", "Cycle end time": "",
        "Workout start time": "2026-03-10 18:00:00", "Duration (min)": "45.5", "GPS enabled": "TRUE",
        "Max HR (bpm)": "160.4"})
    assert row["workout_start"] == datetime(2026, 3, 10, 17, tzinfo=timezone.utc)
    assert (row["cycle_end"], row["duration_ms"], row["gps_enabled"], row["max_heart_rate"]) == \
        (None, 2_730_000, True, 160)


# -- Configuration ----------------------------------------------------------------------

def test_it_runs_daily_mid_morning_once_connected(whoop_connector, secret_key):
    assert not whoop_connector.api_configured()
    assert whoop_connector.setup_hint() == "Not connected: connect Whoop on its page."
    # Nothing to read before it is connected, so nothing is scheduled either.
    assert schedule.resolve(whoop_connector, None, "Europe/Paris").cron is None
    oauth.save("whoop", oauth.Grant("an-access-token", "a-refresh-token", None, None))
    assert whoop_connector.api_configured()
    runs = schedule.resolve(whoop_connector, None, "Europe/Paris")
    assert (runs.cron, runs.label, runs.source) == ("35 10 * * *", "Daily at 10:35", "default")


def test_the_app_s_settings_are_named_when_missing(whoop_connector, monkeypatch):
    monkeypatch.setattr(whoop_connector.settings, "client_secret", None)
    monkeypatch.setattr(whoop_connector.settings, "redirect_uri", " ")
    assert whoop_connector.oauth_problem() == ("Set TRIBUTARY_WHOOP_CLIENT_SECRET, TRIBUTARY_WHOOP_REDIRECT_URI "
                                               "(your WHOOP app's) to connect Whoop.")
    monkeypatch.setattr(whoop_connector.settings, "client_secret", SecretStr("x"))
    monkeypatch.setattr(whoop_connector.settings, "redirect_uri", "tributary.example/callback")
    assert "must be a full URL" in whoop_connector.oauth_problem()
    assert whoop.WhoopConnector.required_env == ("TRIBUTARY_WHOOP_CLIENT_ID", "TRIBUTARY_WHOOP_CLIENT_SECRET",
                                                 "TRIBUTARY_WHOOP_REDIRECT_URI", "TRIBUTARY_SECRET_KEY")


def test_the_declared_series(whoop_connector):
    assert whoop.WhoopConnector.measurements == (SLEEP_SESSION, whoop.JOURNAL, DAILY_ACTIVITY, EXERCISE)
    assert whoop_connector.watch_dir is None and whoop_connector.file_types == (".zip", ".csv")
    assert SLEEP_SESSION.tag_keys == ("source", "user", "kind")
    assert DAILY_ACTIVITY.tag_keys == ("source", "user")


# Types already stored in SleepStats..sleep_session and BodyStats..exercise
# (SHOW FIELD KEYS on the owner's InfluxDB, read by the main thread: types
# only). `manual` is a bool there: left out of the declaration (Tributary has
# no bool type).
LIVE_SLEEP_SESSION = {
    **{name: int for name in ("asleep_s", "awake_s", "bedtime_min", "cycles", "deep_s", "in_bed_s", "light_s",
                              "rem_s", "start", "tz_offset_min", "wake_min")},
    **{name: float for name in ("avg_hr", "min_hr", "max_hr", "avg_spo2_pct", "deep_pct", "rating", "sdnn_ms",
                                "noise_level", "snore_s")},
    **{name: str for name in ("note", "tags", "tz")},
}
LIVE_EXERCISE = {"activity": str, "duration_s": int, "energy_kcal": float}


def test_the_declarations_match_the_types_already_stored():
    for name, kind in SLEEP_SESSION.field_types.items():
        assert LIVE_SLEEP_SESSION.get(name, kind) is kind, name
    assert "manual" not in SLEEP_SESSION.field_types
    assert set(SLEEP_SESSION.tag_keys) == {"source", "user", "kind"}
    for name, kind in EXERCISE.field_types.items():
        assert LIVE_EXERCISE.get(name, kind) is kind, name


# -- Helpers ----------------------------------------------------------------------------

def whoop_records(resource):
    from tests.fakes import WHOOP_CYCLES, WHOOP_RECOVERIES, WHOOP_SLEEPS, WHOOP_WORKOUTS

    return {"sleep": WHOOP_SLEEPS, "recovery": WHOOP_RECOVERIES, "cycle": WHOOP_CYCLES,
            "workout": WHOOP_WORKOUTS}[resource]


def whoop_record(resource, key):
    return copy.deepcopy(next(r for r in whoop_records(resource) if r.get("id") == key))


def whoop_sleep(key):
    return whoop_record("sleep", key)
