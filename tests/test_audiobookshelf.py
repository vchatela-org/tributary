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


def test_an_undated_finished_book_keeps_its_first_time_even_through_a_full_resync(abs_api, abs_connector):
    progress = abs_api.me["mediaProgress"][0]
    progress["finishedAt"] = None
    first = abs_connector.sync(None)
    finished, = by_measurement(lines_of(first), "book_finished")
    assert finished[3] == epoch(2025, 3, 5, 22, 0, 30)  # its last update
    assert first.cursor["undated_finished"] == {"book-dune": ms(2025, 3, 5, 22, 0, 30)}
    # Reopened later, then even given a finishedAt: still the first time.
    progress["lastUpdate"], progress["finishedAt"] = ms(2025, 6, 1), ms(2025, 6, 1)
    second = abs_connector.sync(first.cursor)
    finished, = by_measurement(lines_of(second), "book_finished")
    assert finished[3] == epoch(2025, 3, 5, 22, 0, 30)
    full = abs_connector.sync(abs_connector.full_cursor(second.cursor))
    finished, = by_measurement(lines_of(full), "book_finished")
    assert finished[3] == epoch(2025, 3, 5, 22, 0, 30)


def test_books_finished_in_the_same_second_are_always_rewritten_together(abs_api, abs_connector, influx, db):
    """Marked finished together, they share a point (count=2): rewriting one
    alone would overwrite it with count=1."""
    progress = abs_api.me["mediaProgress"]
    together = ms(2025, 4, 1, 12, 0)
    progress[1].update(isFinished=True, finishedAt=together + 300, lastUpdate=together + 300)
    progress[0].update(finishedAt=together, lastUpdate=together + 100)
    runner.run(db, abs_connector)
    (tags, fields, ts), = influx.stored("book_finished")
    assert (ts, fields["count"], fields["title"]) == (epoch(2025, 4, 1, 12, 0), "2i", '"Dune / Started elsewhere"')
    progress[0]["lastUpdate"] = ms(2025, 5, 1)  # one of them reopened
    run = runner.run(db, abs_connector)
    assert "1 finished book(s) new or changed (2 finished books written)" in run.message
    (tags, fields, ts), = influx.stored("book_finished")
    assert fields["count"] == "2i"


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

def test_paging_stops_at_the_first_page_older_than_the_lookback(abs_api, abs_connector, monkeypatch):
    monkeypatch.setattr("app.connectors.audiobookshelf.SESSIONS_PAGE_SIZE", 2)
    monkeypatch.setattr(abs_connector.settings, "lookback_days", 1)
    first = abs_connector.sync(None)
    assert [p["page"] for p in abs_api.calls_to("/api/me/listening-sessions")] == [0, 1, 2]

    abs_api.calls.clear()
    second = abs_connector.sync(first.cursor)
    # Newest first: s4 (Mar 4) and s3 (Mar 3) on page 0, within a day of the
    # newest update; page 1 (Mar 2, Mar 1) is older: the rest is not read.
    # Nothing changed, so nothing to write and no title to look up.
    assert [p["page"] for p in abs_api.calls_to("/api/me/listening-sessions")] == [0, 1]
    assert not abs_api.calls_to("/api/items/batch/get")
    assert second.points == [] and second.cursor == first.cursor


def test_a_session_uploaded_late_with_an_older_update_is_still_read(abs_api, abs_connector):
    """Listened offline: the mobile app uploads it later, with the device's
    own update time -- older than the newest one already synced."""
    first = abs_connector.sync(None)
    offline = session("s6", "book-dune", ms(2025, 3, 3, 23, 0), 900)
    assert offline["updatedAt"] < first.cursor["sessions_updated_at"]
    abs_api.sessions.append(offline)
    second = abs_connector.sync(first.cursor)
    reading, = by_measurement(lines_of(second), "reading_session")
    assert (reading[3], reading[2]["minutes"]) == (epoch(2025, 3, 3, 23, 0), "15.0")
    assert abs_connector.sync(second.cursor).points == []


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


def test_the_cursor_holds_timestamps_and_opaque_ids_only(abs_api, abs_connector):
    cursor = abs_connector.sync(None).cursor
    newest = max(s["updatedAt"] for s in SESSIONS)
    assert cursor == {
        "sessions_updated_at": newest,
        "progress_updated_at": max(p["lastUpdate"] for p in ME["mediaProgress"]),
        # Within the lookback window (30 days) of the newest update.
        "recent_sessions": {s["id"]: s["updatedAt"] for s in SESSIONS if s["updatedAt"] > newest - 30 * 86_400_000},
    }


def test_titles_come_in_one_batch_or_from_the_libraries_when_refused(abs_api, abs_connector):
    abs_connector.sync(None)
    batch, = abs_api.calls_to("/api/items/batch/get")
    assert batch == {"libraryItemIds": ["book-dune", "book-gone", "pod-1"]}
    assert not abs_api.calls_to("/api/libraries")
    abs_api.calls.clear()
    abs_api.batch_status = 403
    result = abs_connector.sync(None)
    assert abs_api.calls_to("/api/libraries") and abs_api.calls_to("/api/libraries/lib-books/items")
    titles = {f["title"] for _m, _t, f, _ts in by_measurement(lines_of(result), "reading_session")}
    assert titles == {'"Dune"', '"The Podcast"', '"Removed book"'}


def test_page_size_is_what_the_api_is_asked_for(abs_api, abs_connector):
    abs_connector.sync(None)
    assert abs_api.calls_to("/api/me/listening-sessions")[0]["itemsPerPage"] == SESSIONS_PAGE_SIZE


# -- Runs ---------------------------------------------------------------------------------

def test_second_run_writes_nothing_new(abs_api, abs_connector, influx, db):
    run = runner.run(db, abs_connector)
    assert run.status == "ok", run.message
    assert run.points == 5 and run.message == (
        "4 new or updated session(s) (1 empty, skipped), 1 finished book(s) new or changed: "
        "5 point(s) written (MediaStats 5).")
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
        assert fake.calls_to("/api/items/batch/get") == [{"libraryItemIds": ["book-dune", "book-gone", "pod-1"]}]
        # Over HTTP too, a refused batch falls back to the libraries.
        fake.batch_status = 403
        assert len(abs_connector.sync(None).points) == 5
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


def test_a_url_without_scheme_is_explained(abs_connector, monkeypatch, influx, db):
    from pydantic import SecretStr

    monkeypatch.setattr(abs_connector.settings, "url", "audiobookshelf:80")
    monkeypatch.setattr(abs_connector.settings, "token", SecretStr("k"))
    run = runner.run(db, abs_connector)
    assert run.status == "error" and run.message.startswith("Invalid TRIBUTARY_ABS_URL (audiobookshelf:80)")


def test_a_dropped_connection_is_an_error_not_a_crash(abs_connector, monkeypatch):
    import socket
    import threading

    from pydantic import SecretStr

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def hang_up():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.close()

    threading.Thread(target=hang_up, daemon=True).start()
    monkeypatch.setattr(abs_connector.settings, "url", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setattr(abs_connector.settings, "token", SecretStr("k"))
    try:
        with pytest.raises(ConnectorError, match="lost on /api/me|unreachable"):
            abs_connector._get("/api/me")
    finally:
        listener.close()


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
