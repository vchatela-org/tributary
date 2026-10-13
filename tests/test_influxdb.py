from datetime import datetime, timedelta, timezone

import pytest

from app import influxdb, runner
from app.models import Cursor
from tests.fakes import parse


def test_missing_databases_are_created_with_year_long_shards(abs_api, abs_connector, influx, db):
    run = runner.run(db, abs_connector)
    assert run.status == "ok", run.message
    assert [q for _db, q in influx.queries if q.startswith("CREATE")] == [
        'CREATE DATABASE "MediaStats" WITH SHARD DURATION 52w']


def test_existing_databases_are_left_alone(abs_api, abs_connector, influx, db):
    influx.databases.add("MediaStats")  # e.g. created by another tool
    runner.run(db, abs_connector)
    assert not [q for _db, q in influx.queries if q.startswith(("CREATE", "ALTER"))]


def test_creation_can_be_turned_off(abs_api, abs_connector, influx, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.influxdb_create_databases", False)
    runner.run(db, abs_connector)
    assert not [q for _db, q in influx.queries if "DATABASE" in q]


def test_writes_go_in_batches_with_second_precision(abs_api, abs_connector, influx, db, monkeypatch):
    monkeypatch.setattr(influxdb, "BATCH_LINES", 2)
    run = runner.run(db, abs_connector)
    assert run.points == 5
    assert [len(body) for _db, _p, body in influx.writes] == [2, 2, 1]
    assert {(database, precision) for database, precision, _b in influx.writes} == {("MediaStats", "s")}


def test_the_user_tag_when_configured(abs_api, abs_connector, influx, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.influxdb_user", "someone")
    runner.run(db, abs_connector)
    assert all(parse(line)[1]["user"] == "someone" for line in influx.written_lines())


def test_points_beyond_retention_are_skipped_not_fatal(abs_api, abs_connector, influx, db):
    influx.fail["/write"] = (400, '{"error":"partial write: points beyond retention policy dropped=2"}')
    run = runner.run(db, abs_connector)
    assert run.status == "ok", run.message
    assert run.points == 3 and "2 older than their database's retention, skipped." in run.message
    assert db.get(Cursor, "audiobookshelf") is not None


def test_a_field_type_conflict_keeps_the_batch_but_fails_the_run(abs_api, abs_connector, influx, db, monkeypatch):
    monkeypatch.setattr(influxdb, "BATCH_LINES", 2)
    influx.fail["/write"] = (400, '{"error":"partial write: field type conflict: input field \\"minutes\\" on '
                                  'measurement \\"reading_session\\" is type integer, already exists as type float '
                                  'dropped=1"}')
    run = runner.run(db, abs_connector)
    assert run.status == "error"
    # Every batch was still sent (2, 2, 1 lines), one point refused in each.
    assert run.points == 2 and "3 refused (MediaStats: field type conflict" in run.message
    # Read again next time: the cursor did not move.
    assert db.get(Cursor, "audiobookshelf") is None


def test_any_other_refusal_fails_the_run(abs_api, abs_connector, influx, db):
    influx.fail["/write"] = (401, '{"error":"authorization failed"}')
    run = runner.run(db, abs_connector)
    assert run.status == "error" and run.message == "InfluxDB refused /write (401): authorization failed"
    assert db.get(Cursor, "audiobookshelf") is None


def test_query_errors_in_a_200_are_errors(abs_api, abs_connector, influx, db):
    influx.fail["/query"] = (200, '{"results":[{"error":"database not found: x"}]}')
    run = runner.run(db, abs_connector)
    assert run.status == "error" and "database not found" in run.message


def test_unreachable(abs_api, abs_connector, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.influxdb_url", "http://127.0.0.1:9")
    run = runner.run(db, abs_connector)
    assert run.status == "error" and run.message.startswith("InfluxDB unreachable (http://127.0.0.1:9)")


def test_a_url_without_scheme_is_explained(abs_api, abs_connector, db, monkeypatch):
    monkeypatch.setattr("app.config.settings.influxdb_url", "influxdb:8086")
    run = runner.run(db, abs_connector)
    assert run.status == "error" and run.message.startswith("Invalid TRIBUTARY_INFLUXDB_URL (influxdb:8086)")


def test_nothing_to_write_means_no_request(abs_api, abs_connector, influx, db):
    runner.run(db, abs_connector)
    influx.queries.clear()
    run = runner.run(db, abs_connector)
    assert run.message == "Nothing new." and influx.queries == []


def test_drop_source_quotes_its_values(influx):
    influxdb.drop_source("BodyStats", 'we"ird', "it's")
    assert influx.queries[-1] == ("BodyStats", "DROP SERIES FROM \"we\\\"ird\" WHERE \"source\" = 'it\\'s'")


def test_drop_source_over_a_span_deletes_its_points_in_it_only(influx):
    """DROP SERIES takes no time condition: a span is a DELETE, UTC bounds,
    the end excluded."""
    paris = timezone(timedelta(hours=2))
    influxdb.drop_source("BodyStats", "exercise", "it's",
                         datetime(2026, 10, 1, tzinfo=paris), datetime(2026, 10, 5, tzinfo=paris))
    assert influx.queries[-1] == ("BodyStats", "DELETE FROM \"exercise\" WHERE \"source\" = 'it\\'s' "
                                  "AND time >= '2026-09-30T22:00:00Z' AND time < '2026-10-04T22:00:00Z'")


@pytest.mark.parametrize("body, expected", [
    ('{"error":"partial write: points beyond retention policy dropped=3"}', (3, None)),
    ('{"error":"partial write: field type conflict: x dropped=1"}', (1, "field type conflict: x")),
])
def test_partial_write_parsing(influx, body, expected):
    influx.fail["/write"] = (400, body)
    assert influxdb.write("MediaStats", ["m f=1i 1"]) == expected
