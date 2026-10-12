"""Test doubles: a fake InfluxDB 1.x and a fake Audiobookshelf server, both
real HTTP servers in a thread (the code under test speaks HTTP to them).

The Audiobookshelf one also runs on its own, to try Tributary end to end
without a real server:

    python -m tests.fakes --port 13378 --token demo-key

then TRIBUTARY_ABS_URL=http://<host>:13378 and TRIBUTARY_ABS_TOKEN=demo-key.
"""
import argparse
import copy
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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
        return result

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
