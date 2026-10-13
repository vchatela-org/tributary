"""Test doubles: a fake InfluxDB 1.x, a fake Audiobookshelf server, a fake
Renpho cloud and a fake Todoist API, real HTTP servers in a thread (the code under test speaks HTTP
to them).

The Audiobookshelf one also runs on its own, to try Tributary end to end
without a real server:

    python -m tests.fakes --port 13378 --token demo-key

then TRIBUTARY_ABS_URL=http://<host>:13378 and TRIBUTARY_ABS_TOKEN=demo-key.
"""
import argparse
import copy
import csv
import io
import itertools
import json
import re
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


def ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


# ────────────────────────────────────────────────
# Line protocol parsing (what InfluxDB would read)
# ────────────────────────────────────────────────

def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse(line: str) -> Tuple[str, Dict[str, str], Dict[str, str], int]:
    """'m,t=v f=1i 123' -> (measurement, tags, fields, ts), tag values
    unescaped, field values as written (strings keep their quotes)."""
    head, fields, ts = re.match(r'^((?:[^ \\]|\\.)+) (.*) (\d+)$', line).groups()
    measurement, *tags = re.split(r"(?<!\\),", head)
    tag_values = dict(map(_unescape, re.split(r"(?<!\\)=", t, maxsplit=1)) for t in tags)
    field_values = dict(re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|[^,]+)', fields))
    return _unescape(measurement), tag_values, field_values, int(ts)


# ────────────────────────────────────────────────
# InfluxDB
# ────────────────────────────────────────────────

class FakeInflux:
    """Keeps points by key (database, measurement, tags, time) as InfluxDB
    does -- writing the same key overwrites --, records every /query
    statement and /write body; `fail[path] = (code, body)` makes a path
    answer an error."""

    def __init__(self):
        self.queries: List[Tuple[Optional[str], str]] = []
        self.writes: List[Tuple[str, str, List[str]]] = []
        self.fail: Dict[str, Tuple[int, str]] = {}
        self.databases = {"_internal"}
        self.points: Dict[Tuple, Dict[str, str]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if url.path in outer.fail:
                    code, payload = outer.fail[url.path]
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(payload.encode())
                    return
                if url.path == "/query":
                    result = outer._query(params.get("db"), params["q"])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"results": [result]}).encode())
                    return
                outer.writes.append((params["db"], params["precision"], body.split("\n")))
                for line in body.split("\n"):
                    measurement, tags, fields, ts = parse(line)
                    outer.points[(params["db"], measurement, tuple(sorted(tags.items())), ts)] = fields
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _query(self, database: Optional[str], statement: str) -> Dict:
        self.queries.append((database, statement))
        result: Dict = {"statement_id": 0}
        if statement == "SHOW DATABASES":
            result["series"] = [{"name": "databases", "columns": ["name"],
                                 "values": [[name] for name in sorted(self.databases)]}]
        elif statement.startswith("CREATE DATABASE"):
            self.databases.add(statement.split('"')[1])
        elif statement.startswith("DROP SERIES FROM"):
            match = re.match(r'DROP SERIES FROM "(.+)" WHERE "source" = \'(.+)\'$', statement)
            measurement, source = match.groups()
            for key in [k for k in self.points if k[0] == database and k[1] == measurement
                        and dict(k[2]).get("source") == source]:
                del self.points[key]
        elif statement.startswith("DELETE FROM"):
            match = re.match(r'DELETE FROM "(.+)" WHERE "source" = \'(.+)\' '
                             r"AND time >= '(.+)' AND time < '(.+)'$", statement)
            measurement, source, start, end = match.groups()
            start, end = (int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()) for t in (start, end))
            for key in [k for k in self.points if k[0] == database and k[1] == measurement
                        and dict(k[2]).get("source") == source and start <= k[3] < end]:
                del self.points[key]
        else:
            result.update(self._select(database, statement))
        return result

    def _select(self, database: Optional[str], statement: str) -> Dict:
        """The one SELECT a connector makes today: a single field of a
        measurement, filtered on two tags (the MyFitnessPal weigh-in dedup).
        Answers one merged series, no tags, RFC3339 times -- as InfluxDB does
        without `GROUP BY *` and without `epoch=s`."""
        match = re.match(r'SELECT "(\w+)" FROM "(\w+)" WHERE "(\w+)" = \'([^\']*)\''
                         r' AND "(\w+)" != \'([^\']*)\'$', statement)
        if match is None:
            return {}
        field, measurement, t1, v1, t2, v2 = match.groups()
        values = []
        for (db, m, tags, ts), fields in self.points.items():
            tags = dict(tags)
            if db != database or m != measurement or tags.get(t1) != v1 or tags.get(t2) == v2:
                continue
            if field in fields:
                raw = fields[field]
                values.append([datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               int(raw[:-1]) if raw.endswith("i") else float(raw)])
        if not values:
            return {}
        return {"series": [{"name": measurement, "columns": ["time", field], "values": values}]}

    def stored(self, measurement: Optional[str] = None) -> List[Tuple[Dict[str, str], Dict[str, str], int]]:
        """(tags, fields, ts) of the points InfluxDB would hold, oldest first."""
        return sorted(((dict(tags), fields, ts) for (_db, m, tags, ts), fields in self.points.items()
                       if measurement in (None, m)), key=lambda p: (p[2], sorted(p[0].items())))

    def written_lines(self) -> List[str]:
        return [line for _db, _p, body in self.writes for line in body]

    def close(self):
        self.server.shutdown()


# ────────────────────────────────────────────────
# Audiobookshelf
# ────────────────────────────────────────────────

SECRETS = ("old-non-expiring-token", "reader@example.com", "203.0.113.20", "the-api-key")


def _book(item_id, title, author, added, **meta):
    return {
        "id": item_id, "libraryId": "lib-books", "mediaType": "book", "addedAt": added, "updatedAt": added,
        "path": f"/audiobooks/{author}/{title}", "relPath": f"{author}/{title}", "ino": "123",
        "media": {"id": f"m-{item_id}", "duration": 36000.5,
                  "metadata": {"title": title, "authorName": author, **meta}},
    }


ITEMS = [
    _book("book-dune", "Dune", "Frank Herbert", ms(2025, 1, 10), narratorName="Simon Vance"),
    _book("book-never", "Never listened", "Anne Author", ms(2025, 2, 1)),
    _book("book-elsewhere", "Started elsewhere", "Paul Writer", ms(2025, 2, 2)),
    {"id": "pod-1", "libraryId": "lib-pods", "mediaType": "podcast", "addedAt": ms(2025, 1, 1),
     "updatedAt": ms(2025, 1, 1), "media": {"metadata": {"title": "The Podcast", "author": "Radio"}, "numEpisodes": 3}},
]


def session(session_id, item_id, started, seconds, **extra):
    return {
        "id": session_id, "userId": "user-1", "libraryId": "lib-books", "libraryItemId": item_id,
        "bookId": f"m-{item_id}", "episodeId": None, "mediaType": "book",
        "mediaMetadata": {"title": "Dune (old title)", "narrators": ["Simon Vance"]},
        "chapters": [{"id": 0, "start": 0, "end": 600, "title": "Chapter 1"}],
        "displayTitle": "Dune (old title)", "displayAuthor": "Frank Herbert", "coverPath": "/metadata/c.jpg",
        "duration": 36000.5, "playMethod": 0, "mediaPlayer": "exoplayer",
        "deviceInfo": {"id": "dev-1", "userId": "user-1", "deviceId": "abc", "ipAddress": "203.0.113.20",
                       "manufacturer": "Google", "model": "Pixel 7", "clientName": "Abs Android",
                       "clientVersion": "0.10.0"},
        "serverVersion": "2.37.1", "date": "2025-03-01", "dayOfWeek": "Saturday",
        "timeListening": seconds, "startTime": 0, "currentTime": seconds,
        "startedAt": started, "updatedAt": started + int(seconds * 1000) + 1000, **extra,
    }


SESSIONS = [
    session("s1", "book-dune", ms(2025, 3, 1, 20, 0), 1800),
    session("s2", "book-dune", ms(2025, 3, 2, 21, 30), 600,
            deviceInfo={"deviceName": "Bedroom tablet", "manufacturer": "Lenovo", "ipAddress": "203.0.113.20"}),
    # A podcast episode: the show is the title, the episode is not sent.
    session("s3", "pod-1", ms(2025, 3, 3, 8, 0), 1200, mediaType="podcast", episodeId="ep-1", bookId=None,
            libraryId="lib-pods", displayTitle="Episode 1", displayAuthor="Radio",
            mediaMetadata={"title": "The Podcast", "author": "Radio"},
            deviceInfo={"browserName": "Firefox", "osName": "Linux", "clientName": "Abs Web",
                        "ipAddress": "203.0.113.20"}),
    # Opened and closed straight away.
    session("s4", "book-dune", ms(2025, 3, 4, 9, 0), 0),
    # A book since removed from the server: its sessions still name it.
    session("s5", "book-gone", ms(2025, 2, 20, 18, 0), 900,
            mediaMetadata={"title": "Removed book"}, displayTitle="Removed book", displayAuthor="X"),
]

ME = {
    "id": "user-1", "username": "reader", "email": "reader@example.com", "type": "admin",
    "token": "old-non-expiring-token",
    "mediaProgress": [
        {"id": "p1", "userId": "user-1", "libraryItemId": "book-dune", "episodeId": None, "mediaItemId": "m-book-dune",
         "mediaItemType": "book", "duration": 36000.5, "progress": 1, "currentTime": 36000, "isFinished": True,
         "lastUpdate": ms(2025, 3, 5, 22, 0, 30), "startedAt": ms(2025, 3, 1, 20, 0),
         "finishedAt": ms(2025, 3, 5, 22, 0)},
        {"id": "p2", "userId": "user-1", "libraryItemId": "book-elsewhere", "episodeId": None,
         "mediaItemId": "m-book-elsewhere", "mediaItemType": "book", "progress": 0.4, "isFinished": False,
         "lastUpdate": ms(2025, 3, 6), "startedAt": ms(2025, 3, 6), "finishedAt": None},
        # A finished podcast episode is not a finished book.
        {"id": "p3", "userId": "user-1", "libraryItemId": "pod-1", "episodeId": "ep-1", "mediaItemId": "ep-1",
         "mediaItemType": "podcastEpisode", "progress": 1, "isFinished": True, "lastUpdate": ms(2025, 3, 3, 9),
         "finishedAt": ms(2025, 3, 3, 9)},
    ],
    "bookmarks": [{"libraryItemId": "book-dune", "title": "Key passage", "time": 900, "createdAt": ms(2025, 3, 1)}],
}

LIBRARIES = [
    {"id": "lib-books", "name": "Audiobooks", "mediaType": "book", "folders": [{"fullPath": "/audiobooks"}]},
    {"id": "lib-pods", "name": "Podcasts", "mediaType": "podcast", "folders": [{"fullPath": "/podcasts"}]},
]


class FakeAbs:
    """Audiobookshelf's API: GET path -> payload, in its shapes, with
    paging. Callable as the connector's `_get`, or served over HTTP."""

    def __init__(self, sessions=SESSIONS, items=ITEMS, me=ME, token="the-api-key"):
        self.sessions = copy.deepcopy(list(sessions))
        self.items, self.me, self.token = copy.deepcopy(items), copy.deepcopy(me), token
        self.calls: List[Tuple[str, Dict]] = []
        self.server: Optional[HTTPServer] = None
        # Answer POST /api/items/batch/get with this status instead (e.g.
        # 403: one of the items is no longer the user's to open).
        self.batch_status: Optional[int] = None

    def post(self, path, body):
        """POST path -> payload; a refusal raises the connector's ApiError,
        as its _request would."""
        self.calls.append((path, dict(body)))
        if path == "/api/items/batch/get":
            if self.batch_status:
                from app.connectors.audiobookshelf import ApiError

                raise ApiError(f"refused ({self.batch_status})", self.batch_status)
            wanted = set(body["libraryItemIds"])
            return {"libraryItems": copy.deepcopy([i for i in self.items if i["id"] in wanted])}
        raise LookupError(path)

    def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "/api/me":
            return copy.deepcopy(self.me)
        if path == "/api/me/listening-sessions":
            # Newest update first, as the API does.
            ordered = sorted(self.sessions, key=lambda s: -s["updatedAt"])
            size, page = int(params["itemsPerPage"]), int(params["page"])
            return {"total": len(ordered), "numPages": -(-len(ordered) // size), "page": page,
                    "itemsPerPage": size, "sessions": copy.deepcopy(ordered[page * size:(page + 1) * size])}
        if path == "/api/libraries":
            return {"libraries": copy.deepcopy(LIBRARIES)}
        match = re.fullmatch(r"/api/libraries/([^/]+)/items", path)
        if match:
            mine = [i for i in self.items if i["libraryId"] == unquote(match.group(1))]
            size, page = int(params["limit"]), int(params["page"])
            return {"results": copy.deepcopy(mine[page * size:(page + 1) * size]), "total": len(mine),
                    "limit": size, "page": page}
        raise LookupError(path)

    def calls_to(self, path: str) -> List[Dict]:
        return [params for p, params in self.calls if p == path]

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    payload = outer(url.path, {k: v[0] for k, v in parse_qs(url.query).items()})
                except LookupError:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if outer.batch_status:
                    self.send_response(outer.batch_status)
                    self.end_headers()
                    return
                try:
                    payload = outer.post(urlparse(self.path).path, body)
                except LookupError:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return f"http://{host}:{self.server.server_port}"

    def close(self):
        if self.server:
            self.server.shutdown()


# ────────────────────────────────────────────────
# Renpho
# ────────────────────────────────────────────────

# Synthetic credentials (the "# ggignore" marks tell secret scanners so).
RENPHO_SECRETS = ("alice@example.com", "alice-password", "bob@example.com", "bob-password",  # ggignore
                  "renpho-token-", "1990-01-01", "Alice Liddell")


_record_ids = itertools.count(1)


def weigh_in(ts, user_id=100001, scale="ES-CS20M", **values):
    """A record as renpho-api returns it: Renpho's metric names, 0 for an
    unmeasured metric, ids and device details the connector must not keep.
    Row ids are unique: renpho-api drops a second record with the same."""
    record = {"id": next(_record_ids), "timeStamp": ts, "scaleName": scale, "bUserId": user_id, "userId": user_id,
              "mac": "AA:BB:CC:DD:EE:FF", "internalModel": "0106", "timeZone": "Europe/Paris",
              "height": 170.5, "gender": 1, "localCreatedAt": "2025-09-16 07:20:00",
              "weight": 0, "bmi": 0, "bodyfat": 0, "water": 0, "muscle": 0, "bone": 0, "bmr": 0, "visfat": 0,
              "subfat": 0, "protein": 0, "bodyage": 0, "sinew": 0, "fatFreeWeight": 0, "heartRate": 0,
              "cardiacIndex": 0, "bodyShape": 0}
    if scale is None:
        del record["scaleName"]
    record.update(values)
    return record


ALICE_RECORDS = [
    # Newest first, as the API: the newer scale reports no name.
    weigh_in(1_758_000_000, scale=None, weight=78.4, bmi=24.1, bodyfat=18.2, visfat=6, bmr=1617),
    # Milliseconds, as some tables have them.
    weigh_in(1_757_000_000_123, weight=79.0, bmi=24.5, bodyfat=19.0, water=55.5, visfat=7, bmr=1600),
    # Not a weigh-in: no time, or nothing measured.
    weigh_in(None, weight=80.0),
    weigh_in(1_756_000_000),
]

BOB_RECORDS = [weigh_in(1_758_100_000, user_id=100002, weight=65.2, bmi=21.0)]

# Boundaries for alice only (synthetic numbers).
THRESHOLDS = {"alice": {"weight_kg": [60.0, 80.0, 95.0], "bmi": [18.5, 25.0, 30.0],
                        "body_fat_pct": [6.0, 13.0, 17.0, 25.0], "visceral_fat": [6, 11, 15],
                        "bmr_kcal": [1617]}}


class FakeRenpho:
    """Renpho's cloud as renpho-api calls it: encrypted JSON bodies, a token
    after login, the scales' tables paged. `accounts` maps an email to
    (password, user id, records). `hang` makes every call wait (until
    close); `echo_email` puts the email in the refusal message."""

    def __init__(self, accounts=None):
        from renpho.crypto import aes_decrypt, encrypt_request

        self._decrypt, self._encrypt = aes_decrypt, encrypt_request
        self.accounts = accounts if accounts is not None else copy.deepcopy({
            "alice@example.com": ("alice-password", 100001, ALICE_RECORDS),
            "bob@example.com": ("bob-password", 100002, BOB_RECORDS),
        })
        self.tokens: Dict[str, str] = {}
        self.calls: List[str] = []
        self.hang = False
        self._release = threading.Event()
        self.echo_email = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                path = urlparse(self.path).path.lstrip("/")
                outer.calls.append(path)
                if outer.hang:
                    outer._release.wait(30)
                    return
                payload = outer.answer(path, body, self.headers)
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _ok(self, data):
        return {"code": 101, "msg": "success", "data": None if data is None else self._encrypt(data)["encryptData"]}

    def answer(self, path, body, headers):
        # device/count sends an empty body first (encrypted empty bytes).
        request = json.loads(self._decrypt(body["encryptData"]) or "{}") if body.get("encryptData") else {}
        if path == "renpho-aggregation/user/login":
            login = request.get("login") or {}
            account = self.accounts.get(login.get("email"))
            if account is None or account[0] != login.get("password"):
                message = f"Account {login.get('email')} or password error" if self.echo_email else "password error"
                return {"code": 50001, "msg": message, "data": None}
            token = f"renpho-token-{account[1]}"
            self.tokens[token] = login["email"]
            # The profile the login returns: never kept.
            return self._ok({"login": {"token": token, "id": account[1], "email": login["email"],
                                       "accountName": "Alice Liddell", "birthday": "1990-01-01", "height": 170}})
        email = self.tokens.get(headers.get("token"))
        if email is None:
            return {"code": 40001, "msg": "token invalid", "data": None}
        _password, user_id, records = self.accounts[email]
        if path == "renpho-aggregation/device/count":
            return self._ok({"scale": [{"tableName": f"measurements_info_{user_id % 24}", "count": len(records),
                                        "userIds": [user_id]}]})
        if path == "RenphoHealth/scale/queryBodyCompositionMeasureData":
            size, page = request["pageSize"], request["pageNum"]
            chunk = records[(page - 1) * size:page * size]
            return self._ok(chunk or None)
        return {"code": 404, "msg": f"unknown {path}", "data": None}

    def close(self):
        self._release.set()
        self.server.shutdown()



# ────────────────────────────────────────────────
# Todoist
# ────────────────────────────────────────────────

# Synthetic token and profile (the "# ggignore" marks tell secret scanners so).
TODOIST_TOKEN = "todoist-test-token"  # ggignore
TODOIST_SECRETS = (TODOIST_TOKEN, "user-api-token-of-dana", "dana@example.com", "Dana Example")  # ggignore

TODOIST_PROJECTS = [{"id": "p-inbox", "name": "Inbox"}, {"id": "p-home", "name": "Home"}]
TODOIST_ARCHIVED = [{"id": "p-old", "name": "Old project"}]
TODOIST_SECTIONS = [{"id": "s-errands", "name": "Errands", "project_id": "p-home"}]


def todoist_task(task_id, completed_at, project_id="p-inbox", **extra):
    """A completed task as /tasks/completed/by_completion_date returns it."""
    task = {"id": task_id, "content": f"Task {task_id}", "description": "a private note", "project_id": project_id,
            "section_id": None, "parent_id": None, "priority": 1, "labels": [], "due": None, "duration": None,
            "added_at": None, "completed_at": completed_at, "postponed_count": 0, "user_id": "u-dana"}
    task.update(extra)
    return task


TODOIST_TASKS = [
    todoist_task("t-milk", "2026-10-01T18:30:15.123456Z", "p-home", content="Buy milk, eggs", section_id="s-errands",
                 priority=4, labels=["shop", "errand"], added_at="2026-09-30T10:00:00.000000Z",
                 due={"date": "2026-10-01", "is_recurring": False}, duration={"amount": 30, "unit": "minute"},
                 postponed_count=1),
    # Archived project, sub-task, 5 days late, a duration in days.
    todoist_task("t-report", "2025-06-15T07:45:00Z", "p-old", content='Write "Q3" report', parent_id="t-parent",
                 added_at="2025-03-01T09:00:00Z", due={"date": "2025-06-10T17:00:00", "is_recurring": False},
                 duration={"amount": 2, "unit": "day"}),
    # A recurring task completed for good: no late_days.
    todoist_task("t-water", "2026-10-02T06:00:00Z", "p-home", priority=2,
                 due={"date": "2026-10-02", "is_recurring": True}),
    # A project deleted since, named by the activity log; one known nowhere.
    todoist_task("t-gone", "2026-02-01T12:00:00Z", "p-gone", priority=3, added_at="2026-02-01T13:00:00Z"),
    todoist_task("t-lost", "2026-02-02T12:00:00Z", "p-lost"),
    # Not completions: no time, no id.
    todoist_task("t-open", None),
    todoist_task("", "2026-02-03T12:00:00Z"),
]


def todoist_event(task_id, event_date, project_id="p-inbox", content=None, project_name=None):
    """An `item:completed` event of /activities, annotated."""
    extra = {"content": content or f"Task {task_id}", "client": "TodoistForAndroid"}
    if project_name:
        extra["parent_project_name"] = project_name
    return {"id": f"e-{task_id}-{event_date}", "object_type": "item", "object_id": task_id,
            "event_type": "completed", "event_date": event_date, "parent_project_id": project_id,
            "parent_item_id": None, "initiator_id": None, "extra_data": extra}


TODOIST_EVENTS = [
    todoist_event("t-milk", "2026-10-01T18:30:15.000000Z", "p-home", "Buy milk, eggs", "Home"),
    # A recurring task: one event per completion.
    todoist_event("t-plants", "2026-10-03T07:00:00Z", "p-home", "Water the plants", "Home"),
    todoist_event("t-plants", "2026-10-10T07:00:00Z", "p-home", "Water the plants", "Home"),
    todoist_event("t-gone", "2026-02-01T12:00:00Z", "p-gone", "Old chore", "Gone project"),
    todoist_event("t-lost", "2026-02-02T12:00:00Z", "p-lost"),
    # On a 90-day window boundary of a full run (joined_at + 90 days): the
    # API returns it with both windows.
    todoist_event("t-edge", "2025-04-10T08:00:00Z"),
    {"object_id": "t-undated", "event_date": None, "extra_data": {}},
]

TODOIST_STATS = {
    "karma": 1234.5, "completed_count": 42, "karma_trend": "up",
    "karma_graph_data": [{"date": "2026-10-04", "karma_avg": 1200}, {"date": "2026-10-05", "karma_avg": 1210.5},
                         {"date": "not a date", "karma_avg": 1}],
    "days_items": [{"date": "2026-10-05", "total_completed": 3}],
    "goals": {"daily_goal": 5, "weekly_goal": 25, "karma_disabled": 0,
              "current_daily_streak": {"count": 3, "start": "2026-10-03", "end": "2026-10-05"},
              "max_daily_streak": {"count": 12, "start": "2026-01-01", "end": "2026-01-12"},
              "current_weekly_streak": {"count": 2}, "max_weekly_streak": {"count": 9}},
}


def _todoist_time(raw):
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class FakeTodoist:
    """The Todoist API v1 as the connector calls it: a bearer token, lists
    paged by `page_size` (next_cursor), completed tasks and activity events
    filtered by their dates (bounds included, as the API: an item on a
    window's boundary comes with both windows), windows over 3 months
    refused. `fail[path] = [(code, headers), ...]` answers those first.
    Records every call as (path, params)."""

    def __init__(self, page_size=2):
        self.token = TODOIST_TOKEN
        self.page_size = page_size
        self.tasks = copy.deepcopy(TODOIST_TASKS)
        self.events = copy.deepcopy(TODOIST_EVENTS)
        self.stats = copy.deepcopy(TODOIST_STATS)
        self.calls: List[Tuple[str, Dict[str, str]]] = []
        self.user_agents: set = set()
        self.fail: Dict[str, List[Tuple[int, Dict[str, str]]]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                path = url.path.removeprefix("/api/v1")
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                outer.calls.append((path, params))
                outer.user_agents.add(self.headers.get("User-Agent"))
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"error": "Unauthorized"})
                if outer.fail.get(path):
                    code, headers = outer.fail[path].pop(0)
                    return self._send(code, {"error": "failed"}, headers)
                answer = outer.answer(path, params)
                if answer is None:
                    return self._send(400, {"error": f"bad request on {path}"})
                self._send(200, answer)

            def _send(self, code, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1"

    def _page(self, items, key, params):
        start = int(params.get("cursor") or 0)
        size = min(int(params.get("limit") or 50), self.page_size)
        end = start + size
        return {key: items[start:end], "next_cursor": str(end) if end < len(items) else None}

    def _between(self, items, field, lower, upper):
        lower, upper = _todoist_time(lower), _todoist_time(upper)
        if upper - lower > timedelta(days=92):
            return None
        return [i for i in items if i.get(field) and lower <= _todoist_time(i[field]) <= upper]

    def answer(self, path, params):
        if path == "/user":
            return {"id": "u-dana", "email": "dana@example.com", "full_name": "Dana Example",
                    "token": "user-api-token-of-dana", "joined_at": "2025-01-10T08:00:00.000000Z",
                    "tz_info": {"timezone": "Europe/Paris"}}
        if path == "/projects":
            return self._page(TODOIST_PROJECTS, "results", params)
        if path == "/projects/archived":
            return self._page(TODOIST_ARCHIVED, "results", params)
        if path == "/sections":
            return self._page(TODOIST_SECTIONS, "results", params)
        if path == "/tasks/completed/by_completion_date":
            items = self._between(self.tasks, "completed_at", params["since"], params["until"])
            return None if items is None else self._page(items, "items", params)
        if path == "/activities":
            if params.get("object_event_types") != '["item:completed"]':
                return None
            items = self._between(self.events, "event_date", params["date_from"], params["date_to"])
            return None if items is None else self._page(items, "results", params)
        if path == "/tasks/completed/stats":
            return self.stats
        return None

    def paths(self):
        return [path for path, _params in self.calls]

    def close(self):
        self.server.shutdown()


# ────────────────────────────────────────────────
# MyTherapy (its export file, built by hand)
# ────────────────────────────────────────────────

MYTHERAPY_COLUMNS = "actual_date,scheduled_date,type,name,value,unit,status,note"
# Invented aliases and names: the real mapping is the owner's (privacy),
# and the app's aliases are deliberately vague (see docs/connectors/mytherapy.md).
MYTHERAPY_NAMES = {"First": "Alpha med", "Second": "Beta med"}
MYTHERAPY_NOTE = "taken with breakfast"  # ggignore


def mytherapy_row(name="First", scheduled="2026-03-01 12:00:00", actual=None, value="2",
                  unit="pill(s)", status="confirmed", kind="drug", note=""):
    """One row of "MyTherapy Archive.csv": a scheduled dose, or a reminder."""
    fields = (actual if actual is not None else scheduled, scheduled, kind, name, value, unit, status, note)
    return ",".join(f'"{field}"' if "," in field else field for field in fields)


def write_mytherapy_export(path, rows, zipped=False):
    """An export: the CSV itself, or the zip the app saves it in."""
    text = "\n".join([MYTHERAPY_COLUMNS, *rows]) + "\n"
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("MyTherapy Archive.csv", text)
    else:
        Path(path).write_text(text, encoding="utf-8")
    return Path(path)


# ────────────────────────────────────────────────
# MyFitnessPal (its export zip, built by hand)
# ────────────────────────────────────────────────

# The export's columns, in the app's order (French here). The rows the tests
# build are invented: no real account, no real food or workout name.
MYFITNESSPAL_WEIGHT_COLUMNS = ["Date", "Body Fat %", "Poids"]
MYFITNESSPAL_FOOD_COLUMNS = [
    "Date", "Repas", "Calories", "Lipides (g)", "Acides gras saturés", "Acides gras polyinsaturés",
    "Acides gras monoinsaturés", "Acides gras trans", "Cholestérol", "Sodium (mg)", "Potassium",
    "Glucides (g)", "Fibres", "Sucres", "Protéines (g)", "Vitamine A", "Vitamine C", "Calcium", "Fer",
    "export_headers.note"]
MYFITNESSPAL_EXERCISE_COLUMNS = [
    "Date", "Exercices", "Type", "Calories liées aux exercices", "Minutes d'exercice", "Séries",
    "Répétitions par série", "Kilogrammes", "Pas", "export_headers.note"]
# A note a food entry might carry: it is not one of the columns the connector
# writes, so it must never reach InfluxDB (nor ever be in the code).
MYFITNESSPAL_NOTE = "a private note"  # ggignore


def mfp_weight_row(day="2026-03-01", weight="70.1", fat=""):
    """One row of the weigh-ins CSV (Date, Body Fat % and Poids matter)."""
    return {"Date": day, "Body Fat %": fat, "Poids": weight}


def mfp_food_row(day="2026-03-01", meal="Déjeuner", **nutrients):
    """One meal; a nutrient left out stays out (the export writes its 0)."""
    return {"Date": day, "Repas": meal, **nutrients}


def mfp_exercise_row(day="2026-03-01", activity="Course", kcal="300", minutes="30"):
    return {"Date": day, "Exercices": activity, "Calories liées aux exercices": kcal,
            "Minutes d'exercice": minutes}


def _mfp_csv(columns, rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def write_myfitnesspal_export(path, weight=(), food=(), exercise=(), span=None):
    """The export the app saves: a zip of the three CSVs. The connector
    recognises the files by their columns, so the test names them as nonsense
    on purpose -- unless `span` (first, last) is given: then as the app names
    them, after the date range the user picked
    ("Récapitulatif-des-mesures-du-2019-01-01-au-2024-12-31.csv")."""
    names = (["r1.csv", "r2.csv", "r3.csv"] if span is None else
             [f"Récapitulatif-des-{what}-du-{span[0]}-au-{span[1]}.csv"
              for what in ("mesures", "aliments-consommés", "exercices")])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(names[0], _mfp_csv(MYFITNESSPAL_WEIGHT_COLUMNS, weight))
        archive.writestr(names[1], _mfp_csv(MYFITNESSPAL_FOOD_COLUMNS, food))
        archive.writestr(names[2], _mfp_csv(MYFITNESSPAL_EXERCISE_COLUMNS, exercise))
    return Path(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A fake Audiobookshelf server with a few sessions.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13378)
    parser.add_argument("--token", default="demo-key")
    parser.add_argument("--without", default="", help="session ids to leave out, comma-separated "
                                                      "(as if deleted in Audiobookshelf)")
    args = parser.parse_args()
    gone = set(filter(None, args.without.split(",")))
    fake = FakeAbs(sessions=[s for s in SESSIONS if s["id"] not in gone], token=args.token)
    print(f"Fake Audiobookshelf on {fake.serve(args.host, args.port)} (API key: {args.token})", flush=True)
    threading.Event().wait()
