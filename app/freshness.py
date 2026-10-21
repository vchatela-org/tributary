"""When a compatible tool's data last arrived: the one read Tributary makes of
another tool's data, and it reads timestamps only.

A tool Tributary does not run (app/categories.py) may name a probe here. Its
card then carries one line, wherever the tool is listed:

    Garmin data up to 2026-10-12 13:14 · last watch upload 2026-10-13 07:47

Each time on it is the `time` of the newest row an InfluxQL statement returns.
The values that come back beside it are never read: not kept, not logged, not
shown. When the newest data is older than the probe's threshold, the line is
marked stale -- a warning, never an error, since Tributary does not run the tool.

What it cannot tell is *why* nothing new arrived. garmin-grafana writes nothing
when it polls and finds no new upload, so a stopped fetcher and a watch that has
not synced look the same from InfluxDB: the line says when data last arrived,
not why (docs/compatible-tools.md).

Cheap and harmless by construction. An answer, a failure included, is kept in
memory for a few minutes; one refresh has a short time budget for all its
statements; and no InfluxDB, an unreachable or slow one, a missing database or
no point all read as "no data seen", never as an error on the page. Only the
pages ask for it, never the fragment the overview polls.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, NamedTuple, Optional, Tuple

from app import influxdb
from app.categories import ExternalTool
from app.config import settings

logger = logging.getLogger(__name__)

# How long an answer is reused. garmin-grafana itself looks for a new upload
# every 5 minutes: reading more often would show nothing newer.
CACHE_SECONDS = 300.0
# The time one refresh may take, all its statements together: a slow or
# unreachable InfluxDB costs a page this much at most, once per CACHE_SECONDS.
TIMEOUT_SECONDS = 2.0


class Mark(NamedTuple):
    """One time on the line: its words, and the statement whose newest row's
    time it is."""
    label: str
    statement: str


class Probe(NamedTuple):
    """How to tell when a tool's data last arrived."""
    database: Callable[[], str]  # read at each call; empty turns the line off
    stale_hours: Callable[[], float]
    marks: Tuple[Mark, ...]  # in the line's order; the first one found decides staleness
    nothing: str  # the muted line when no mark is found
    limit: str  # what the line cannot say, in its tooltip


PROBES: Dict[str, Probe] = {
    "garmin-grafana": Probe(
        database=lambda: settings.garmin_grafana_database,
        stale_hours=lambda: settings.garmin_grafana_stale_hours,
        marks=(
            # Its own cursor: where its next fetch starts.
            Mark("Garmin data up to", 'SELECT last("HeartRate") FROM "HeartRateIntraday"'),
            # Written when it sees a new upload from the watch.
            Mark("last watch upload", 'SELECT * FROM "DeviceSync" ORDER BY time DESC LIMIT 1'),
        ),
        nothing="No Garmin data seen",
        limit="garmin-grafana writes nothing when it polls and finds no new upload, so a stopped "
              "fetcher and a watch that has not synced look the same from InfluxDB.",
    ),
    "fitbit-grafana": Probe(
        database=lambda: settings.fitbit_grafana_database,
        stale_hours=lambda: settings.fitbit_grafana_stale_hours,
        marks=(
            # Its heart-rate samples: the newest one is where its next fetch starts.
            # (Its battery and last-sync points are not written under Google's API.)
            Mark("Fitbit data up to", 'SELECT last("value") FROM "HeartRate_Intraday"'),
        ),
        nothing="No Fitbit data seen",
        limit="fitbit-grafana writes nothing when it polls and finds no new data, so a stopped "
              "fetcher and a bracelet that has not synced look the same from InfluxDB.",
    ),
}


class Seen(NamedTuple):
    label: str
    at: datetime  # UTC


class Freshness(NamedTuple):
    """What a tool's line shows."""
    seen: Tuple[Seen, ...]  # the marks found, in the probe's order; empty: nothing seen
    stale: bool
    stale_after: str  # the threshold, in words ("24 h")
    nothing: str
    title: str


# (probe, InfluxDB URL, database) -> (when it was read, the marks found, why none were)
_cache: Dict[Tuple[str, str, str], Tuple[float, Tuple[Seen, ...], str]] = {}
_lock = threading.Lock()
_clock = time.monotonic  # the cache's clock (tests move it)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clear() -> None:
    with _lock:
        _cache.clear()


def _newest_time(payload: Dict) -> Optional[datetime]:
    """The newest `time` in an answer asked with epoch=s. Only that column is
    looked at: the rest of each row is left as it came, unread."""
    newest = None
    for result in payload.get("results") or []:
        for series in result.get("series") or []:
            columns = series.get("columns") or []
            if "time" not in columns:
                continue
            at = columns.index("time")
            for row in series.get("values") or []:
                value = row[at]
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and (newest is None or value > newest):
                    newest = value
    return None if newest is None else datetime.fromtimestamp(newest, timezone.utc)


def _read(key: str, probe: Probe, database: str) -> Tuple[Tuple[Seen, ...], str]:
    """(the marks found, why none were). Stops at the first failure: the next
    statement would only wait on the same InfluxDB again."""
    seen = []
    deadline = time.monotonic() + TIMEOUT_SECONDS
    for mark in probe.marks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return tuple(seen), "InfluxDB answered too slowly"
        try:
            at = _newest_time(influxdb.query(mark.statement, database, timeout=remaining, epoch="s"))
        except influxdb.InfluxError as exc:
            # Its message names the statement and InfluxDB's reason, no value.
            logger.debug("Freshness of %s: %s", key, exc)
            if "database not found" in str(exc):
                return tuple(seen), f"InfluxDB has no {database} database"
            return tuple(seen), "InfluxDB could not be read"
        except Exception as exc:  # noqa: BLE001 -- a line of context never breaks a page
            logger.debug("Freshness of %s: %s", key, type(exc).__name__)
            return tuple(seen), "InfluxDB could not be read"
        if at is not None:
            seen.append(Seen(mark.label, at))
    return tuple(seen), "" if seen else f"no point in {database} yet"


def _cached(key: str, probe: Probe, database: str) -> Tuple[Tuple[Seen, ...], str]:
    cache_key = (key, settings.influxdb_url or "", database)
    # One refresh at a time: two pages opened together read InfluxDB once.
    with _lock:
        hit = _cache.get(cache_key)
        if hit is not None and _clock() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
        seen, why = _read(key, probe, database)
        _cache[cache_key] = (_clock(), seen, why)
        return seen, why


def line(key: Optional[str]) -> Optional[Freshness]:
    """The line of the probe `key`, or None when there is none to show (no such
    probe, or its database setting is empty)."""
    probe = PROBES.get(key or "")
    database = probe.database() if probe else ""
    if not database:
        return None
    if influxdb.is_configured():
        seen, why = _cached(key, probe, database)
    else:
        seen, why = (), "InfluxDB is not configured"
    hours = probe.stale_hours()
    stale = bool(seen) and _now() - seen[0].at > timedelta(hours=hours)
    if seen:
        title = f"When data last arrived in {database}, not why: {probe.limit}"
    else:
        title = f"Nothing read: {why}. Tributary does not run this tool; it only reads when its data last arrived."
    return Freshness(seen=seen, stale=stale, stale_after=f"{hours:g} h", nothing=probe.nothing, title=title)


def for_tools(tools: Iterable[ExternalTool]) -> Dict[str, Freshness]:
    """The lines of the tools that have one, by label. A tool listed under
    several categories is read once."""
    out: Dict[str, Freshness] = {}
    for tool in tools:
        if tool.label not in out:
            found = line(tool.freshness)
            if found is not None:
                out[tool.label] = found
    return out
