"""garmin-grafana's freshness line: when its data last arrived, read from
InfluxDB, timestamps only. Tributary does not run garmin-grafana: the line is a
fact on its card, stale is a warning, and nothing about it can break a page.

Every value below is invented, and chosen to be unmistakable: none of them may
reach a page, the API, the logs or the cache."""
import logging
import socket
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import freshness, influxdb
from app.config import settings
from app.main import app

DATA_UP_TO = datetime(2026, 10, 12, 11, 14, tzinfo=timezone.utc)  # 13:14 in Paris
UPLOADED = datetime(2026, 10, 13, 5, 47, tzinfo=timezone.utc)  # 07:47 in Paris
# Values garmin-grafana would write beside those times. Only the times may leave.
HEART_RATE = 86421
DEVICE_TAG = "zz-fake-watch-7c1"
DEVICE_NAME = "zz-device-label-5e2"
IMAGE_URL = "https://example.invalid/zz-img-3f9.png"
VALUES = (str(HEART_RATE), DEVICE_TAG, DEVICE_NAME, IMAGE_URL)

STATEMENTS = ['SELECT last("HeartRate") FROM "HeartRateIntraday"',
              'SELECT * FROM "DeviceSync" ORDER BY time DESC LIMIT 1']


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def now(monkeypatch):
    """The wall clock the staleness is judged by: a day after the data, by default."""
    moment = {"at": DATA_UP_TO + timedelta(hours=22)}
    monkeypatch.setattr(freshness, "_now", lambda: moment["at"])
    return moment


@pytest.fixture()
def clock(monkeypatch):
    """The cache's clock, moved by hand."""
    tick = {"t": 1000.0}
    monkeypatch.setattr(freshness, "_clock", lambda: tick["t"])
    return tick


def _heart_rate(at, value=HEART_RATE):
    return f"HeartRateIntraday,Device={DEVICE_TAG},Database_Name=GarminStats HeartRate={value}i {int(at.timestamp())}"


def _device_sync(at):
    return (f'DeviceSync,Device={DEVICE_TAG},Database_Name=GarminStats Device_Name="{DEVICE_NAME}",'
            f'imageUrl="{IMAGE_URL}" {int(at.timestamp())}')


@pytest.fixture()
def garmin(influx, now):
    """garmin-grafana's database with an older and a newer heart-rate point,
    and a device sync."""
    influxdb.write("GarminStats", [_heart_rate(DATA_UP_TO - timedelta(hours=3), value=HEART_RATE - 1),
                                   _heart_rate(DATA_UP_TO),
                                   _device_sync(UPLOADED - timedelta(days=2)), _device_sync(UPLOADED)])
    return influx


def _lines(page):
    """Each freshness line on a page, its markup up to the end of its block."""
    return [chunk.split("</div>", 1)[0] for chunk in page.split('class="tb-fresh"')[1:]]


def _reads(influx):
    return [statement for _db, statement in influx.queries]


# -- What the line says ------------------------------------------------------------------

def test_the_line_gives_both_times_on_every_card_of_the_tool(client, garmin):
    page = client.get("/").text
    lines = _lines(page)
    # garmin-grafana is listed under Body & health and under Sleep: a line on each.
    assert len(lines) == 2
    for line in lines:
        assert "Garmin data up to <b>2026-10-12 13:14</b> · last watch upload <b>2026-10-13 07:47</b>" in line
        assert "Over" not in line and "tb-tone-warning" not in line
    # Two cards, one read: each statement once, against its database.
    assert garmin.queries == [("GarminStats", statement) for statement in STATEMENTS]


def test_the_settings_page_lists_it_with_the_compatible_tools(client, garmin):
    page = client.get("/settings").text
    assert len(_lines(page)) == 1
    assert "<strong>garmin-grafana</strong>" in page.split("tb-tools-fresh", 1)[1]
    assert "last watch upload <b>2026-10-13 07:47</b>" in _lines(page)[0]


def test_only_the_data_time_when_no_upload_was_recorded(client, influx, now):
    influxdb.write("GarminStats", [_heart_rate(DATA_UP_TO)])
    [line, _] = _lines(client.get("/").text)
    assert "Garmin data up to <b>2026-10-12 13:14</b>" in line
    assert "last watch upload" not in line


def test_a_tool_without_a_probe_has_no_line(client, garmin):
    """Takeback is listed, with nothing read for it."""
    page = client.get("/").text
    assert "Takeback" in page
    assert len(_lines(page)) == 2  # garmin-grafana's two cards only


# -- Stale is a warning, at the threshold -------------------------------------------------

def test_stale_past_the_threshold_and_fresh_up_to_it(client, garmin, now):
    now["at"] = DATA_UP_TO + timedelta(hours=24) - timedelta(minutes=1)
    fresh = _lines(client.get("/").text)[0]
    assert "tb-tone-warning" not in fresh and "Over" not in fresh

    now["at"] = DATA_UP_TO + timedelta(hours=24) + timedelta(minutes=1)
    stale = _lines(client.get("/").text)[0]
    # The warning's icon and its words, never colour alone; never an error.
    assert 'tb-pill tb-tone-warning"><i class="bi bi-exclamation-circle-fill" aria-hidden="true"></i>Over 24 h old' in stale
    assert "tb-tone-critical" not in stale and "tb-src-err" not in stale
    # The times stay, so the reader sees how old.
    assert "<b>2026-10-12 13:14</b>" in stale


def test_the_threshold_is_a_setting(client, garmin, now, monkeypatch):
    monkeypatch.setattr(settings, "garmin_grafana_stale_hours", 6.0)
    now["at"] = DATA_UP_TO + timedelta(hours=7)
    assert "Over 6 h old" in _lines(client.get("/").text)[0]
    monkeypatch.setattr(settings, "garmin_grafana_stale_hours", 1.5)
    assert "Over 1.5 h old" in _lines(client.get("/").text)[0]


def test_staleness_is_judged_on_the_data_not_the_upload(garmin, now):
    """The newest data decides: an upload the data does not follow is still stale."""
    now["at"] = DATA_UP_TO + timedelta(hours=25)  # the upload is 6 h old by then
    line = freshness.line("garmin-grafana")
    assert line.stale and [seen.at for seen in line.seen] == [DATA_UP_TO, UPLOADED]


# -- Its limit, said on the line ----------------------------------------------------------

def test_the_tooltip_says_when_not_why(client, garmin):
    title = _lines(client.get("/").text)[0].split('title="', 1)[1].split('"', 1)[0]
    assert title.startswith("When data last arrived in GarminStats, not why")
    assert "a stopped fetcher and a watch that has not synced look the same" in title


# -- Settings ----------------------------------------------------------------------------

def test_an_empty_database_setting_turns_the_line_off(client, garmin, monkeypatch):
    monkeypatch.setattr(settings, "garmin_grafana_database", "")
    assert _lines(client.get("/").text) == [] and _lines(client.get("/settings").text) == []
    assert garmin.queries == []
    # garmin-grafana itself is still listed.
    assert "from the Garmin API" in client.get("/").text


def test_another_database_name_is_read_instead(client, influx, now, monkeypatch):
    monkeypatch.setattr(settings, "garmin_grafana_database", "GarminElsewhere")
    influxdb.write("GarminElsewhere", [_heart_rate(DATA_UP_TO)])
    assert "<b>2026-10-12 13:14</b>" in _lines(client.get("/").text)[0]
    assert {db for db, _statement in influx.queries} == {"GarminElsewhere"}


def test_times_are_in_the_configured_time_zone(client, garmin, monkeypatch):
    monkeypatch.setattr(settings, "tz", "America/New_York")
    line = _lines(client.get("/").text)[0]
    assert "Garmin data up to <b>2026-10-12 07:14</b> · last watch upload <b>2026-10-13 01:47</b>" in line


# -- Never in the way: no InfluxDB, a failing one, nothing to read -----------------------

def _nothing_seen(client, path="/"):
    started = time.monotonic()
    response = client.get(path)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    lines = _lines(response.text)
    assert lines and all("No Garmin data seen" in line and "tb-fresh-none" in line for line in lines)
    assert all("tb-tone-warning" not in line and "tb-tone-critical" not in line for line in lines)
    return lines[0], elapsed


def test_no_influxdb_reads_as_nothing_seen(client):
    assert not influxdb.is_configured()
    line, _ = _nothing_seen(client)
    assert "InfluxDB is not configured" in line
    _nothing_seen(client, "/settings")


def test_an_unreachable_influxdb_reads_as_nothing_seen_without_slowing_the_page(client, monkeypatch):
    with socket.socket() as probe:  # a local port nothing listens on any more
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "influxdb_url", f"http://127.0.0.1:{port}")
    line, elapsed = _nothing_seen(client)
    assert "InfluxDB could not be read" in line and elapsed < 1.5
    _nothing_seen(client, "/settings")


def test_a_slow_influxdb_is_given_up_on_quickly(client, garmin, monkeypatch):
    monkeypatch.setattr(freshness, "TIMEOUT_SECONDS", 0.3)
    garmin.hold = 10.0
    line, elapsed = _nothing_seen(client)
    assert "InfluxDB could not be read" in line
    assert elapsed < 2.0
    # The failure is remembered too: the next page does not wait again.
    _, again = _nothing_seen(client)
    assert again < 0.5


def test_an_influxdb_error_reads_as_nothing_seen(client, garmin):
    garmin.fail["/query"] = (500, '{"error": "something broke"}')
    line, _ = _nothing_seen(client)
    assert "InfluxDB could not be read" in line


def test_a_missing_database_reads_as_nothing_seen(client, influx):
    line, _ = _nothing_seen(client)
    assert "InfluxDB has no GarminStats database" in line
    # It stops at the first answer: the second would only say the same.
    assert _reads(influx) == STATEMENTS[:1]


def test_a_database_without_points_reads_as_nothing_seen(client, influx):
    influx.databases.add("GarminStats")
    line, _ = _nothing_seen(client)
    assert "no point in GarminStats yet" in line


# -- Cheap: cached, and never read by the poll -------------------------------------------

def test_answers_are_cached_for_a_few_minutes(client, garmin, clock):
    client.get("/")
    client.get("/settings")
    client.get("/")
    assert _reads(garmin) == STATEMENTS

    # A new upload shows once the cache has expired, not before.
    influxdb.write("GarminStats", [_heart_rate(DATA_UP_TO + timedelta(hours=5))])
    clock["t"] += freshness.CACHE_SECONDS - 1
    assert "<b>2026-10-12 13:14</b>" in _lines(client.get("/").text)[0]
    assert _reads(garmin) == STATEMENTS
    clock["t"] += 2
    assert "Garmin data up to <b>2026-10-12 18:14</b>" in _lines(client.get("/").text)[0]
    assert _reads(garmin) == STATEMENTS * 2


def test_the_polled_fragment_never_reads_influxdb(client, garmin):
    for _ in range(3):
        fragment = client.get("/partials/status")
        assert fragment.status_code == 200 and "tb-fresh" not in fragment.text
    assert garmin.queries == []


def test_viewing_the_line_writes_nothing(client, garmin):
    """Read-only: SELECTs only, and no write beyond the test's own seeding."""
    writes = list(garmin.writes)
    client.get("/")
    client.get("/settings")
    assert all(statement.startswith("SELECT ") for statement in _reads(garmin))
    assert garmin.writes == writes


# -- Privacy: times only ------------------------------------------------------------------

def test_no_value_reaches_a_page_the_api_the_logs_or_the_cache(client, garmin, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = [client.get(path).text for path in ("/", "/settings", "/partials/status", "/api/connectors",
                                                 "/api/runs", "/api/health", "/api/preferences",
                                                 "/openapi.json")]
    assert "2026-10-13 07:47" in bodies[0]  # the times did arrive
    cached = repr(freshness._cache)
    assert "2026, 10, 13, 5, 47" in cached  # ... and only they are kept
    # A failing read logs too, and still nothing of what came back.
    freshness.clear()
    garmin.fail["/query"] = (400, '{"error": "something broke"}')
    bodies.append(client.get("/").text)
    for value in VALUES:
        for body in bodies:
            assert value not in body
        assert value not in caplog.text
        assert value not in cached
