from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import runner
from app.config import settings
from app.connectors import todoist
from app.connectors.base import ConnectorError
from app.points import encode
from tests.fakes import TODOIST_STATS, TODOIST_TOKEN, parse, todoist_event

PARIS = ZoneInfo("Europe/Paris")

# Written by hand from the ported job's code (its transform.Point.to_line):
# sorted tags, empty ones left out, priority 4 = p1, labels sorted and
# comma-joined, lead time rounded to 0.01 h, late_days in local days.
MILK = ("todoist_task_completed,priority=p1,project=Home,recurring=false,section=Errands,subtask=false,task_id=t-milk "
        'content="Buy milk, eggs",count=1i,duration_minutes=30i,labels="errand,shop",late_days=0i,'
        "lead_time_hours=32.5,postponed_count=1i 1790879415")
REPORT = ("todoist_task_completed,priority=p4,project=Old\\ project,recurring=false,subtask=true,task_id=t-report "
          'content="Write \\"Q3\\" report",count=1i,duration_minutes=2880i,late_days=5i,lead_time_hours=2542.75,'
          "postponed_count=0i 1749973500")
MILK_DONE = 'todoist_completion,project=Home,task_id=t-milk content="Buy milk, eggs",count=1i 1790879415'
STATS = ("todoist_stats completed_count=42i,current_daily_streak=3i,current_weekly_streak=2i,daily_goal=5i,"
         "karma=1234.5,max_daily_streak=12i,max_weekly_streak=9i,weekly_goal=25i")


def lines_of(result, user=None):
    return encode(result.points, "todoist", user).get("TodoistStats", [])


def of(lines, measurement):
    return [line for line in lines if parse(line)[0] == measurement]


def windows_of(fake, path):
    keys = ("since", "until") if path.startswith("/tasks") else ("date_from", "date_to")
    seen = [(p[keys[0]], p[keys[1]]) for name, p in fake.calls if name == path and "cursor" not in p]
    return [(datetime.fromisoformat(a.replace("Z", "+00:00")), datetime.fromisoformat(b.replace("Z", "+00:00")))
            for a, b in seen]


@pytest.fixture()
def with_text(monkeypatch, todoist_connector):
    monkeypatch.setattr(todoist_connector.settings, "task_text", True)


# -- Mapping ----------------------------------------------------------------------------

def test_completed_tasks_are_written_as_the_ported_job_wrote_them(todoist_api, todoist_connector, with_text):
    tasks = of(lines_of(todoist_connector.sync(None)), "todoist_task_completed")
    assert MILK in tasks and REPORT in tasks
    by_id = {parse(line)[1]["task_id"]: parse(line) for line in tasks}
    # Not completions: no time, no id.
    assert set(by_id) == {"t-milk", "t-report", "t-water", "t-gone", "t-lost"}
    # A recurring task's due date has moved on: no late_days.
    _m, tags, fields, _ts = by_id["t-water"]
    assert (tags["recurring"], tags["priority"]) == ("true", "p3") and "late_days" not in fields
    # A deleted project is named by the activity log, else "(unknown)".
    assert by_id["t-gone"][1]["project"] == "Gone project"
    assert by_id["t-lost"][1]["project"] == "(unknown)"
    # Added after its completion (clock skew): no lead time.
    assert "lead_time_hours" not in by_id["t-gone"][2]


def test_every_completion_is_a_point_recurring_ones_included(todoist_api, todoist_connector, with_text):
    done = of(lines_of(todoist_connector.sync(None)), "todoist_completion")
    assert MILK_DONE in done
    assert [parse(line)[3] for line in done if parse(line)[1]["task_id"] == "t-plants"] == [1791010800, 1791615600]
    # On a window boundary, returned with both windows: one point, count 1.
    edge = [parse(line) for line in done if parse(line)[1]["task_id"] == "t-edge"]
    assert [(fields["count"], ts) for _m, _t, fields, ts in edge] == [("1i", 1744272000)]
    assert (datetime(2025, 4, 10, 8, tzinfo=timezone.utc), datetime(2025, 4, 10, 8, tzinfo=timezone.utc)) in [
        (a[1], b[0]) for a, b in zip(windows_of(todoist_api, "/activities"), windows_of(todoist_api, "/activities")[1:])]


def test_karma_by_day_and_todays_stats_at_local_midnight(todoist_api, todoist_connector):
    lines = lines_of(todoist_connector.sync(None))
    assert of(lines, "todoist_karma") == ["todoist_karma karma=1200.0 1791064800",
                                          "todoist_karma karma=1210.5 1791151200"]
    today = datetime.now(PARIS).date()
    midnight = int(datetime.combine(today, datetime.min.time(), tzinfo=PARIS).timestamp())
    assert of(lines, "todoist_stats") == [f"{STATS} {midnight}"]


def test_no_stats_point_without_stats(todoist_api, todoist_connector):
    todoist_api.stats = {"karma_graph_data": [], "goals": {"current_daily_streak": {"count": True}}}
    assert of(lines_of(todoist_connector.sync(None)), "todoist_stats") == []


def test_the_task_text_is_left_out_unless_asked(todoist_api, todoist_connector):
    lines = lines_of(todoist_connector.sync(None))
    assert not [line for line in lines if "content=" in line or "labels=" in line]
    milk = [parse(line) for line in of(lines, "todoist_task_completed") if "t-milk" in line][0]
    assert set(milk[2]) == {"count", "duration_minutes", "late_days", "lead_time_hours", "postponed_count"}


def test_records_sharing_a_key_are_merged_the_later_one_winning():
    events = [todoist_event("t-a", "2026-01-01T10:00:00Z", content="first"),
              todoist_event("t-a", "2026-01-01T10:00:00.900Z", content="second")]
    assert encode(todoist.event_points(events, {}, True), "todoist")["TodoistStats"] == [
        'todoist_completion,project=(unknown),task_id=t-a content="second",count=1i 1767261600']


def test_task_text_is_written_whole(todoist_api, todoist_connector, with_text):
    """As the job: the core's cut of long texts would rewrite its history shorter."""
    todoist_api.tasks[0]["content"] = "x" * 600
    milk = [line for line in lines_of(todoist_connector.sync(None)) if "task_id=t-milk" in line
            and line.startswith("todoist_task_completed")]
    assert parse(milk[0])[2]["content"] == f'"{"x" * 600}"'


def test_a_malformed_event_is_skipped_not_fatal(todoist_api, todoist_connector, with_text):
    todoist_api.events.append({**todoist_event("t-odd", "2026-10-04T08:00:00Z"), "extra_data": ["odd"]})
    done = of(lines_of(todoist_connector.sync(None)), "todoist_completion")
    assert [line for line in done if "task_id=t-odd" in line] == [
        "todoist_completion,project=Inbox,task_id=t-odd count=1i 1791100800"]  # no text to write


def test_the_summary_counts_points_not_records(todoist_api, todoist_connector):
    result = todoist_connector.sync(None)
    # 7 events, one undated, t-edge returned twice: 6 completions.
    assert result.summary == ["5 completed task(s) and 6 completion(s) since 2025-01-10"]


def test_no_source_and_no_user_tag_even_with_a_user_configured(todoist_api, todoist_connector, influx, db,
                                                                monkeypatch):
    """The ported job wrote neither: a tag more would start new series next
    to its history."""
    monkeypatch.setattr(settings, "influxdb_user", "dana")
    record = runner.run(db, todoist_connector)
    assert record.status == "ok", record.message
    stored = influx.stored()
    assert stored and all(not {"source", "user"} & set(tags) for tags, _f, _ts in stored)
    assert {db_ for db_, _p, _l in influx.writes} == {"TodoistStats"}


# -- Reading: windows, cursor, paging -----------------------------------------------------

def test_a_first_run_reads_the_whole_history_in_90_day_windows(todoist_api, todoist_connector):
    todoist_connector.sync(None)
    assert todoist_api.paths().count("/user") == 1
    for path in ("/tasks/completed/by_completion_date", "/activities"):
        windows = windows_of(todoist_api, path)
        assert windows[0][0] == datetime(2025, 1, 10, 8, tzinfo=timezone.utc)  # the account's creation
        assert all(b - a <= timedelta(days=90) for a, b in windows)
        assert all(prev[1] == nxt[0] for prev, nxt in zip(windows, windows[1:]))
        assert datetime.now(timezone.utc) - windows[-1][1] < timedelta(minutes=1)


def test_later_runs_read_from_the_last_one_minus_the_lookback(todoist_api, todoist_connector, influx, db):
    assert runner.run(db, todoist_connector).status == "ok"
    until = runner.load_cursor(db, "todoist")["sync"]["until"]
    todoist_api.calls.clear()
    todoist_api.events.append(todoist_event("t-new", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    written = len(influx.written_lines())

    record = runner.run(db, todoist_connector)
    assert record.status == "ok", record.message
    assert "/user" not in todoist_api.paths()
    since = datetime.fromtimestamp(until, timezone.utc) - timedelta(days=3)
    assert windows_of(todoist_api, "/activities")[0][0] == since
    new = influx.written_lines()[written:]
    assert any("task_id=t-new" in line for line in new)
    assert not any("task_id=t-report" in line for line in new)  # 2025: not read again


def test_a_full_resync_reads_everything_again(todoist_api, todoist_connector, influx, db):
    runner.run(db, todoist_connector)
    todoist_api.calls.clear()
    assert runner.run(db, todoist_connector, full=True).status == "ok"
    assert "/user" in todoist_api.paths()


def test_paged_lists_are_read_to_the_end(todoist_api, todoist_connector, with_text):
    todoist_api.page_size = 100
    whole = sorted(lines_of(todoist_connector.sync(None)))
    todoist_api.page_size = 1
    todoist_api.calls.clear()
    assert sorted(lines_of(todoist_connector.sync(None))) == whole
    assert any("cursor" in params for path, params in todoist_api.calls if path == "/projects")


# -- Errors -------------------------------------------------------------------------------

def test_a_refused_token_names_the_variable_never_the_token(todoist_api, todoist_connector):
    todoist_api.token = "another-token"
    with pytest.raises(ConnectorError) as error:
        todoist_connector.sync(None)
    assert "TRIBUTARY_TODOIST_TOKEN" in str(error.value) and TODOIST_TOKEN not in str(error.value)
    assert todoist_api.slept == []


def test_rate_limits_and_server_errors_are_retried(todoist_api, todoist_connector):
    todoist_api.fail["/tasks/completed/stats"] = [(429, {"Retry-After": "5"}), (503, {}), (429, {"Retry-After": "3600"})]
    todoist_connector.sync(None)
    assert todoist_api.slept == [5, 4, 60]  # Retry-After when longer, at most a minute


def test_a_lasting_error_fails_the_run(todoist_api, todoist_connector, influx, db):
    todoist_api.fail["/tasks/completed/stats"] = [(500, {})] * 4
    record = runner.run(db, todoist_connector)
    assert record.status == "error" and "Todoist answered 500 on /tasks/completed/stats" in record.message
    assert todoist_api.slept == [2, 4, 8]
    assert runner.load_cursor(db, "todoist").get("sync") is None
    assert influx.writes == []


def test_a_client_error_is_not_retried(todoist_api, todoist_connector):
    todoist_api.fail["/sections"] = [(404, {})]
    with pytest.raises(ConnectorError, match="Todoist answered 404 on /sections"):
        todoist_connector.sync(None)
    assert todoist_api.slept == []


def test_an_unreachable_api(todoist_api, todoist_connector, monkeypatch):
    monkeypatch.setattr(todoist, "API_URL", "http://127.0.0.1:9/api/v1")
    with pytest.raises(ConnectorError, match="Todoist unreachable"):
        todoist_connector.sync(None)
    assert todoist_api.slept == [2, 4, 8]


def test_a_forbidden_endpoint_is_named(todoist_api, todoist_connector):
    todoist_api.fail["/activities"] = [(403, {})]
    with pytest.raises(ConnectorError, match="Todoist refused /activities \\(403\\)"):
        todoist_connector.sync(None)


def test_a_redirect_is_refused_never_followed_with_the_token(todoist_api, todoist_connector):
    from tests.fakes import FakeTodoist

    elsewhere = FakeTodoist()
    try:
        todoist_api.fail["/user"] = [(302, {"Location": elsewhere.url + "/user"})]
        with pytest.raises(ConnectorError, match="Todoist answered 302 on /user"):
            todoist_connector.sync(None)
        assert elsewhere.calls == []
    finally:
        elsewhere.close()


def test_requests_name_tributary(todoist_api, todoist_connector):
    from app.__version__ import __version__

    todoist_connector.sync(None)
    assert todoist_api.user_agents == {f"Tributary/{__version__}"}


def test_a_paging_loop_fails_the_run(todoist_api, todoist_connector, monkeypatch):
    monkeypatch.setattr(todoist_api, "_page", lambda items, key, params: {key: items[:1], "next_cursor": "again"})
    with pytest.raises(ConnectorError, match="paged /projects in a loop"):
        todoist_connector.sync(None)


def test_configured_only_with_a_token(todoist_connector, monkeypatch):
    from pydantic import SecretStr

    assert not todoist_connector.api_configured()
    monkeypatch.setattr(todoist_connector.settings, "token", SecretStr("  "))
    assert not todoist_connector.api_configured()
    assert todoist_connector.settings.interval_minutes == 1440


# -- Parsing edges ------------------------------------------------------------------------

def test_parsing_edges():
    assert todoist.parse_datetime("2026-10-01T18:30:15.123456Z") == datetime(2026, 10, 1, 18, 30, 15, 123456,
                                                                              tzinfo=timezone.utc)
    assert todoist.parse_datetime("2026-10-01T20:30:15+02:00") == datetime(2026, 10, 1, 18, 30, 15,
                                                                           tzinfo=timezone.utc)
    assert todoist.parse_datetime("2026-10-01T18:30:15") == datetime(2026, 10, 1, 18, 30, 15, tzinfo=timezone.utc)
    assert [todoist.parse_datetime(x) for x in (None, "", "soon", 12)] == [None] * 4
    assert [todoist._priority(x) for x in (4, "1", 0, 5, None, "x")] == ["p1", "p4", "", "", "", ""]
    assert todoist._duration_minutes({"amount": 2, "unit": "day"}) == 2880
    assert todoist._duration_minutes({"amount": "45", "unit": "minute"}) == 45
    assert [todoist._duration_minutes(x) for x in (None, {}, {"amount": None}, "30")] == [None] * 4
    assert todoist._due_date({"date": "2026-10-01T17:00:00"}) == date(2026, 10, 1)
    assert [todoist._due_date(x) for x in (None, {}, {"date": "someday"}, "2026-10-01")] == [None] * 4
    assert todoist.stats_fields({"karma": True, "completed_count": 1.5, "goals": {"daily_goal": "5"}}) == {}
    assert todoist.stats_fields(TODOIST_STATS)["karma"] == 1234.5


def test_local_midnight_follows_daylight_saving():
    assert todoist.local_midnight(date(2026, 1, 15), PARIS) == datetime(2026, 1, 14, 23, tzinfo=timezone.utc)
    assert todoist.local_midnight(date(2026, 7, 15), PARIS) == datetime(2026, 7, 14, 22, tzinfo=timezone.utc)
