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
from typing import Dict, List, Optional, Tuple
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


def _post(path: str, params: Dict[str, str], body: bytes = b"") -> bytes:
    url = settings.influxdb_url.rstrip("/") + path + "?" + urlencode(params)
    if urlparse(url).scheme not in ("http", "https"):
        raise InfluxError(f"Invalid TRIBUTARY_INFLUXDB_URL ({settings.influxdb_url}): "
                          "it must start with http:// or https://.")
    request = Request(url, data=body, method="POST", headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
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


def query(statement: str, database: Optional[str] = None) -> Dict:
    """One InfluxQL statement. /query answers 200 even when the statement
    fails: the error is in the body. Connectors may read through this too
    (e.g. to skip what another source already measured)."""
    params = {"q": statement}
    if database:
        params["db"] = database
    payload = json.loads(_post("/query", params) or b"{}")
    for result in payload.get("results") or []:
        if result.get("error"):
            raise InfluxError(f"InfluxDB: {result['error']} ({statement})")
    return payload


def create_missing_databases(names: List[str]) -> List[str]:
    """Create the databases that don't exist yet, with year-long shards:
    InfluxDB's default for a database kept forever is one shard per week,
    and a few years of history made 600+ mostly tiny shards, each costing
    memory and file handles for good. An existing database is left exactly
    as it is."""
    payload = query("SHOW DATABASES")
    existing = {
        row[0] for result in payload.get("results") or [] for series in result.get("series") or []
        for row in series.get("values") or []
    }
    created = [name for name in names if name not in existing]
    for name in created:
        query(f'CREATE DATABASE "{name}" WITH SHARD DURATION {SHARD_DURATION}')
    return created


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


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _identifier(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def drop_source(database: str, measurement: str, source: str) -> None:
    """Every series of one measurement written by one source: for a
    connector that rebuilds its own source from scratch. Safe only because
    each source has a single writer (docs/schema.md)."""
    query(f"DROP SERIES FROM {_identifier(measurement)} WHERE \"source\" = {_quote(source)}", database)
