import json

import pytest

from app import runner
from app.connectors.audiobookshelf import SESSIONS_PAGE_SIZE, device_label
from app.connectors.base import ConnectorError
from app.models import Cursor
from tests.fakes import ME, SESSIONS, FakeAbs, epoch, ms, parse, session


def lines_of(result):
    from app.points import encode

    return encode(result.points, "audiobookshelf")["MediaStats"]


def by_measurement(lines, measurement):
    return [parse(line) for line in lines if parse(line)[0] == measurement]


# -- Mapping ----------------------------------------------------------------------------

def test_sessions_become_reading_sessions(abs_api, abs_connector):
    result = abs_connector.sync(None)
    sessions = by_measurement(lines_of(result), "reading_session")
    # The 0 s session is left out.
    assert len(sessions) == 4
    removed, first, second, podcast = sessions
    _m, tags, fields, ts = first
    assert ts == epoch(2025, 3, 1, 20, 0)
    assert tags == {"source": "audiobookshelf", "kind": "audiobook", "device": "Google Pixel 7",
                    "time_precision": "second"}
    # The library's current title, not the one the session recorded.
    assert (fields["minutes"], fields["sessions"], fields["title"]) == ("30.0", "1i", '"Dune"')
    assert (fields["local_hour"], fields["local_weekday"]) == ("21i", "6i")
    assert second[1]["device"] == "Bedroom tablet"
    # A podcast episode adds up under its show.
    assert (podcast[1]["kind"], podcast[1]["device"], podcast[2]["title"], podcast[2]["minutes"]) == (
        "podcast", "Firefox · Linux", '"The Podcast"', "20.0")
    # No longer in any library: named by its own session.
    assert removed[2]["title"] == '"Removed book"'


def test_duration_is_time_listened_not_the_distance_covered(abs_api, abs_connector):
    # A seek from 0 to 5 h in a 10-minute session.
    abs_api.sessions = [session("seek", "book-dune", ms(2025, 4, 1, 12, 0), 600, startTime=0, currentTime=18000)]
    reading, = by_measurement(lines_of(abs_connector.sync(None)), "reading_session")
    assert reading[2]["minutes"] == "10.0"


def test_finished_books_but_never_episodes(abs_api, abs_connector):
    finished, = by_measurement(lines_of(abs_connector.sync(None)), "book_finished")
    _m, tags, fields, ts = finished
    assert ts == epoch(2025, 3, 5, 22, 0)  # finishedAt
    assert tags == {"source": "audiobookshelf", "kind": "audiobook", "time_precision": "second"}
    assert (fields["count"], fields["title"]) == ("1i", '"Dune"')


def test_a_finished_book_gone_from_the_library_still_counts(abs_api, abs_connector):
    abs_api.items = [i for i in abs_api.items if i["id"] != "book-dune"]
    finished, = by_measurement(lines_of(abs_connector.sync(None)), "book_finished")
    assert finished[2]["count"] == "1i" and "title" not in finished[2]


def test_finished_without_finished_at_falls_back_to_the_last_update_once(abs_api, abs_connector):
    progress = abs_api.me["mediaProgress"][0]
    progress["finishedAt"] = None
    first = abs_connector.sync(None)
    finished, = by_measurement(lines_of(first), "book_finished")
    assert finished[3] == epoch(2025, 3, 5, 22, 0, 30)
    assert first.cursor["undated_finished"] == ["book-dune"]
    # Reopened later: its last update moves, but it was counted already.
    progress["lastUpdate"] = ms(2025, 6, 1)
    second = abs_connector.sync(first.cursor)
    assert second.points == [] and second.cursor["undated_finished"] == ["book-dune"]
    # A book with its finishedAt is simply rewritten at the same time.
    progress["finishedAt"], progress["lastUpdate"] = ms(2025, 3, 5, 22, 0), ms(2025, 6, 2)
    finished, = by_measurement(lines_of(abs_connector.sync(second.cursor)), "book_finished")
    assert finished[3] == epoch(2025, 3, 5, 22, 0)


@pytest.mark.parametrize("info, label", [
    ({"deviceName": "Kitchen speaker", "manufacturer": "X"}, "Kitchen speaker"),
    ({"manufacturer": "Google", "model": "Pixel 7"}, "Google Pixel 7"),
    ({"browserName": "Firefox", "osName": "Linux"}, "Firefox · Linux"),
    ({"clientName": "Abs Web", "ipAddress": "203.0.113.20"}, "Abs Web"),
    ({"ipAddress": "203.0.113.20"}, None),
    (None, None),
])
def test_device_label(info, label):
    assert device_label(info) == label


# -- Paging and cursor ------------------------------------------------------------------

def test_paging_stops_at_the_first_page_with_nothing_new(abs_api, abs_connector, monkeypatch):
    monkeypatch.setattr("app.connectors.audiobookshelf.SESSIONS_PAGE_SIZE", 2)
    first = abs_connector.sync(None)
    assert [p["page"] for p in abs_api.calls_to("/api/me/listening-sessions")] == [0, 1, 2]

    abs_api.calls.clear()
    second = abs_connector.sync(first.cursor)
    # The newest page brings nothing new: older ones are not read, and
    # neither are the libraries (no title needed).
    assert [p["page"] for p in abs_api.calls_to("/api/me/listening-sessions")] == [0]
    assert not abs_api.calls_to("/api/libraries")
    assert second.points == [] and second.cursor == first.cursor


def test_an_updated_session_is_read_again(abs_api, abs_connector, monkeypatch):
    monkeypatch.setattr("app.connectors.audiobookshelf.SESSIONS_PAGE_SIZE", 2)
    first = abs_connector.sync(None)
    s1 = next(s for s in abs_api.sessions if s["id"] == "s1")
    # Listened to again: now the most recently updated session.
    s1["timeListening"], s1["updatedAt"] = 2400, max(s["updatedAt"] for s in abs_api.sessions) + 600_000
    second = abs_connector.sync(first.cursor)
    reading, = by_measurement(lines_of(second), "reading_session")
    assert (reading[3], reading[2]["minutes"]) == (epoch(2025, 3, 1, 20, 0), "40.0")
    assert second.cursor["sessions_updated_at"] == s1["updatedAt"]


def test_cursor_holds_timestamps_only(abs_api, abs_connector):
    cursor = abs_connector.sync(None).cursor
    assert cursor == {"sessions_updated_at": max(s["updatedAt"] for s in SESSIONS),
                      "progress_updated_at": max(p["lastUpdate"] for p in ME["mediaProgress"])}


def test_page_size_is_what_the_api_is_asked_for(abs_api, abs_connector):
    abs_connector.sync(None)
    assert abs_api.calls_to("/api/me/listening-sessions")[0]["itemsPerPage"] == SESSIONS_PAGE_SIZE


# -- Runs ---------------------------------------------------------------------------------

def test_second_run_writes_nothing_new(abs_api, abs_connector, influx, db):
    run = runner.run(db, abs_connector)
    assert run.status == "ok", run.message
    assert run.points == 5 and run.message == (
        "4 new or updated session(s) (1 empty, skipped), 1 finished book(s): 5 point(s) written (MediaStats 5).")
    writes = len(influx.writes)
    run = runner.run(db, abs_connector)
    assert (run.status, run.points, run.message) == ("ok", 0, "Nothing new.")
    assert len(influx.writes) == writes


def test_a_session_deleted_in_audiobookshelf_stays_in_influxdb(abs_api, abs_connector, influx, db):
    runner.run(db, abs_connector)
    abs_api.sessions = [s for s in abs_api.sessions if s["id"] != "s5"]
    # Even a full resync (cursor ignored) drops nothing.
    run = runner.run(db, abs_connector, full=True)
    assert run.status == "ok" and run.points == 4
    assert not [q for _db, q in influx.queries if q.startswith("DROP")]
    titles = [fields["title"] for _tags, fields, _ts in influx.stored("reading_session")]
    assert '"Removed book"' in titles and len(titles) == 4


def test_a_full_resync_overwrites_instead_of_duplicating(abs_api, abs_connector, influx, db):
    runner.run(db, abs_connector)
    before = influx.stored()
    runner.run(db, abs_connector, full=True)
    assert influx.stored() == before


def test_dry_run_shows_the_lines_writes_nothing_and_keeps_the_cursor(abs_api, abs_connector, influx, db):
    run = runner.run(db, abs_connector, dry_run=True)
    assert run.status == "ok" and run.dry_run and run.points == 5
    assert run.message.startswith("Dry run. 4 new or updated session(s)")
    assert run.preview.startswith("# MediaStats\nreading_session,")
    assert len(run.preview.splitlines()) == 6
    assert influx.writes == [] and not influx.queries
    assert db.get(Cursor, "audiobookshelf") is None
    # So the real run that follows writes everything.
    assert runner.run(db, abs_connector).points == 5


def test_dry_run_works_without_influxdb(abs_api, abs_connector, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.influxdb_url", None)
    run = runner.run(db, abs_connector, dry_run=True)
    assert run.status == "ok" and run.points == 5
    run = runner.run(db, abs_connector)
    assert run.status == "error" and "TRIBUTARY_INFLUXDB_URL" in run.message


def test_a_refused_key_is_an_error_and_keeps_the_cursor(abs_api, abs_connector, influx, db, monkeypatch):
    runner.run(db, abs_connector)
    cursor = db.get(Cursor, "audiobookshelf").value_json

    def refuse(path, params=None):
        raise ConnectorError("Audiobookshelf refused the API key (401): check TRIBUTARY_ABS_TOKEN.")

    monkeypatch.setattr(abs_connector, "_get", refuse)
    run = runner.run(db, abs_connector)
    assert run.status == "error" and "401" in run.message
    db.expire_all()
    assert db.get(Cursor, "audiobookshelf").value_json == cursor


def test_unexpected_answers_are_errors_not_crashes(abs_api, abs_connector, influx, db, monkeypatch):
    monkeypatch.setattr(abs_connector, "_get", lambda path, params=None: [] if path == "/api/me" else {})
    run = runner.run(db, abs_connector)
    assert run.status == "error" and "/api/me" in run.message


# -- HTTP -----------------------------------------------------------------------------------

def test_api_calls_send_the_key_and_explain_a_refusal(abs_connector, monkeypatch):
    from pydantic import SecretStr

    fake = FakeAbs(token="good-key")
    url = fake.serve()
    try:
        monkeypatch.setattr(abs_connector.settings, "url", url + "/")
        monkeypatch.setattr(abs_connector.settings, "token", SecretStr("good-key"))
        result = abs_connector.sync(None)
        assert len(result.points) == 5
        assert fake.calls_to("/api/libraries/lib-books/items")[0] == {"limit": "200", "page": "0", "minified": "1"}
        monkeypatch.setattr(abs_connector.settings, "token", SecretStr("bad-key"))
        with pytest.raises(ConnectorError, match=r"refused the API key \(401\): check TRIBUTARY_ABS_TOKEN"):
            abs_connector.sync(None)
        monkeypatch.setattr(abs_connector.settings, "token", SecretStr("good-key"))
        with pytest.raises(ConnectorError, match="answered 404"):
            abs_connector._get("/api/nope")
    finally:
        fake.close()
    monkeypatch.setattr(abs_connector.settings, "url", "http://127.0.0.1:9/")  # nothing listens there
    with pytest.raises(ConnectorError, match="unreachable"):
        abs_connector.sync(None)


def test_configured_only_with_url_and_key(abs_connector, monkeypatch):
    from pydantic import SecretStr

    assert not abs_connector.is_configured()
    monkeypatch.setattr(abs_connector.settings, "url", "http://abs")
    assert not abs_connector.is_configured()
    monkeypatch.setattr(abs_connector.settings, "token", SecretStr(""))
    assert not abs_connector.is_configured()
    monkeypatch.setattr(abs_connector.settings, "token", SecretStr("k"))
    assert abs_connector.is_configured()


def test_settings_come_from_the_environment(monkeypatch):
    from app.connectors.audiobookshelf import AudiobookshelfSettings

    monkeypatch.setenv("TRIBUTARY_ABS_URL", "http://abs:80")
    monkeypatch.setenv("TRIBUTARY_ABS_TOKEN", "from-env")
    monkeypatch.setenv("TRIBUTARY_ABS_INTERVAL_MINUTES", "30")
    loaded = AudiobookshelfSettings()
    assert (loaded.url, loaded.token.get_secret_value(), loaded.interval_minutes) == ("http://abs:80", "from-env", 30)
    assert "from-env" not in repr(loaded) and "from-env" not in json.dumps(loaded.model_dump(mode="json"))
