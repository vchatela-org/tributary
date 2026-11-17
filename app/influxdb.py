"""InfluxDB 1.x over its HTTP API: stdlib urllib, no client library (a few
POSTs don't justify a dependency in an image whose vulnerability scan
gates every release).

Writes are append-only and idempotent: a point is keyed by measurement +
tags + second, so writing it again overwrites it with the same values, and
nothing is dropped unless a connector opts into rebuilding its own source
(drop_source). A session a source later forgets stays in InfluxDB.

Databases are created when missing (TRIBUTARY_INFLUXDB_CREATE_DATABASES),
with year-long shards; an existing database is never altered.
"""
import http.client
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from app.config import settings

BATCH_LINES = 5000
TIMEOUT_SECONDS = 60
SHARD_DURATION = "52w"
# A 400 whose body says some points of the batch were dropped and the others
# written: older than the database's retention, or a field type conflict.
_PARTIAL_WRITE = re.compile(r"partial write: (?P<reason>.*?)\s*dropped=(?P<dropped>\d+)")


class InfluxError(Exception):
    """A request InfluxDB refused or never answered, with a message meant
    for the UI."""


def is_configured() -> bool:
    return bool(settings.influxdb_url)


def _post(path: str, params: Dict[str, str], body: bytes = b"", timeout: float = TIMEOUT_SECONDS) -> bytes:
    url = settings.influxdb_url.rstrip("/") + path + "?" + urlencode(params)
    if urlparse(url).scheme not in ("http", "https"):
        raise InfluxError(f"Invalid TRIBUTARY_INFLUXDB_URL ({settings.influxdb_url}): "
                          "it must start with http:// or https://.")
    request = Request(url, data=body, method="POST", headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        try:
            detail = json.loads(detail).get("error") or detail
        except (ValueError, AttributeError):
            pass
        raise InfluxError(f"InfluxDB refused {path} ({exc.code}): {str(detail)[:500]}") from exc
    except URLError as exc:
        raise InfluxError(f"InfluxDB unreachable ({settings.influxdb_url}): {exc.reason}") from exc
    except TimeoutError as exc:
        raise InfluxError(f"InfluxDB did not answer ({settings.influxdb_url})") from exc
    except (http.client.HTTPException, OSError) as exc:
        raise InfluxError(f"Connection to InfluxDB lost on {path}: {type(exc).__name__} {exc}".rstrip()) from exc


def query(statement: str, database: Optional[str] = None, timeout: float = TIMEOUT_SECONDS,
          epoch: Optional[str] = None) -> Dict:
    """One InfluxQL statement. /query answers 200 even when the statement
    fails: the error is in the body. Connectors may read through this too
    (e.g. to skip what another source already measured). `timeout` is in
    seconds; `epoch` ("s", "ms"...) answers times as numbers in that unit
    rather than RFC3339 strings."""
    params = {"q": statement}
    if database:
        params["db"] = database
    if epoch:
        params["epoch"] = epoch
    payload = json.loads(_post("/query", params, timeout=timeout) or b"{}")
    for result in payload.get("results") or []:
        if result.get("error"):
            raise InfluxError(f"InfluxDB: {result['error']} ({statement})")
    return payload


def existing_databases() -> List[str]:
    """The databases InfluxDB holds. A read of one it doesn't have yet is an
    error, not an empty answer: a caller looking for what is already there
    skips it."""
    payload = query("SHOW DATABASES")
    return [row[0] for result in payload.get("results") or [] for series in result.get("series") or []
            for row in series.get("values") or []]


def create_missing_databases(names: List[str]) -> List[str]:
    """Create the databases that don't exist yet, with year-long shards:
    InfluxDB's default for a database kept forever is one shard per week,
    and a few years of history made 600+ mostly tiny shards, each costing
    memory and file handles for good. An existing database is left exactly
    as it is."""
    existing = set(existing_databases())
    created = [name for name in names if name not in existing]
    for name in created:
        query(f'CREATE DATABASE "{name}" WITH SHARD DURATION {SHARD_DURATION}')
    return created


def stored_points(database: str, measurement: str, start: int, end: int,
                  source: Optional[str] = None
                  ) -> Dict[Tuple[FrozenSet[Tuple[str, str]], int], Dict[str, Any]]:
    """(tags, second) -> {field: value} of the points of one measurement in
    [start, end] that `source` wrote (every source when None): what InfluxDB
    keys each point by and the values it holds. Read `GROUP BY *` so every
    series carries its own tags whole, and with `epoch=s` so the second is the
    one `encode` writes. The values are for a comparison, never shown."""
    conditions = []
    if source is not None:
        conditions.append(f'"source" = {quote(source)}')
    conditions.append(f"time >= {time_literal(datetime.fromtimestamp(start, timezone.utc))}")
    conditions.append(f"time <= {time_literal(datetime.fromtimestamp(end, timezone.utc))}")
    payload = query(f"SELECT * FROM {_identifier(measurement)} WHERE {' AND '.join(conditions)} GROUP BY *",
                    database, epoch="s")
    points: Dict[Tuple[FrozenSet[Tuple[str, str]], int], Dict[str, Any]] = {}
    for result in payload.get("results") or []:
        # A partial answer would count the points it left out as new: refuse it
        # rather than report a wrong count (the server's max-row-limit).
        if result.get("partial"):
            raise InfluxError(f"InfluxDB answered the read of {measurement} partially: too many points "
                              "to compare in one response")
        for series in result.get("series") or []:
            tags = frozenset((name, value) for name, value in (series.get("tags") or {}).items()
                             if value not in (None, ""))
            columns = series.get("columns") or []
            for row in series.get("values") or []:
                if not row or not isinstance(row[0], int) or isinstance(row[0], bool):
                    continue
                points[(tags, row[0])] = {name: value for name, value in zip(columns[1:], row[1:])
                                          if value is not None}
    return points


def write(database: str, lines: List[str]) -> Tuple[int, Optional[str]]:
    """(points InfluxDB dropped, why -- None when only for being older than
    the database's retention, which is expected). A partial write keeps the
    batch's other points, so the run goes on rather than leave everything
    after it unwritten; any other refusal raises."""
    try:
        _post("/write", {"db": database, "precision": "s"}, "\n".join(lines).encode("utf-8"))
    except InfluxError as exc:
        match = _PARTIAL_WRITE.search(str(exc))
        if not match:
            raise
        reason = match["reason"].rstrip(": ")
        return int(match["dropped"]), None if "beyond retention policy" in reason else reason
    return 0, None


def quote(value: str) -> str:
    """An InfluxQL string literal (a tag value in a WHERE clause)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def time_literal(value: datetime) -> str:
    """An InfluxQL time literal, RFC3339 UTC, from an aware datetime."""
    if value.tzinfo is None:
        raise ValueError("a naive datetime has no instant: give it a time zone")
    return value.astimezone(timezone.utc).strftime("'%Y-%m-%dT%H:%M:%SZ'")


def _identifier(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def drop_source(database: str, measurement: str, source: str,
                start: Optional[datetime] = None, end: Optional[datetime] = None,
                user: Optional[str] = None) -> None:
    """Every series of one measurement written by one source: for a
    connector that rebuilds its own source from scratch. Safe only because
    each source has a single writer (docs/schema.md). With `start` and
    `end`, only its points in [start, end), of `user` when given: an import
    that covers those days only."""
    if (start is None) != (end is None):
        raise ValueError("drop_source takes both start and end, or neither")
    where = f"\"source\" = {quote(source)}" + (f" AND \"user\" = {quote(user)}" if user else "")
    if start is None:
        query(f"DROP SERIES FROM {_identifier(measurement)} WHERE {where}", database)
        return
    query(f"DELETE FROM {_identifier(measurement)} WHERE {where} "
          f"AND time >= {time_literal(start)} AND time < {time_literal(end)}", database)
