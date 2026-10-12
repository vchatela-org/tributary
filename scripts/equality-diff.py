#!/usr/bin/env python3
"""Equality diff for a port (docs/porting.md): does a connector's dry run
write exactly what the job it replaces wrote?

    equality-diff.py LINES INFLUX_JSON [--types FIELD_KEYS_JSON] [--since EPOCH]
                     [--latest MEASUREMENT ...] [--show-values]

LINES        the dry run's line protocol, one point per line, precision
             seconds; lines starting with '#' are skipped, so a run's preview
             (GET /api/runs/{id}) can be saved as is.
INFLUX_JSON  what InfluxDB holds, from the influx CLI, tags grouped:
               influx -database DB -format json -precision s -execute \\
                 "SELECT * FROM m1 WHERE \\"source\\"='x' GROUP BY *; SELECT * FROM m2 ... GROUP BY *"
--types      the stored field types (influx -database DB -format json -execute 'SHOW FIELD KEYS'):
             each field the dry run writes must have the type the database holds.
--since      compare the points at or after this epoch (seconds) only, on both sides.
--latest     a measurement stamped with the run's time (not the data's): per
             series, its newest point on each side is compared, times ignored.

A point is keyed by measurement + tags + time: that key must exist on both
sides, with the same fields and values. Prints counts and field names, never
a value or a tag value (health data, account ids), unless --show-values.
Exit status 1 on any difference.

Standard library only: runs anywhere Python 3 does.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict

INFLUX_TYPES = {"float": float, "integer": int, "string": str, "boolean": bool}


def _unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


def _split(text, sep):
    """Split on `sep` outside quotes and escapes."""
    parts, current, quoted, escaped = [], [], False, False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == sep and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _value(raw):
    """A line-protocol field value -> (python value, type)."""
    if raw.startswith('"'):
        return _unescape(raw[1:-1]), str
    if raw.endswith("i"):
        return int(raw[:-1]), int
    if raw in ("t", "T", "true", "True", "TRUE", "f", "F", "false", "False", "FALSE"):
        return raw[0] in "tT", bool
    return float(raw), float


def parse_lines(path):
    """{(measurement, tags, time): {field: (value, type)}}"""
    points = {}
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = _split(line, " ")
            if len(parts) != 3:
                raise SystemExit(f"{path}:{number}: not a line of line protocol with a time")
            head, fields, time = parts
            measurement, *tags = re.split(r"(?<!\\),", head)
            tag_set = frozenset(tuple(_unescape(x) for x in re.split(r"(?<!\\)=", t, maxsplit=1)) for t in tags)
            values = {}
            for item in _split(fields, ","):
                name, raw = re.split(r"(?<!\\)=", item, maxsplit=1)
                values[_unescape(name)] = _value(raw)
            points[(_unescape(measurement), tag_set, int(time))] = values
    return points


def parse_influx(path):
    """{(measurement, tags, time): {field: value}} from the CLI's JSON (one
    document per statement, or one with every result)."""
    points = {}
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    decoder, position = json.JSONDecoder(), 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        document, position = decoder.raw_decode(text, position)
        for result in document.get("results") or []:
            if result.get("error"):
                raise SystemExit(f"{path}: InfluxDB error: {result['error']}")
            for series in result.get("series") or []:
                tags = frozenset((k, v) for k, v in (series.get("tags") or {}).items() if v not in (None, ""))
                columns = series["columns"]
                for row in series.get("values") or []:
                    record = dict(zip(columns, row))
                    time = record.pop("time")
                    if not isinstance(time, int):
                        raise SystemExit(f"{path}: times must be epochs: run the CLI with -precision s")
                    points[(series["name"], tags, time)] = {k: v for k, v in record.items() if v is not None}
    return points


def parse_types(path):
    """{(measurement, field): type} from SHOW FIELD KEYS."""
    types = {}
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    for result in document.get("results") or []:
        for series in result.get("series") or []:
            for name, kind in series.get("values") or []:
                types[(series["name"], name)] = INFLUX_TYPES.get(kind)
    return types


def _same(stored, written):
    value, kind = written
    if kind is float:
        return isinstance(stored, (int, float)) and not isinstance(stored, bool) and float(stored) == value
    return stored == value and type(stored) is kind


def _latest(points, measurement):
    """Each series' newest point, keyed without its time."""
    newest = {}
    for (m, tags, time), fields in points.items():
        if m == measurement and (tags not in newest or time > newest[tags][0]):
            newest[tags] = (time, fields)
    return {(measurement, tags, None): fields for tags, (_time, fields) in newest.items()}


def compare(written, stored, types=None, since=None, latest=(), show_values=False):
    """Prints the report; returns whether both sides are equal."""
    if since is not None:
        written = {k: v for k, v in written.items() if k[2] >= since or k[0] in latest}
        stored = {k: v for k, v in stored.items() if k[2] >= since or k[0] in latest}
    for measurement in latest:
        written = {**{k: v for k, v in written.items() if k[0] != measurement}, **_latest(written, measurement)}
        stored = {**{k: v for k, v in stored.items() if k[0] != measurement}, **_latest(stored, measurement)}

    equal = True
    for measurement in sorted({k[0] for k in written} | {k[0] for k in stored}):
        mine = {k: v for k, v in written.items() if k[0] == measurement}
        theirs = {k: v for k, v in stored.items() if k[0] == measurement}
        both = mine.keys() & theirs.keys()
        differing, fields_differing = 0, Counter()
        for key in sorted(both, key=lambda k: (k[2] or 0, sorted(k[1]))):
            names = {n for n in mine[key].keys() | theirs[key].keys()
                     if n not in mine[key] or n not in theirs[key] or not _same(theirs[key][n], mine[key][n])}
            if names:
                differing += 1
                fields_differing.update(names)
                if show_values:
                    print(f"  differs {key}: dry run {mine[key]} / InfluxDB {theirs[key]}")
        only_mine, only_theirs = mine.keys() - theirs.keys(), theirs.keys() - mine.keys()
        series_mine = Counter(tuple(sorted(k for k, _v in key[1])) for key in mine)
        series_theirs = Counter(tuple(sorted(k for k, _v in key[1])) for key in theirs)
        print(f"{measurement}: {len(mine)} point(s) in the dry run, {len(theirs)} in InfluxDB; "
              f"{len(both) - differing} equal, {differing} differing, {len(only_mine)} only in the dry run, "
              f"{len(only_theirs)} only in InfluxDB"
              + (" (newest of each series, times ignored)" if measurement in latest else ""))
        print(f"  tag keys: dry run {dict(series_mine)} / InfluxDB {dict(series_theirs)}")
        if fields_differing:
            print(f"  differing fields: {dict(fields_differing)}")
        if show_values:
            for key in sorted(only_mine, key=lambda k: (k[2] or 0, sorted(k[1]))):
                print(f"  only in the dry run: {key}")
            for key in sorted(only_theirs, key=lambda k: (k[2] or 0, sorted(k[1]))):
                print(f"  only in InfluxDB: {key}")
        if differing or only_mine or only_theirs:
            equal = False

    if types is not None:
        wrong = defaultdict(set)
        for (measurement, _tags, _time), fields in written.items():
            for name, (_value, kind) in fields.items():
                held = types.get((measurement, name))
                if held is not None and held is not kind:
                    wrong[measurement].add(f"{name} ({kind.__name__}, InfluxDB holds {held.__name__})")
                elif held is None:
                    wrong[measurement].add(f"{name} (new field: no type in InfluxDB yet)")
        for measurement, problems in sorted(wrong.items()):
            print(f"{measurement}: field types differ: {', '.join(sorted(problems))}")
            equal = False
        if not wrong:
            print("field types: every field written has the type InfluxDB holds")
    print("EQUAL" if equal else "DIFFERENT")
    return equal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("lines")
    parser.add_argument("influx_json")
    parser.add_argument("--types")
    parser.add_argument("--since", type=int)
    parser.add_argument("--latest", action="append", default=[])
    parser.add_argument("--show-values", action="store_true")
    args = parser.parse_args(argv)
    equal = compare(parse_lines(args.lines), parse_influx(args.influx_json),
                    parse_types(args.types) if args.types else None, args.since, tuple(args.latest),
                    args.show_values)
    return 0 if equal else 1


if __name__ == "__main__":
    sys.exit(main())
