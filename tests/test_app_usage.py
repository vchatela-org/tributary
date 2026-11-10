"""App Usage: the phone's screen time from the app's backup, imported in the UI."""
import threading
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from app import connectors, runner
from app.config import settings
from app.connectors import play_store
from app.connectors.app_usage import day_of
from app.connectors.app_usage_categories import CATEGORIES as VOCABULARY, KNOWN, LEAN_BACK, categorize
from tests.fakes import epoch, ms, parse

USER = "alice"


def key(day: date) -> str:
    """The backup's day: year << 9 | day of the year, 0-based."""
    return str(day.year << 9 | (day.timetuple().tm_yday - 1))


def table(columns, rows):
    """The app's CSV dialect: tab-separated, every value quoted."""
    return "\n".join("\t".join(f'"{v}"' for v in row) for row in [columns, *rows]) + "\n"


PKGS = [
    # pid, package, name, category, user category
    ("1", "org.example.chat", "Chatter", "4", "-1"),
    ("2", "org.example.reader", "Reader", "-1", "7"),   # the user filed it under productivity
    ("3", "org.example.launcher", "", "-1", "-1"),      # no name: its package stands in
]
CATEGORIES = [("-1", "OTHERS", "1"), ("4", "SOCIAL", "1"), ("7", "PRODUCTIVITY", "1")]
DAY1, DAY2 = date(2026, 3, 10), date(2026, 3, 11)
USAGE = [
    # pid, day, ms in the foreground, launches, notifications
    ("1", key(DAY1), "1800500", "12", "3"),
    ("2", key(DAY1), "600000", "2", "0"),
    ("3", key(DAY1), "300000", "20", "0"),
    ("1", key(DAY2), "900000", "5", "0"),
]
CHECKY = [(key(DAY1), "31", "4200999"), (key(DAY2), "17", "1500000")]
# 2026-03-10 20:30:00 UTC = 21:30 in Paris (CET).
EVENING = ms(2026, 3, 10, 20, 30)
ACT = [
    # time, type, pid, duration, flag
    ("1788818400000", "11", "-1", "0", "0"),        # the app's own marker
    (str(EVENING), "6", "-1", "60000", "256"),      # the screen off
    (str(EVENING + 60000), "5", "-1", "600", "1536"),   # an unlock
    (str(EVENING + 61000), "7", "1", "95250", "0"),     # Chatter, 95.25 s
    (str(EVENING + 61500), "7", "1", "4750", "0"),      # again in the same second: adds up
    (str(EVENING + 70000), "7", "2", "0", "0"),         # no time at all: left out
    (str(EVENING + 400000), "5", "-1", "0", "2304"),    # the screen woken, still locked: no unlock
    (str(EVENING + 500000), "5", "-1", "0", "1280"),    # another way of unlocking
    (str(EVENING + 501000), "7", "99", "1000", "0"),    # a package the backup doesn't list
]
PLACE = [("1700000000000", "48.85", "2.35", "100")]


def write_backup(tmp_path, name="AUM_V4_2026-03-12_03-31-00.zip", pkgs=PKGS, usage=USAGE, checky=CHECKY,
                 act=ACT, drop=(), act_columns=("time", "type", "pid", "duration", "type_flag")):
    files = {
        "version.csv": '"Created by App Usage (PRO) on Thursday, March 12, 2026 at 03:31"\n"version=14"\n',
        "pkgs.csv": table(("pid", "pkg_name", "app_name", "version", "update_time", "type", "ai_flag", "note",
                           "access_time", "category", "user_category", "version_code"),
                          [(p, pkg, n, "1.0", "0", "3", "0", "", "0", c, uc, "1") for p, pkg, n, c, uc in pkgs]),
        "categories.csv": table(("cid", "name", "sys"), CATEGORIES),
        "usage.csv": table(("pid", "time", "usage_time", "access_count", "noty_count"), usage),
        "checky.csv": table(("time", "ccount", "ctime"), checky),
        "act.csv": table(act_columns, [row[:len(act_columns)] for row in act]),
        "place.csv": table(("time", "lat", "lng", "radius"), PLACE),
        "installs.csv": table(("pid", "version", "type", "update_time", "version_code"),
                              [("1", "1.0", "3", "1700000000000", "1")]),
        "battery.csv": table(("time", "flag", "level"), [("1700000000000", "43264", "71")]),
    }
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as z:
        for member, text in files.items():
            if member not in drop:
                z.writestr(member, text)
        z.writestr("icon/org.example.chat.png", b"\x89PNG")
    return target


@pytest.fixture()
def connector(monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return connectors.get("app_usage")


def _import(db, connector, path, **options):
    return runner.run(db, connector, file=str(path), filename=path.name, trigger="upload", **options)


def _lines(influx, measurement):
    return [parse(line) for line in influx.written_lines() if line.startswith(measurement + ",")]


def test_a_day_is_the_year_shifted_and_its_day_of_the_year_from_zero():
    assert day_of(str(2026 << 9 | 272)) == date(2026, 9, 30)
    assert day_of(str(2026 << 9 | 0)) == date(2026, 1, 1)
    assert day_of(key(date(2024, 12, 31))) == date(2024, 12, 31)  # a leap year's 366th day
    for wrong in (str(2026 << 9 | 365), str(2026 << 9 | 400), "12"):
        with pytest.raises(ValueError):
            day_of(wrong)


def test_a_backup_writes_the_days_the_apps_their_sessions_and_the_unlocks(connector, influx, db, tmp_path):
    run = _import(db, connector, write_backup(tmp_path))
    assert run.status == "ok", run.message
    assert run.message.startswith("Imported AUM_V4_2026-03-12_03-31-00.zip, 2 day(s), 3 app(s) known, "
                                  "2 session(s), 2 unlock(s), 1 package id(s) not in pkgs.csv left out: ")

    # Local midnight: 2026-03-10 00:00 in Paris is 2026-03-09 23:00 UTC.
    midnight1, midnight2 = epoch(2026, 3, 9, 23), epoch(2026, 3, 10, 23)
    days = sorted(_lines(influx, "screen_time"), key=lambda p: p[3])
    assert days == [
        ("screen_time", {"source": "app_usage", "user": USER},
         {"unlocks": "31i", "screen_on_s": "4200i", "app_s": "2700i", "launches": "34i", "notifications": "3i"},
         midnight1),
        ("screen_time", {"source": "app_usage", "user": USER},
         {"unlocks": "17i", "screen_on_s": "1500i", "app_s": "900i", "launches": "5i", "notifications": "0i"},
         midnight2),
    ]

    apps = {(t["package"], ts): (t, f) for _m, t, f, ts in _lines(influx, "app_usage")}
    assert len(apps) == 4
    assert apps[("org.example.chat", midnight1)] == (
        {"source": "app_usage", "user": USER, "app": "Chatter", "package": "org.example.chat", "category": "social"},
        {"foreground_s": "1800i", "launches": "12i", "notifications": "3i"})
    assert apps[("org.example.reader", midnight1)][0]["category"] == "productivity"  # the user's choice
    assert apps[("org.example.launcher", midnight1)][0]["app"] == "org.example.launcher"
    assert apps[("org.example.launcher", midnight1)][0]["category"] == "other"

    sessions = _lines(influx, "app_session")
    assert sessions == [(
        "app_session",
        {"source": "app_usage", "user": USER, "app": "Chatter", "package": "org.example.chat", "category": "social",
         "time_precision": "second"},
        # 95.2 + 4.8: two sessions starting in the same second add up.
        {"count": "2i", "duration_s": "100.0", "local_hour": "21i", "local_weekday": "2i"},
        epoch(2026, 3, 10, 20, 31, 1))]

    unlocks = _lines(influx, "unlock")
    assert [(t, f, ts) for _m, t, f, ts in unlocks] == [
        ({"source": "app_usage", "user": USER, "time_precision": "second"},
         {"count": "1i", "local_hour": "21i", "local_weekday": "2i"}, epoch(2026, 3, 10, 20, 31)),
        ({"source": "app_usage", "user": USER, "time_precision": "second"},
         {"count": "1i", "local_hour": "21i", "local_weekday": "2i"}, epoch(2026, 3, 10, 20, 38, 20)),
    ]


def test_an_import_replaces_the_days_the_backup_covers_for_its_user_only(connector, influx, db, tmp_path):
    """Before its first day and for another user, nothing is touched; an app
    renamed since the last import leaves no old series behind on the days the
    backup holds."""
    before = epoch(2026, 3, 1, 23)
    stale = ("DigitalStats", "app_usage", (("app", "Old name"), ("category", "social"),
                                           ("package", "org.example.chat"), ("source", "app_usage"),
                                           ("user", USER)), epoch(2026, 3, 9, 23))
    influx.points[stale] = {"foreground_s": "1i"}
    kept = ("DigitalStats", "screen_time", (("source", "app_usage"), ("user", USER)), before)
    influx.points[kept] = {"unlocks": "9i"}
    bob = ("DigitalStats", "screen_time", (("source", "app_usage"), ("user", "bob")), epoch(2026, 3, 9, 23))
    influx.points[bob] = {"unlocks": "4i"}

    run = _import(db, connector, write_backup(tmp_path))
    assert run.status == "ok", run.message
    deletes = [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]
    assert deletes == [
        f'DELETE FROM "{m}" WHERE "source" = \'app_usage\' AND "user" = \'{USER}\' '
        "AND time >= '2026-03-09T23:00:00Z' AND time < '" + end + "'"
        for m, end in (("screen_time", "2026-03-11T23:00:00Z"), ("app_usage", "2026-03-11T23:00:00Z"),
                       ("app_session", "2026-03-10T23:00:00Z"), ("unlock", "2026-03-10T23:00:00Z"))]
    assert stale not in influx.points
    assert influx.points[kept] == {"unlocks": "9i"} and influx.points[bob] == {"unlocks": "4i"}

    # The same backup again rewrites the same points.
    stored = influx.stored()
    assert _import(db, connector, write_backup(tmp_path)).status == "ok"
    assert influx.stored() == stored


def test_the_user_tag_is_required(connector, influx, db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", "")
    run = _import(db, connector, write_backup(tmp_path))
    assert run.status == "error" and "TRIBUTARY_INFLUXDB_USER is not set" in run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


@pytest.mark.parametrize("options, message", [
    ({"drop": ("usage.csv",)}, "not an App Usage backup"),
    ({"drop": ("version.csv",)}, "not an App Usage backup"),
    ({"act_columns": ("time", "type", "pid", "duration")}, "act.csv has no type_flag column"),
    ({"usage": [], "checky": [], "act": []}, "holds nothing to import"),
])
def test_what_is_not_a_backup_is_refused_and_nothing_is_dropped(connector, influx, db, tmp_path, options, message):
    run = _import(db, connector, write_backup(tmp_path, **options))
    assert run.status == "error" and message in run.message, run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_a_file_that_is_not_a_zip_is_refused(connector, influx, db, tmp_path):
    path = tmp_path / "AUM_V4_broken.zip"
    path.write_bytes(b"not a zip")
    run = _import(db, connector, path)
    assert run.status == "error" and "unreadable" in run.message


def test_places_installs_and_battery_never_leave(connector, influx, db, tmp_path):
    assert _import(db, connector, write_backup(tmp_path)).status == "ok"
    written = "\n".join(influx.written_lines())
    assert {line.split(",", 1)[0] for line in influx.written_lines()} == {
        "screen_time", "app_usage", "app_session", "unlock"}
    for value in ("48.85", "2.35", "1700000000", "43264", "PNG"):
        assert value not in written


def test_it_takes_uploads_only(connector, tmp_path):
    """Each daily backup holds the whole history so far: not what a watched
    folder's whole-source rebuild assumes."""
    assert connector.file_types == (".zip",) and connector.default_schedule is None
    connector.settings.watch_dir = str(tmp_path)
    try:
        assert connector.watch_dir is None and connector.is_configured() is False
    finally:
        connector.settings.watch_dir = None


def test_an_app_s_category_is_yours_then_the_one_you_chose_in_the_app_then_tributary_s_then_the_app_s():
    mine = {"org.example.tv": "watching"}
    assert categorize("org.example.tv", "SOCIAL", "VIDEO", mine) == "watching"
    # Your choice in App Usage, in its words or in a category you named after one of ours.
    assert categorize("com.google.android.youtube", "MAPS", "VIDEO", {}) == "navigation"
    assert categorize("org.example.cam", "Smart_Home", None, {}) == "smart_home"
    # A category of your own that is none of ours: the next rule decides.
    assert categorize("com.google.android.gm", "Mine", "SOCIAL", {}) == "communication"
    # Well-known apps, whatever App Usage files them under (it says SOCIAL for a browser).
    assert categorize("com.brave.browser", None, "SOCIAL", {}) == "browsing"
    assert categorize("org.example.game", None, "GAME", {}) == "games"
    assert categorize("com.miui.weather2", None, "OTHERS", {}) == "system"
    assert categorize("org.example.app", None, "OTHERS", {}) == "other"
    assert categorize("org.example.app", None, None, {}) == "other"


def test_the_vocabulary_is_closed():
    assert set(KNOWN.values()) <= set(VOCABULARY) and set(LEAN_BACK) <= set(VOCABULARY)
    assert len(VOCABULARY) == len(set(VOCABULARY))


def test_your_categories_come_from_the_environment(connector, influx, db, tmp_path, monkeypatch):
    monkeypatch.setattr(connector.settings, "categories", '{"org.example.chat": "Watching"}')
    assert connector.import_fingerprint() == {"categories": '{"org.example.chat": "Watching"}', "play_lookup": False}
    assert _import(db, connector, write_backup(tmp_path)).status == "ok"
    assert {t["category"] for _m, t, _f, _ts in _lines(influx, "app_session")} == {"watching"}


@pytest.mark.parametrize("value, message", [
    ('{"org.example.chat": "tv"}', "'tv' is not a category"),
    ('["watching"]', "must be a JSON object"),
    ("{not json", "not valid JSON"),
])
def test_a_wrong_category_fails_the_import_before_anything_is_dropped(connector, influx, db, tmp_path, monkeypatch,
                                                                        value, message):
    monkeypatch.setattr(connector.settings, "categories", value)
    run = _import(db, connector, write_backup(tmp_path))
    assert run.status == "error" and message in run.message, run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


# ── Google Play ─────────────────────────────────────────────────────────────

class FakePlay:
    """play.google.com's app page: its structured data's category, a 404 for
    an app it doesn't have, or an error for every page (`fail`)."""

    def __init__(self, genres):
        self.genres, self.asked, self.fail, self.agents = dict(genres), [], None, set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                query = parse_qs(urlparse(self.path).query)
                outer.asked.append(query["id"][0])
                outer.agents.add(self.headers.get("User-Agent"))
                if outer.fail:
                    self.send_response(outer.fail)
                    self.end_headers()
                    return
                genre = outer.genres.get(query["id"][0])
                if genre is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = ('<html><script type="application/ld+json">{"@type":"SoftwareApplication",'
                        f'"applicationCategory":"{genre}"}}</script></html>').encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/store/apps/details"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def play(monkeypatch, tmp_path):
    # org.example.chat is decided by App Usage's own SOCIAL... after Google Play;
    # the reader by the user's choice in App Usage: never asked.
    fake = FakePlay({"org.example.chat": "VIDEO_PLAYERS", "org.example.launcher": "TOOLS"})
    monkeypatch.setattr(play_store, "PLAY_URL", fake.url)
    monkeypatch.setattr(play_store, "DELAY_S", 0)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    yield fake
    fake.close()


def _categories(influx):
    return {t["package"]: t["category"] for _m, t, _f, _ts in _lines(influx, "app_usage")}


def test_google_play_is_never_asked_unless_turned_on(connector, influx, db, tmp_path, play):
    assert _import(db, connector, write_backup(tmp_path)).status == "ok"
    assert play.asked == []


def test_google_play_s_category_comes_after_yours_and_the_built_in_list(connector, influx, db, tmp_path, play,
                                                                       monkeypatch):
    monkeypatch.setattr(connector.settings, "play_lookup", True)
    pkgs = PKGS + [("4", "com.brave.browser", "Brave", "4", "-1"), ("5", "org.example.gone", "Gone", "-1", "-1")]
    usage = USAGE + [("4", key(DAY1), "1000", "1", "0"), ("5", key(DAY1), "1000", "1", "0")]
    play.genres["com.brave.browser"] = "COMMUNICATION"
    run = _import(db, connector, write_backup(tmp_path, pkgs=pkgs, usage=usage))
    assert run.status == "ok", run.message
    assert "Google Play: 3 app(s) looked up, 1 not on it" in run.message
    # The reader (the user's choice in App Usage) and Brave (built-in) are never asked.
    assert sorted(play.asked) == ["org.example.chat", "org.example.gone", "org.example.launcher"]
    assert play.agents == {play_store.USER_AGENT}
    assert _categories(influx) == {
        "org.example.chat": "watching",          # Google Play over App Usage's SOCIAL
        "org.example.reader": "productivity",
        "org.example.launcher": "system",        # TOOLS
        "com.brave.browser": "browsing",         # the built-in list over COMMUNICATION
        "org.example.gone": "other",             # not on Google Play: the next rules
    }

    # Cached: the same backup again asks nothing, and writes the same.
    play.asked.clear()
    run = _import(db, connector, write_backup(tmp_path, pkgs=pkgs, usage=usage))
    assert run.status == "ok" and play.asked == [] and "Google Play" not in run.message
    assert _categories(influx)["org.example.chat"] == "watching"


def test_an_unreachable_google_play_never_fails_the_import(connector, influx, db, tmp_path, play, monkeypatch):
    monkeypatch.setattr(connector.settings, "play_lookup", True)
    play.fail = 503
    run = _import(db, connector, write_backup(tmp_path))
    assert run.status == "ok", run.message
    assert "Google Play unreachable (HTTP 503): 2 app(s) left to the next import" in run.message
    assert len(play.asked) == 1  # stops at the first failure
    assert _categories(influx)["org.example.chat"] == "social"  # App Usage's own, meanwhile
    # Nothing cached: the next import asks again.
    play.fail = None
    play.asked.clear()
    assert _import(db, connector, write_backup(tmp_path)).status == "ok"
    assert sorted(play.asked) == ["org.example.chat", "org.example.launcher"]


def test_a_lookup_stops_at_its_budget_and_the_cache_expires(tmp_path, play, monkeypatch):
    monkeypatch.setattr(play_store, "MAX_LOOKUPS", 1)
    found = play_store.lookup(["org.example.chat", "org.example.launcher", "not a package"], str(tmp_path),
                              today=date(2026, 3, 1))
    assert found.genres == {"org.example.chat": "VIDEO_PLAYERS"}
    assert found.summary == ["Google Play: 1 app(s) looked up, 0 not on it",
                             "Google Play: 1 app(s) left to the next import"]
    assert play.asked == ["org.example.chat"]  # "not a package" never leaves
    play.asked.clear()
    play_store.lookup(["org.example.chat"], str(tmp_path), today=date(2026, 3, 1) + date.resolution * 179)
    assert play.asked == []
    play_store.lookup(["org.example.chat"], str(tmp_path), today=date(2026, 3, 1) + date.resolution * 180)
    assert play.asked == ["org.example.chat"]


def test_google_play_s_categories_map_to_tributary_s():
    assert play_store.category_of("GAME_PUZZLE") == "games"
    assert play_store.category_of("VIDEO_PLAYERS") == "watching"
    assert play_store.category_of("SOMETHING_NEW") is None and play_store.category_of(None) is None
    assert set(play_store.GENRES.values()) <= set(VOCABULARY)
