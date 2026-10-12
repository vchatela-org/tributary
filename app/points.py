"""Points, and how they become InfluxDB 1.x line protocol.

A connector declares the measurements it writes (Measurement: database,
tags, field types) and yields Points; this module checks every point
against its declaration, aggregates them and formats the lines that
app/influxdb.py writes.

Field types are part of the contract: InfluxDB 1.x rejects a write whose
field type differs from what the shard already holds, and other tools write
into the same databases (docs/schema.md). So every field is declared once
(int / float / str) and always written the same way; an undeclared field or
tag is an error, not a guess.

Privacy: InfluxDB 1.x usually runs without authentication. Only numbers and
low-cardinality tags leave; a text field exists only where a measurement
declares it (`str`), and tests/test_privacy.py fails on any text field that
isn't on its short allow-list.

Points are keyed by measurement + tags + second: InfluxDB overwrites a
point with the same key, so points of one run that share a key are merged
first, numbers summed and texts joined (two sessions started in the same
second on the same device must add up, not replace each other).

Times are UTC (naive datetimes are read as UTC). Measurements declared with
`local_time` also get local_hour / local_weekday (in TRIBUTARY_TZ: InfluxQL
can't shift a time zone, and an evening-reading panel needs the local
hour) and a `time_precision` tag: `second`, or `day` for a date without a
time of day, which gets no local hour.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

# Tags every point may carry: the tracker (a connector's `source`) and the
# person (TRIBUTARY_INFLUXDB_USER, or the connector's own per account).
COMMON_TAGS = ("source", "user")
LOCAL_FIELDS: Dict[str, type] = {"local_hour": int, "local_weekday": int}
TEXT_MAX = 250


@dataclass(frozen=True)
class Measurement:
    database: str
    name: str
    # The measurement's own tag keys, besides `source` and `user`.
    tags: Tuple[str, ...]
    # Every field it may write, with its type; `str` opts a text field in.
    fields: Mapping[str, type]
    # local_hour / local_weekday fields and a time_precision tag.
    local_time: bool = False

    @property
    def field_types(self) -> Dict[str, type]:
        return {**self.fields, **(LOCAL_FIELDS if self.local_time else {})}

    @property
    def tag_keys(self) -> Tuple[str, ...]:
        return COMMON_TAGS + self.tags + (("time_precision",) if self.local_time else ())


@dataclass
class Point:
    measurement: Measurement
    # UTC: an aware datetime, or a naive one read as UTC.
    time: datetime
    tags: Dict[str, Optional[str]] = field(default_factory=dict)
    fields: Dict[str, Any] = field(default_factory=dict)
    # A date without a time of day (time_precision=day, no local hour).
    day_precision: bool = False


def epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return math.floor(value.timestamp())


# ────────────────────────────────────────────────
# Line protocol
# ────────────────────────────────────────────────

def _escape_key(value: str) -> str:
    """Measurement names, tag keys/values and field keys. A line break would
    end the line: it becomes a space first, escaped like any other."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def _format_field(name: str, value: Any, kind: type) -> str:
    if kind is int:
        return f"{_escape_key(name)}={int(value)}i"
    if kind is float:
        return f"{_escape_key(name)}={float(value)!r}"
    if kind is str:
        text = str(value)[:TEXT_MAX].replace("\r", " ").replace("\n", " ").replace("\\", "\\\\").replace('"', '\\"')
        return f'{_escape_key(name)}="{text}"'
    raise ValueError(f"unsupported field type {kind!r} for {name}")


def format_line(measurement: str, tags: Mapping[str, Optional[str]], fields: Mapping[str, Any],
                types: Mapping[str, type], epoch: int) -> str:
    """One point. Tags sorted (what InfluxDB recommends), empty ones left
    out (InfluxDB refuses an empty tag value); every field must be declared."""
    tag_part = "".join(f",{_escape_key(k)}={_escape_key(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    parts = []
    for name, value in sorted(fields.items()):
        if name not in types:
            raise ValueError(f"field {name!r} of {measurement} is not declared")
        parts.append(_format_field(name, value, types[name]))
    if not parts:
        raise ValueError(f"a point of {measurement} has no field")
    return f"{_escape_key(measurement)}{tag_part} {','.join(parts)} {epoch}"


# ────────────────────────────────────────────────
# Points -> lines
# ────────────────────────────────────────────────

@lru_cache(maxsize=4)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local_fields(epoch: int, tz: str) -> Dict[str, int]:
    local = datetime.fromtimestamp(epoch, timezone.utc).astimezone(_zone(tz))
    return {"local_hour": local.hour, "local_weekday": local.isoweekday()}


def _usable(value: Any) -> bool:
    if value is None or value == "":
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def encode(points: Iterable[Point], source: str, user: Optional[str] = None,
           tz: str = "Europe/Paris") -> Dict[str, List[str]]:
    """{database: lines}: the points checked against their declarations,
    merged per key (measurement + tags + second), oldest first."""
    groups: Dict[Tuple, Dict[str, Any]] = {}
    for point in points:
        m = point.measurement
        types = m.field_types
        unknown = set(point.tags) - set(m.tag_keys)
        if unknown:
            raise ValueError(f"tag(s) {sorted(unknown)} of {m.name} not declared")
        epoch = epoch_seconds(point.time)
        tags = {"source": source, "user": user, **point.tags}
        if m.local_time:
            tags["time_precision"] = "day" if point.day_precision else "second"
        tags = {k: str(v) for k, v in tags.items() if v not in (None, "")}
        key = (m.database, m.name, epoch, tuple(sorted(tags.items())))
        acc = groups.get(key)
        if acc is None:
            acc = groups[key] = {"measurement": m, "tags": tags, "epoch": epoch, "numbers": {}, "texts": {},
                                 "local": local_fields(epoch, tz) if m.local_time and not point.day_precision else {}}
        for name, value in point.fields.items():
            kind = types.get(name)
            if kind is None or name in LOCAL_FIELDS:
                raise ValueError(f"field {name!r} of {m.name} is not declared")
            if not _usable(value):
                continue
            if kind is str:
                acc["texts"].setdefault(name, set()).add(str(value))
            else:
                acc["numbers"][name] = acc["numbers"].get(name, 0) + value

    out: Dict[str, List[str]] = {}
    for key in sorted(groups, key=lambda k: (k[2], k[0], k[1], k[3])):
        acc = groups[key]
        m: Measurement = acc["measurement"]
        fields: Dict[str, Any] = dict(acc["numbers"])
        for name, values in acc["texts"].items():
            fields[name] = " / ".join(sorted(values))
        if not fields:
            continue  # nothing but empty values: no point to write
        fields.update(acc["local"])
        out.setdefault(m.database, []).append(format_line(m.name, acc["tags"], fields, m.field_types, acc["epoch"]))
    return out

