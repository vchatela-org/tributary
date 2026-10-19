"""Test doubles: a fake InfluxDB 1.x, a fake Audiobookshelf server, a fake
Renpho cloud, a fake Todoist API and a fake WHOOP (its OAuth and its API),
real HTTP servers in a thread (the code under test speaks HTTP to them), and
the exports of the file sources, built by hand. The WHOOP section holds
portions adapted from kryoseu/whoops (Apache License 2.0; modified): see
NOTICE.

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

def _epoch(rfc3339: str) -> int:
    return int(datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).timestamp())


class FakeInflux:
    """Keeps points by key (database, measurement, tags, time) as InfluxDB
    does -- writing the same key overwrites the fields it carries, and keeps
    the others --, records every /query
    statement and /write body; `fail[path] = (code, body)` makes a path
    answer an error, and `hold` seconds makes /query wait that long before
    answering (cut short when the fake closes)."""

    def __init__(self):
        self.queries: List[Tuple[Optional[str], str]] = []
        self.writes: List[Tuple[str, str, List[str]]] = []
        self.fail: Dict[str, Tuple[int, str]] = {}
        self.databases = {"_internal"}
        self.points: Dict[Tuple, Dict[str, str]] = {}
        self.hold = 0.0
        self._closing = threading.Event()
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
                    if outer.hold:
                        outer._closing.wait(outer.hold)
                    result = outer._query(params.get("db"), params["q"], params.get("epoch"))
                    try:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(json.dumps({"results": [result]}).encode())
                    except OSError:
                        pass  # the client stopped waiting (`hold`)
                    return
                outer.writes.append((params["db"], params["precision"], body.split("\n")))
                for line in body.split("\n"):
                    measurement, tags, fields, ts = parse(line)
                    # The same key again: InfluxDB keeps the union of the two
                    # field sets, the new values winning.
                    key = (params["db"], measurement, tuple(sorted(tags.items())), ts)
                    outer.points[key] = {**outer.points.get(key, {}), **fields}
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _query(self, database: Optional[str], statement: str, epoch: Optional[str] = None) -> Dict:
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
            literal = r"'((?:[^'\\]|\\.)*)'"  # an InfluxQL string, escapes included
            match = re.match(rf'DELETE FROM "(.+)" WHERE "source" = {literal}(?: AND "user" = {literal})? '
                             r"AND time >= '(.+)' AND time < '(.+)'$", statement)
            measurement, source, user, start, end = match.groups()
            start, end = (_epoch(t) for t in (start, end))
            for key in [k for k in self.points if k[0] == database and k[1] == measurement
                        and dict(k[2]).get("source") == source and (user is None or dict(k[2]).get("user") == user)
                        and start <= k[3] < end]:
                del self.points[key]
        elif statement.startswith(("SELECT last(", "SELECT * FROM")):
            result.update(self._newest(database, statement, epoch))
        else:
            result.update(self._select(database, statement))
        return result

    def _exists(self, database: Optional[str]) -> bool:
        return database in self.databases or any(k[0] == database for k in self.points)

    def _newest(self, database: Optional[str], statement: str, epoch: Optional[str]) -> Dict:
        """The reads of a compatible tool's freshness line: a measurement's
        newest point, as `last("<field>")` or as `*` newest first, limit 1.
        Answered whole, its values and tags included, as InfluxDB does: only
        the code under test may drop them. Times as epoch seconds with
        `epoch=s`, RFC3339 otherwise."""
        last = re.match(r'SELECT last\("(\w+)"\) FROM "(\w+)"$', statement)
        star = re.match(r'SELECT \* FROM "(\w+)" ORDER BY time DESC LIMIT 1$', statement)
        if not (last or star):
            raise AssertionError(f"the fake does not answer {statement!r}")
        if not self._exists(database):
            return {"error": f"database not found: {database}"}
        field, measurement = last.groups() if last else (None, star.group(1))
        rows = [(ts, dict(tags), fields) for (db, m, tags, ts), fields in self.points.items()
                if db == database and m == measurement and (field is None or field in fields)]
        if not rows:
            return {}
        ts, tags, fields = max(rows, key=lambda row: row[0])
        when = ts if epoch == "s" else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if last:
            columns, values = ["time", "last"], [when, _field_value(fields[field])]
        else:
            names = sorted(set(tags) | set(fields))
            columns = ["time", *names]
            values = [when, *(tags[n] if n in tags else _field_value(fields[n]) for n in names)]
        return {"series": [{"name": measurement, "columns": columns, "values": [values]}]}

    def _select(self, database: Optional[str], statement: str) -> Dict:
        """The one SELECT a connector makes today: a single field of a
        measurement, filtered on two tags and a time range (the MyFitnessPal
        weigh-in dedup). Answers one merged series, no tags, RFC3339 times --
        as InfluxDB does without `GROUP BY *` and without `epoch=s` -- or
        InfluxDB's error for a database that doesn't exist."""
        match = re.match(r'SELECT "(\w+)" FROM "(\w+)" WHERE "(\w+)" = \'([^\']*)\''
                         r' AND "(\w+)" != \'([^\']*)\' AND time >= \'(.+)\' AND time < \'(.+)\'$', statement)
        if match is None:
            return {}
        if not self._exists(database):
            return {"error": f"database not found: {database}"}
        field, measurement, t1, v1, t2, v2, start, end = match.groups()
        start, end = _epoch(start), _epoch(end)
        values = []
        for (db, m, tags, ts), fields in self.points.items():
            tags = dict(tags)
            if (db != database or m != measurement or tags.get(t1) != v1 or tags.get(t2) == v2
                    or not start <= ts < end):
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
        self._closing.set()
        self.server.shutdown()


def _field_value(raw: str):
    """A line-protocol field value as InfluxDB answers it in JSON."""
    if raw.startswith('"'):
        return _unescape(raw[1:-1])
    if raw in ("t", "T", "true", "True", "TRUE", "f", "F", "false", "False", "FALSE"):
        return raw.lower().startswith("t")
    return int(raw[:-1]) if raw.endswith(("i", "u")) else float(raw)


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
# GitHub (its REST and GraphQL APIs)
# ────────────────────────────────────────────────

# Synthetic tokens, accounts and repositories (the "# ggignore" marks tell
# secret scanners so): an organisation, acme, and the tokens' own account,
# dana, each with its own token.
GITHUB_TOKEN = "github-test-token"  # ggignore
GITHUB_TOKEN_2 = "github-test-token-2"  # ggignore
GITHUB_ME = "dana"
GITHUB_SECRETS = (GITHUB_TOKEN, GITHUB_TOKEN_2, "dana@example.com")  # ggignore

_DANA = {"email": "dana@example.com", "user": {"login": GITHUB_ME}}
_CLAUDE = {"email": "noreply@anthropic.com", "user": {"login": "claude"}}
_RENOVATE = {"email": "renovate@whitesourcesoftware.com", "user": {"login": "renovate-bot"}}


def github_commit(authored, author=_DANA, coauthors=(), additions=1, deletions=0, parents=1):
    return {"authoredDate": authored, "additions": additions, "deletions": deletions,
            "parents": {"totalCount": parents}, "author": author,
            "authors": {"nodes": [author, *coauthors]}}


def github_usage(day, repo, minutes, sku="Actions Linux", product="actions", unit="Minutes", price=0.006,
                 owner="acme"):
    gross = round(minutes * price, 6)
    return {"date": f"{day}T00:00:00Z", "product": product, "sku": sku, "quantity": minutes, "unitType": unit,
            "pricePerUnit": price, "grossAmount": gross, "discountAmount": gross, "netAmount": 0.0,
            "organizationName": owner, "repositoryName": repo}


GITHUB_REPOS = {
    "acme": [{"name": "api", "visibility": "private", "fork": False},
             {"name": "site", "visibility": "public", "fork": False},
             {"name": "upstream", "visibility": "public", "fork": True}],
    # The token's own account: /user/repos, `private` and no `visibility`.
    "dana": [{"name": "dotfiles", "private": True, "fork": False}],
}
GITHUB_COMMITS = {
    ("acme", "api"): [
        github_commit("2026-10-13T21:30:00Z", coauthors=(_CLAUDE,), additions=10, deletions=2),
        github_commit("2026-10-13T08:00:00Z", additions=3, deletions=1),
        # Renovate's pull request, opened as dana, squash-merged.
        github_commit("2026-10-12T07:00:00Z", coauthors=(_RENOVATE,)),
        github_commit("2026-10-12T06:00:00Z", author=_RENOVATE),
        github_commit("2026-10-12T05:00:00Z", author={"email": "41898282+github-actions[bot]@users.noreply.github.com",
                                                     "user": {"login": "github-actions[bot]"}}),
        github_commit("2026-10-11T12:00:00Z", parents=2),
        github_commit("2026-10-11T11:00:00Z", author={"email": "someone@elsewhere.example", "user": None}),
        github_commit("2026-10-10T09:00:00Z", author={"email": "198982749+Copilot@users.noreply.github.com",
                                                     "user": {"login": "Copilot"}}),
        github_commit("2025-01-01T09:00:00Z"),
    ],
    ("acme", "site"): [github_commit("2026-10-09T18:00:00Z", additions=5)],
    ("acme", "upstream"): [github_commit("2026-10-09T18:00:00Z")],
    # An empty repository: no default branch.
    ("dana", "dotfiles"): None,
}
def github_run(created, workflow_id, path=".github/workflows/tests.yml", conclusion="success", event="push",
               attempt=1, status="completed", started=None, updated=None):
    return {"id": _epoch(created) * 10 + attempt, "created_at": created,
            "run_started_at": started, "updated_at": updated, "status": status, "conclusion": conclusion,
            "run_attempt": attempt, "event": event, "workflow_id": workflow_id, "path": path}


GITHUB_WORKFLOWS = {
    ("acme", "api"): [{"id": 1, "name": "Tests"}, {"id": 2, "name": "Deploy"}, {"id": 3, "name": "Pages"}],
    ("acme", "site"): [{"id": 4, "name": "Pages"}],
    ("dana", "dotfiles"): [{"id": 5, "name": "Sync"}],
}
# Newest first, as GitHub lists them.
GITHUB_RUNS = {
    ("acme", "api"): [
        # A re-run: the same creation, a second attempt.
        github_run("2026-10-13T22:00:00Z", 1, attempt=2, started="2026-10-13T22:00:10Z",
                   updated="2026-10-13T22:04:10Z"),
        github_run("2026-10-13T20:00:00Z", 2, path=".github/workflows/deploy.yml", conclusion="timed_out",
                   event="workflow_dispatch", started="2026-10-13T20:00:30Z",
                   updated="2026-10-13T20:10:30Z"),
        # Still running: not written, read again next sync.
        github_run("2026-10-12T09:00:00Z", 3, status="in_progress", conclusion=None,
                   started="2026-10-12T09:00:05Z", updated="2026-10-12T09:01:00Z"),
        # A workflow the list no longer has: its file's name.
        github_run("2026-09-20T08:00:00Z", 99, path=".github/workflows/old.yml", event="schedule",
                   started="2026-09-20T08:00:05Z", updated="2026-09-20T08:01:05Z"),
        # Before the first sync's window: never read.
        github_run("2025-01-01T08:00:00Z", 1, started="2025-01-01T08:00:05Z",
                   updated="2025-01-01T08:02:05Z"),
    ],
    ("acme", "site"): [
        github_run("2026-10-10T18:00:00Z", 4, path=".github/workflows/pages.yml", event="workflow_dispatch",
                   started="2026-10-10T18:00:02Z", updated="2026-10-10T18:00:32Z"),
    ],
    ("dana", "dotfiles"): [
        github_run("2026-10-14T06:00:00Z", 5, started="2026-10-14T06:00:01Z",
                   updated="2026-10-14T06:00:41Z"),
    ],
}
def github_job(runner="", group="", group_id=1, conclusion="success"):
    return {"runner_name": runner, "runner_group_name": group, "runner_group_id": group_id,
            "conclusion": conclusion, "status": "completed"}


# A run's jobs, by its id: GitHub's own runners say so by their group, ARC's
# by their runner's name.
GITHUB_JOBS = {
    GITHUB_RUNS[("acme", "api")][0]["id"]: [github_job("arc-runner-1", "arc-runners")],
    GITHUB_RUNS[("acme", "api")][1]["id"]: [github_job("GitHub Actions 2", "GitHub Actions", 0)],
    GITHUB_RUNS[("acme", "api")][3]["id"]: [github_job("arc-runner-1", "arc-runners")],
    GITHUB_RUNS[("acme", "site")][0]["id"]: [github_job("GitHub Actions 1", "GitHub Actions", 0)],
    GITHUB_RUNS[("dana", "dotfiles")][0]["id"]: [github_job("GitHub Actions 3", "GitHub Actions", 0)],
}
# The billing report, by (owner, year, month).
GITHUB_USAGE = {
    ("acme", 2026, 10): [
        github_usage("2026-10-13", "api", 12.0),
        github_usage("2026-10-13", "api", 3.0, sku="Actions Windows", price=0.01),
        github_usage("2026-10-13", "site", 30.0),
        # A larger runner: billed, never covered by the included minutes.
        github_usage("2026-10-13", "api", 5.0, sku="Actions Linux 4-core", price=0.012),
        # A repository deleted since: counted as private.
        github_usage("2026-10-02", "gone", 2.0),
        github_usage("2026-10-13", "api", 0.5, product="actions", unit="GigabyteHours", sku="Actions storage"),
        github_usage("2026-10-13", None, 1.0, product="copilot", unit="UserMonths", sku="Copilot Business"),
    ],
    ("acme", 2026, 9): [github_usage("2026-09-30", "api", 40.0)],
    ("acme", 2025, 11): [github_usage("2025-11-03", "api", 7.0)],
    ("dana", 2026, 10): [github_usage("2026-10-14", "dotfiles", 4.0, owner=None)],
}


class FakeGitHub:
    """GitHub's REST and GraphQL APIs as the connector calls them: a bearer
    token per owner (`tokens`, token -> owner), lists paged by `page_size`
    (page / per_page), commits filtered by `since` (on the author's time here)
    and paged by `after`, billing reports by year and month. `fail[path] =
    [(code, headers), ...]` answers those first. Records every call as
    (method, path, params, owner of the token)."""

    def __init__(self, page_size=2):
        self.tokens = {GITHUB_TOKEN: "acme", GITHUB_TOKEN_2: "dana"}
        self.page_size = page_size
        self.calls: List[Tuple[str, str, Dict, str]] = []
        self.user_agents: set = set()
        self.fail: Dict[str, List[Tuple[int, Dict[str, str]]]] = {}
        # X-RateLimit-Remaining on every answer, when set.
        self.remaining: Optional[str] = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                if method == "POST":
                    params = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                scope = outer.tokens.get(bearer)
                outer.calls.append((method, url.path, params, scope))
                outer.user_agents.add(self.headers.get("User-Agent"))
                if scope is None:
                    return self._send(401, {"message": "Bad credentials"})
                if outer.fail.get(url.path):
                    code, headers = outer.fail[url.path].pop(0)
                    return self._send(code, {"message": "failed"}, headers)
                answer = outer.graphql(params) if method == "POST" else outer.answer(url.path, params, scope)
                if answer is None:
                    return self._send(404, {"message": "Not Found"})
                self._send(200, answer)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def _send(self, code, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                if outer.remaining is not None:
                    self.send_header("X-RateLimit-Remaining", outer.remaining)
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

    def _page(self, items, params):
        size = min(int(params.get("per_page") or 30), self.page_size)
        start = (int(params.get("page") or 1) - 1) * size
        return items[start:start + size]

    def answer(self, path, params, scope):
        parts = path.strip("/").split("/")
        if parts[0] == "users" and len(parts) == 2:
            return {"login": parts[1], "type": "Organization" if parts[1] == "acme" else "User"}
        if parts[:1] == ["orgs"] and parts[2:] == ["repos"] and parts[1] == scope:
            return self._page(GITHUB_REPOS.get(parts[1], []), params)
        if parts == ["user", "repos"] and scope == GITHUB_ME:
            return self._page(GITHUB_REPOS[GITHUB_ME], params)
        if parts[0] == "users" and parts[2:] == ["repos"]:
            return self._page([r for r in GITHUB_REPOS.get(parts[1], []) if r.get("visibility") == "public"], params)
        if parts[0] in ("organizations", "users") and parts[2:] == ["settings", "billing", "usage"] \
                and parts[1] == scope:
            return {"usageItems": GITHUB_USAGE.get((parts[1], int(params["year"]), int(params["month"])), [])}
        if parts[0] == "repos" and parts[1] == scope and parts[3:] == ["actions", "workflows"]:
            return {"total_count": len(GITHUB_WORKFLOWS.get((parts[1], parts[2]), [])),
                    "workflows": self._page(GITHUB_WORKFLOWS.get((parts[1], parts[2]), []), params)}
        if parts[0] == "repos" and parts[1] == scope and parts[3:] == ["actions", "runs"]:
            runs = GITHUB_RUNS.get((parts[1], parts[2]), [])
            if params.get("created"):
                start, _, end = params["created"].partition("..")
                runs = [run for run in runs
                        if (not start or _epoch(run["created_at"]) >= _epoch(start))
                        and (not end or _epoch(run["created_at"]) <= _epoch(end))]
            return {"total_count": len(runs), "workflow_runs": self._page(runs, params)}
        if parts[0] == "repos" and parts[1] == scope and parts[3:5] == ["actions", "runs"] \
                and parts[6:] == ["jobs"]:
            jobs = GITHUB_JOBS.get(int(parts[5]), [])
            return {"total_count": len(jobs), "jobs": self._page(jobs, params)}
        return None

    def graphql(self, body):
        if "viewer" in body["query"]:
            return {"data": {"viewer": {"login": GITHUB_ME}}}
        variables = body["variables"]
        commits = GITHUB_COMMITS.get((variables["owner"], variables["name"]), [])
        if commits is None:
            return {"data": {"repository": {"defaultBranchRef": None}}}
        if variables.get("since"):
            commits = [c for c in commits if _epoch(c["authoredDate"]) >= _epoch(variables["since"])]
        start = int(variables.get("after") or 0)
        end = start + self.page_size
        return {"data": {"repository": {"defaultBranchRef": {"target": {"history": {
            "pageInfo": {"hasNextPage": end < len(commits), "endCursor": str(end)},
            "nodes": commits[start:end]}}}}}}

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


# ────────────────────────────────────────────────
# Medisafe (its doses export, built by hand)
# ────────────────────────────────────────────────

# The export's columns, in the app's order. Names, notes and the account are
# invented.
MEDISAFE_COLUMNS = ["group_id", "name", "condition", "Scheduled for", "Recorded on", "dose_status", "dosage",
                    "unit", "med_strength", "notes"]
MEDISAFE_ACCOUNT = "00012345"
MEDISAFE_NOTE = "a private dose note"  # ggignore


def medisafe_row(name="Alpha med", scheduled="2026-01-15 11:00:00", recorded="2026-01-15 11:05:00",
                 status="taken", dosage="1", unit="pills", strength="", notes=""):
    """One dose of the export; times are UTC, as the app writes them."""
    return {"group_id": "g1", "name": name, "condition": "", "Scheduled for": scheduled, "Recorded on": recorded,
            "dose_status": status, "dosage": dosage, "unit": unit, "med_strength": strength, "notes": notes}


def write_medisafe_export(path, rows, zipped=False):
    """Doses_<account>_000.csv, or a zip holding it."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=MEDISAFE_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    member = f"Doses_{MEDISAFE_ACCOUNT}_000.csv"
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, out.getvalue())
    else:
        Path(path).write_text(out.getvalue(), encoding="utf-8")
    return Path(path)


# ────────────────────────────────────────────────
# WHOOP (its API v2 and OAuth endpoints, and its data export)
#
# Portions adapted from kryoseu/whoops (https://github.com/kryoseu/whoops),
# Apache License 2.0; modified: the export's CSV headers, from its
# tests/conftest.py. See NOTICE and LICENSES/Apache-2.0.txt.
# ────────────────────────────────────────────────

# Records written by hand from WHOOP's public API reference samples
# (developer.whoop.com/api), with readable ids and 2026 dates: nobody's data.
# The app's credentials and the tokens are invented (the "# ggignore" marks
# tell secret scanners so).
WHOOP_CLIENT_ID = "test-app-id"
WHOOP_CLIENT_SECRET = "test-app-secret"  # ggignore
WHOOP_REDIRECT = "https://tributary.example/connectors/whoop/callback"
# What must never leave: the app's secret, every token the fake issues
# ("access-N", "refresh-N" with a fixed prefix), a profile's email, a note.
WHOOP_TOKEN_PREFIX = "wt0ken"
WHOOP_EMAIL = "member@example.com"
WHOOP_NOTE = "a private journal note"  # ggignore

WHOOP_SLEEPS = [
    {"id": "sleep-night-1", "cycle_id": 1001, "user_id": 10129, "created_at": "2026-10-02T05:30:00.000Z",
     "updated_at": "2026-10-02T06:00:00.000Z", "start": "2026-10-01T21:10:00.500Z",
     "end": "2026-10-02T05:25:44.774Z", "timezone_offset": "+02:00", "nap": False, "score_state": "SCORED",
     "score": {"stage_summary": {"total_in_bed_time_milli": 30272735, "total_awake_time_milli": 1403507,
                                 "total_no_data_time_milli": 0, "total_light_sleep_time_milli": 14905851,
                                 "total_slow_wave_sleep_time_milli": 6630370,
                                 "total_rem_sleep_time_milli": 5879573, "sleep_cycle_count": 3,
                                 "disturbance_count": 12},
               "sleep_needed": {"baseline_milli": 27395716, "need_from_sleep_debt_milli": 352230,
                                "need_from_recent_strain_milli": 208595, "need_from_recent_nap_milli": -12312},
               "respiratory_rate": 16.11328125, "sleep_performance_percentage": 98,
               "sleep_consistency_percentage": 90, "sleep_efficiency_percentage": 91.69533848}},
    {"id": "sleep-nap-1", "cycle_id": 1001, "user_id": 10129, "created_at": "2026-10-02T13:05:00.000Z",
     "updated_at": "2026-10-02T13:05:00.000Z", "start": "2026-10-02T12:30:00.000Z",
     "end": "2026-10-02T13:00:30.000Z", "timezone_offset": "+02:00", "nap": True, "score_state": "SCORED",
     "score": {"stage_summary": {"total_in_bed_time_milli": 1830000, "total_awake_time_milli": 230000,
                                 "total_no_data_time_milli": 0, "total_light_sleep_time_milli": 1200000,
                                 "total_slow_wave_sleep_time_milli": 400000, "total_rem_sleep_time_milli": 0,
                                 "sleep_cycle_count": 0, "disturbance_count": 1},
               "sleep_needed": {"baseline_milli": 0, "need_from_sleep_debt_milli": 0,
                                "need_from_recent_strain_milli": 0, "need_from_recent_nap_milli": 0},
               "respiratory_rate": 15.5, "sleep_performance_percentage": 30,
               "sleep_efficiency_percentage": 87.4}},
    # Tonight's: not scored yet.
    {"id": "sleep-night-2", "cycle_id": 1002, "user_id": 10129, "created_at": "2026-10-03T05:41:00.000Z",
     "updated_at": "2026-10-03T05:41:00.000Z", "start": "2026-10-02T21:30:00.250Z",
     "end": "2026-10-03T05:40:00.000Z", "timezone_offset": "+02:00", "nap": False,
     "score_state": "PENDING_SCORE"},
    # A night the strap could not score: its times only.
    {"id": "sleep-unscorable", "cycle_id": 999, "user_id": 10129, "created_at": "2026-09-29T04:05:00.000Z",
     "updated_at": "2026-09-29T04:05:00.000Z", "start": "2026-09-28T22:00:00.000Z",
     "end": "2026-09-29T04:00:00.000Z", "timezone_offset": "+02:00", "nap": False,
     "score_state": "UNSCORABLE"},
]

WHOOP_RECOVERIES = [
    {"cycle_id": 1001, "sleep_id": "sleep-night-1", "user_id": 10129, "created_at": "2026-10-02T05:35:00.000Z",
     "updated_at": "2026-10-02T05:35:00.000Z", "score_state": "SCORED",
     "score": {"user_calibrating": False, "recovery_score": 44, "resting_heart_rate": 64,
               "hrv_rmssd_milli": 31.813562, "spo2_percentage": 95.6875, "skin_temp_celsius": 33.7},
     "_start": "2026-10-01T21:10:00.500Z"},
    {"cycle_id": 1002, "sleep_id": "sleep-night-2", "user_id": 10129, "created_at": "2026-10-03T05:45:00.000Z",
     "updated_at": "2026-10-03T05:45:00.000Z", "score_state": "PENDING_SCORE", "_start": "2026-10-02T21:30:00.250Z"},
]

WHOOP_CYCLES = [
    {"id": 1001, "user_id": 10129, "created_at": "2026-10-02T05:30:00.000Z", "updated_at": "2026-10-02T22:00:00.000Z",
     "start": "2026-10-01T21:10:00.500Z", "end": "2026-10-02T21:30:00.250Z", "timezone_offset": "+02:00",
     "score_state": "SCORED", "step_count": 8234,
     "score": {"strain": 12.4, "kilojoule": 8288.297, "average_heart_rate": 68, "max_heart_rate": 141}},
    # Today's, still open: no end, no duration; written, and read again later.
    {"id": 1002, "user_id": 10129, "created_at": "2026-10-03T05:41:00.000Z", "updated_at": "2026-10-03T08:00:00.000Z",
     "start": "2026-10-02T21:30:00.250Z", "timezone_offset": "+02:00", "score_state": "SCORED",
     "step_count": 3021,
     "score": {"strain": 5.2951527, "kilojoule": 4184.0, "average_heart_rate": 61, "max_heart_rate": 120}},
    # Scored later: nothing yet.
    {"id": 999, "user_id": 10129, "created_at": "2026-09-29T04:05:00.000Z", "updated_at": "2026-09-29T04:05:00.000Z",
     "start": "2026-09-28T22:00:00.000Z", "end": "2026-09-29T21:00:00.000Z", "timezone_offset": "+02:00",
     "score_state": "PENDING_SCORE"},
]

WHOOP_WORKOUTS = [
    {"id": "workout-run-1", "user_id": 10129, "created_at": "2026-10-02T17:00:00.000Z",
     "updated_at": "2026-10-02T17:00:00.000Z", "start": "2026-10-02T16:00:00.900Z",
     "end": "2026-10-02T16:45:30.000Z", "timezone_offset": "+02:00", "sport_name": "running", "sport_id": 0,
     "score_state": "SCORED",
     "score": {"strain": 8.2463, "average_heart_rate": 123, "max_heart_rate": 146, "kilojoule": 1569.34033203125,
               "percent_recorded": 100, "distance_meter": 1772.77035916, "altitude_gain_meter": 46.64384460449,
               "altitude_change_meter": -0.781372010707855,
               "zone_durations": {"zone_zero_milli": 300000, "zone_one_milli": 600000, "zone_two_milli": 900000,
                                  "zone_three_milli": 900000, "zone_four_milli": 600000,
                                  "zone_five_milli": 300000}}},
    # Not scorable: the sport and duration only.
    {"id": "workout-yoga-1", "user_id": 10129, "created_at": "2026-10-01T08:00:00.000Z",
     "updated_at": "2026-10-01T08:00:00.000Z", "start": "2026-10-01T07:00:00.000Z",
     "end": "2026-10-01T07:30:00.000Z", "timezone_offset": "+02:00", "sport_name": "yoga", "sport_id": 44,
     "score_state": "UNSCORABLE"},
]

WHOOP_RESOURCES = {"cycle": "cycle", "recovery": "recovery", "activity/sleep": "sleep",
                   "activity/workout": "workout"}


class FakeWhoop:
    """WHOOP's OAuth token endpoint and its API v2, as the connector calls
    them: client id and secret in the token POST's body; authorization codes
    single use; refresh tokens that **rotate** (a refresh kills the refresh
    token it used and every access token issued before); collections paged by
    `page_size` (nextToken), newest first, filtered by `start` (inclusive);
    401 for an unknown or dead access token. `fail[path] = [(code, headers,
    body), ...]` answers those first. Records every request in `events`
    ("token:<grant>", "GET <path>", "DELETE <path>"), and calls `on_data` (if
    set) before answering the first data request."""

    def __init__(self, page_size: int = 25):
        self.page_size = page_size
        self.records = {"cycle": copy.deepcopy(WHOOP_CYCLES), "recovery": copy.deepcopy(WHOOP_RECOVERIES),
                        "sleep": copy.deepcopy(WHOOP_SLEEPS), "workout": copy.deepcopy(WHOOP_WORKOUTS)}
        self.codes: Dict[str, str] = {}             # code -> the redirect_uri it was issued for
        self.refresh_tokens: set = set()
        self.access_tokens: set = set()
        self.issued: List[str] = []                 # every token handed out, for the privacy tests
        self.revoked = False
        self.events: List[str] = []
        self.params: List[Tuple[str, Dict[str, str]]] = []
        self.user_agents: set = set()
        self.fail: Dict[str, List[Tuple[int, Dict[str, str], Dict]]] = {}
        self.on_data = None
        self.expires_in = 3600
        self._serial = itertools.count(1)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                form = {k: v[0] for k, v in parse_qs(body).items()}
                path = urlparse(self.path).path
                outer.events.append(f"token:{form.get('grant_type')}")
                if outer.fail.get(path):
                    code, headers, payload = outer.fail[path].pop(0)
                    return self._send(code, payload, headers)
                if path != "/oauth/oauth2/token":
                    return self._send(404, {"error": "not_found"})
                code, payload = outer.token(form)
                self._send(code, payload)

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path
                outer.events.append(f"GET {path}")
                outer.params.append((path, params))
                outer.user_agents.add(self.headers.get("User-Agent"))
                if outer.on_data is not None:
                    hook, outer.on_data = outer.on_data, None
                    hook()
                if not outer._authorized(self.headers):
                    return self._send(401, {"error": "unauthorized"})
                if outer.fail.get(path):
                    code, headers, payload = outer.fail[path].pop(0)
                    return self._send(code, payload, headers)
                resource = WHOOP_RESOURCES.get(path.removeprefix("/developer/v2/"))
                if resource is None:
                    return self._send(404, {"error": "not_found"})
                self._send(200, outer.page(resource, params))

            def do_DELETE(self):  # noqa: N802
                path = urlparse(self.path).path
                outer.events.append(f"DELETE {path}")
                if not outer._authorized(self.headers):
                    return self._send(401, {"error": "unauthorized"})
                outer.revoked = True
                outer.access_tokens.clear()
                outer.refresh_tokens.clear()
                self.send_response(204)
                self.end_headers()

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
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def _new(self, kind: str) -> str:
        token = f"{WHOOP_TOKEN_PREFIX}-{kind}-{next(self._serial)}"
        self.issued.append(token)
        return token

    def grant(self) -> Dict:
        """A fresh pair, as the token endpoint answers it."""
        access, refresh = self._new("access"), self._new("refresh")
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {"access_token": access, "refresh_token": refresh, "expires_in": self.expires_in,
                "scope": "offline read:recovery read:cycles read:sleep read:workout", "token_type": "bearer"}

    def new_code(self, redirect_uri: str = WHOOP_REDIRECT) -> str:
        code = f"code-{next(self._serial)}"
        self.codes[code] = redirect_uri
        return code

    def token(self, form: Dict[str, str]) -> Tuple[int, Dict]:
        if form.get("client_id") != WHOOP_CLIENT_ID or form.get("client_secret") != WHOOP_CLIENT_SECRET:
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") == "authorization_code":
            if self.codes.pop(form.get("code"), None) != form.get("redirect_uri"):
                return 400, {"error": "invalid_grant", "error_description": "The authorization code is invalid"}
            return 200, self.grant()
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens or form.get("scope") != "offline":
                return 400, {"error": "invalid_grant", "error_description": "The refresh token is invalid"}
            # Rotation: the old refresh token and the access tokens die.
            self.refresh_tokens.discard(form["refresh_token"])
            self.access_tokens.clear()
            return 200, self.grant()
        return 400, {"error": "unsupported_grant_type"}

    def _authorized(self, headers) -> bool:
        auth = headers.get("Authorization") or ""
        return auth.startswith("Bearer ") and auth[len("Bearer "):] in self.access_tokens

    def page(self, resource: str, params: Dict[str, str]) -> Dict:
        records = self.records[resource]
        if params.get("start"):
            lower = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
            records = [r for r in records
                       if datetime.fromisoformat((r.get("start") or r.get("_start")).replace("Z", "+00:00")) >= lower]
        records = sorted(records, key=lambda r: r.get("start") or r.get("_start"), reverse=True)
        offset = int(params.get("nextToken") or 0)
        size = min(int(params.get("limit") or 10), 25, self.page_size)
        page = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records[offset:offset + size]]
        more = offset + size < len(records)
        return {"records": page, "next_token": str(offset + size) if more else None}

    def data_paths(self) -> List[str]:
        return [e.split(" ", 1)[1] for e in self.events if e.startswith("GET ")]

    def close(self):
        self.server.shutdown()


WHOOP_CYCLES_HEADER = ("Cycle start time,Cycle end time,Cycle timezone,Recovery score %,Resting heart rate (bpm),"
                       "Heart rate variability (ms),Skin temp (celsius),Blood oxygen %,Day Strain,"
                       "Energy burned (cal),Max HR (bpm),Average HR (bpm),Sleep onset,Wake onset,"
                       "Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),"
                       "Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),"
                       "Awake duration (min),Sleep need (min),Sleep debt (min),Sleep efficiency %,"
                       "Sleep consistency %")
WHOOP_SLEEPS_HEADER = ("Cycle start time,Cycle end time,Cycle timezone,Sleep onset,Wake onset,Sleep performance %,"
                       "Respiratory rate (rpm),Asleep duration (min),In bed duration (min),"
                       "Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),"
                       "Awake duration (min),Sleep need (min),Sleep debt (min),Sleep efficiency %,"
                       "Sleep consistency %,Nap")
WHOOP_WORKOUTS_HEADER = ("Cycle start time,Cycle end time,Cycle timezone,Workout start time,Workout end time,"
                         "Duration (min),Activity name,Activity Strain,Energy burned (cal),Max HR (bpm),"
                         "Average HR (bpm),HR Zone 1 %,HR Zone 2 %,HR Zone 3 %,HR Zone 4 %,HR Zone 5 %,"
                         "GPS enabled")
WHOOP_JOURNAL_HEADER = "Cycle start time,Cycle end time,Cycle timezone,Question text,Answered yes,Notes"

# The export's headers, as Whoops' tests copied them from a real export; the
# rows are made up. Times are local, to the second, in the row's time zone.
# The first night and the run match the API records above (within a second);
# the night of 2026-03-10 is from before the API was read: written whole.
WHOOP_EXPORT = {
    "physiological_cycles.csv": WHOOP_CYCLES_HEADER + "\n"
    "2026-10-01 23:10:00,2026-10-02 23:30:00,UTC+02:00,44,64,31.8,33.7,95.7,12.4,1981,141,68,"
    "2026-10-01 23:10:00,2026-10-02 07:25:44,98,16.1,457,505,248,111,98,23,465,6,92,90\n"
    "2026-03-09 23:00:00,2026-03-10 22:45:00,UTC+01:00,61,50,48,34.5,97.5,9.8,2100,150,66,"
    "2026-03-09 23:00:00,2026-03-10 06:45:00,85,14.2,420,465,220,110,90,45,480,20,90,81\n",
    "sleeps.csv": WHOOP_SLEEPS_HEADER + "\n"
    "2026-10-01 23:10:00,2026-10-02 23:30:00,UTC+02:00,2026-10-01 23:10:01,2026-10-02 07:25:44,98,16.1,457,505,"
    "248,111,98,23,465,6,92,90,false\n"
    "2026-03-09 23:00:00,2026-03-10 22:45:00,UTC+01:00,2026-03-09 23:00:00,2026-03-10 06:45:00,85,14.2,420,465,"
    "220,110,90,45,480,20,90,81,false\n",
    "workouts.csv": WHOOP_WORKOUTS_HEADER + "\n"
    "2026-10-01 23:10:00,2026-10-02 23:30:00,UTC+02:00,2026-10-02 18:00:01,2026-10-02 18:45:30,45.5,Running,"
    "8.2,375,146,123,20,30,30,15,5,true\n"
    "2026-03-09 23:00:00,2026-03-10 22:45:00,UTC+01:00,2026-03-10 18:00:00,2026-03-10 18:40:00,40,Cycling,"
    "10.1,450,160,135,10,40,30,15,5,false\n",
    "journal_entries.csv": WHOOP_JOURNAL_HEADER + "\n"
    "2026-10-01 23:10:00,2026-10-02 23:30:00,UTC+02:00,Have any alcoholic drinks?,true,\"" + WHOOP_NOTE + "\"\n"
    "2026-10-01 23:10:00,2026-10-02 23:30:00,UTC+02:00,Read in bed before sleeping?,false,\n"
    "2026-03-09 23:00:00,2026-03-10 22:45:00,UTC+01:00,Have any alcoholic drinks?,false,\n"
    "2026-03-09 23:00:00,2026-03-10 22:45:00,UTC+01:00,Have any alcoholic drinks?,false,\n",
}


def write_whoop_export(path, files=None, zipped=True):
    """The export zip (or, `zipped=False`, the one CSV given) at `path`."""
    files = WHOOP_EXPORT if files is None else files
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                archive.writestr(f"my_whoop_data_2026_10_03/{name}", content)
    else:
        (content,) = files.values()
        Path(path).write_text(content, encoding="utf-8")
    return Path(path)


# ────────────────────────────────────────────────
# Sleep as Android (its backup export, built by hand)
# ────────────────────────────────────────────────

# The export's base columns (the ones the import reads), then one "Event"
# column per event of the record; the actigraphy columns in between are named
# with that night's clock times and ignored.
SLEEP_COLUMNS = ("Id", "Tz", "From", "To", "Sched", "Hours", "Rating", "Comment",
                 "Framerate", "Snore", "Noise", "Cycles", "DeepSleep", "LenAdjust", "Geo")

# An invented night: 2026-03-01 23:00 Paris (22:00 UTC), eight hours to 07:00.
SLEEP_START_MS = epoch(2026, 3, 1, 22, 0) * 1000


def sleep_record(record_id=SLEEP_START_MS, tz="Europe/Paris", hours="8.0", rating="0",
                 comment="", snore="-1", noise="-1", cycles="3", deepsleep="0.2",
                 lenadjust="0", events=()):
    """One record's (header, data) lines: the base columns, then its events
    (`KIND-<epoch ms>[-<value>]`, e.g. "DEEP_START-...", "HR-...-58"). The
    writer adds a noise line after it, as the app does."""
    header = list(SLEEP_COLUMNS) + ["Event"] * len(events)
    data = [str(record_id), tz, "From", "To", "Sched", hours, rating, comment, "1",
            snore, noise, cycles, deepsleep, lenadjust, ""] + list(events)
    return header, data


def write_sleep_export(path, records, zipped=True):
    """A Sleep as Android backup: sleep-export.csv in a zip, or the CSV alone
    (`zipped=False`). Each record is a header line, its data line, and a noise
    line (an empty first cell) the connector skips."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    for header, data in records:
        writer.writerow(header)
        writer.writerow(data)
        writer.writerow([""] * len(data))
    text = out.getvalue()
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("sleep-export.csv", text)
    else:
        Path(path).write_text(text, encoding="utf-8")
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
