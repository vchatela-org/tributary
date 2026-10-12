import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import connectors, points
from app.points import Measurement, Point, encode, format_line
from app.schema import BOOK_FINISHED, READING_SESSION
from tests.fakes import epoch, parse

DOCS = Path(__file__).resolve().parents[1] / "docs" / "schema.md"


# -- Line protocol --------------------------------------------------------------------

def test_escaping_and_declared_fields():
    line = format_line(
        "my measure", {"b key": "a,b=c d", "empty": "", "none": None},
        {"title": 'The "title" \\ end', "count": 2, "minutes": 1.5}, {"title": str, "count": int, "minutes": float},
        1700000000,
    )
    assert line == 'my\\ measure,b\\ key=a\\,b\\=c\\ d count=2i,minutes=1.5,title="The \\"title\\" \\\\ end" 1700000000'
    with pytest.raises(ValueError, match="not declared"):
        format_line("m", {}, {"oops": 1}, {}, 0)


def test_line_breaks_never_end_a_line():
    line = format_line("m", {"device": "Bedroom\ntablet"}, {"title": "a\r\nb"}, {"title": str}, 1)
    assert line == 'm,device=Bedroom\\ tablet title="a  b" 1'


def test_ints_stay_ints_and_floats_stay_floats():
    line = format_line("m", {}, {"count": 2.0, "minutes": 3}, {"count": int, "minutes": float}, 1)
    assert line == "m count=2i,minutes=3.0 1"


def test_long_texts_are_cut():
    line = format_line("m", {}, {"title": "x" * 1000}, {"title": str}, 1)
    assert line == f'm title="{"x" * points.TEXT_MAX}" 1'


# -- Points -> lines --------------------------------------------------------------------

def at(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_common_tags_local_fields_and_order():
    lines = encode([
        Point(READING_SESSION, at(2025, 3, 2, 9, 0), {"kind": "audiobook", "device": "Phone"},
              {"minutes": 10.0, "sessions": 1, "title": "B"}),
        Point(READING_SESSION, at(2025, 3, 1, 20, 0, 5, 900), {"kind": "audiobook", "device": "Phone"},
              {"minutes": 30.0, "sessions": 1, "title": "A"}),
    ], source="audiobookshelf", user="someone", tz="Europe/Paris")
    first, second = lines["MediaStats"]
    _m, tags, fields, ts = parse(first)
    assert ts == epoch(2025, 3, 1, 20, 0, 5)  # oldest first, to the second
    assert tags == {"source": "audiobookshelf", "user": "someone", "kind": "audiobook", "device": "Phone",
                    "time_precision": "second"}
    # 20:00 UTC is 21:00 in Paris in March; a Saturday.
    assert (fields["local_hour"], fields["local_weekday"]) == ("21i", "6i")
    assert parse(second)[3] == epoch(2025, 3, 2, 9, 0)


def test_same_key_same_second_is_summed_never_overwritten():
    session = dict(tags={"kind": "audiobook", "device": "Phone"})
    lines = encode([
        Point(READING_SESSION, at(2025, 3, 1, 20, 0, 5, 100), fields={"minutes": 10.0, "sessions": 1, "title": "B"},
              **session),
        Point(READING_SESSION, at(2025, 3, 1, 20, 0, 5, 800), fields={"minutes": 5.5, "sessions": 1, "title": "A"},
              **session),
        # Another device: another series, its own point.
        Point(READING_SESSION, at(2025, 3, 1, 20, 0, 5), {"kind": "audiobook", "device": "Tablet"},
              {"minutes": 1.0, "sessions": 1, "title": "A"}),
    ], source="audiobookshelf")
    by_device = {parse(line)[1]["device"]: parse(line)[2] for line in lines["MediaStats"]}
    assert (by_device["Phone"]["minutes"], by_device["Phone"]["sessions"], by_device["Phone"]["title"]) == (
        "15.5", "2i", '"A / B"')
    assert by_device["Tablet"]["minutes"] == "1.0"


def test_no_user_tag_unless_configured_and_empty_tags_left_out():
    line, = encode([Point(BOOK_FINISHED, at(2025, 1, 1), {"kind": "audiobook"}, {"count": 1, "title": "T"})],
                   source="audiobookshelf", user=None)["MediaStats"]
    assert "user=" not in line
    line, = encode([Point(READING_SESSION, at(2025, 1, 1), {"kind": "audiobook", "device": None},
                          {"minutes": 1.0, "sessions": 1})], source="audiobookshelf")["MediaStats"]
    assert "device" not in parse(line)[1]


def test_a_connector_may_set_the_user_per_point():
    """Several accounts in one connector (e.g. one per person)."""
    line, = encode([Point(BOOK_FINISHED, at(2025, 1, 1), {"kind": "audiobook", "user": "second"},
                          {"count": 1})], source="x", user="first")["MediaStats"]
    assert parse(line)[1]["user"] == "second"


def test_a_connector_may_write_no_user_tag():
    """A port reproducing an old job that wrote none, whatever TRIBUTARY_INFLUXDB_USER says."""
    line, = encode([Point(BOOK_FINISHED, at(2025, 1, 1), {"kind": "audiobook", "user": None},
                          {"count": 1})], source="x", user="first")["MediaStats"]
    assert "user" not in parse(line)[1]


def test_day_precision_has_no_local_hour():
    line, = encode([Point(BOOK_FINISHED, datetime(2025, 3, 5), {"kind": "audiobook"}, {"count": 1},
                          day_precision=True)], source="x")["MediaStats"]
    _m, tags, fields, ts = parse(line)
    assert tags["time_precision"] == "day" and "local_hour" not in fields
    assert ts == epoch(2025, 3, 5)  # naive times are UTC


def test_measurements_without_local_time_get_neither_tag_nor_fields():
    weight = Measurement("BodyStats", "body_composition", tags=(), fields={"weight": float})
    line, = encode([Point(weight, at(2025, 1, 1, 7, 0), {}, {"weight": 70.1})], source="scale", user="me")["BodyStats"]
    assert line == f"body_composition,source=scale,user=me weight=70.1 {epoch(2025, 1, 1, 7, 0)}"


def test_undeclared_tags_and_fields_are_refused():
    with pytest.raises(ValueError, match="tag"):
        encode([Point(READING_SESSION, at(2025, 1, 1), {"ip": "x"}, {"minutes": 1.0})], source="x")
    with pytest.raises(ValueError, match="not declared"):
        encode([Point(READING_SESSION, at(2025, 1, 1), {}, {"author": "x"})], source="x")
    with pytest.raises(ValueError, match="not declared"):
        encode([Point(READING_SESSION, at(2025, 1, 1), {}, {"local_hour": 3})], source="x")


def test_empty_and_not_finite_values_are_skipped():
    lines = encode([
        Point(READING_SESSION, at(2025, 1, 1), {"kind": "audiobook"}, {"minutes": math.nan, "sessions": 1, "title": ""}),
        Point(BOOK_FINISHED, at(2025, 1, 2), {"kind": "audiobook"}, {"count": None}),
    ], source="x")
    line, = lines["MediaStats"]
    assert parse(line)[2].keys() == {"sessions", "local_hour", "local_weekday"}


# -- The contract -----------------------------------------------------------------------

def all_measurements():
    return [m for c in connectors.all_connectors() for m in c.measurements]


def test_every_field_type_is_stable():
    """InfluxDB 1.x rejects a field whose type changes: one type per field
    name within a measurement, whatever the connector."""
    seen = {}
    for m in all_measurements():
        for name, kind in m.field_types.items():
            assert kind in (int, float, str)
            assert seen.setdefault((m.database, m.name, name), kind) is kind, (m.name, name)


def _documented():
    """docs/schema.md's tables: (database, measurement) -> ({field: type}, {tags})."""
    contract = {}
    database = None
    for line in DOCS.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^### `(\w+)`", line)
        if heading:
            database = heading.group(1)
            continue
        row = re.match(r"^\| `(\w+)` \| (.+?) \| (.+?) \|", line)
        if row and database:
            measurement, tags, fields = row.groups()
            contract[(database, measurement)] = (
                {name: kind for name, kind in re.findall(r"`(\w+)` \((int|float|str)\)", fields)},
                set(re.findall(r"`(\w+)`", tags)),
            )
    return contract


def test_the_documented_schema_matches_the_code():
    """docs/schema.md is the contract other tools write against: every field
    a connector writes is documented with the same type, every tag too."""
    contract = _documented()
    assert contract, "no table found in docs/schema.md"
    for m in all_measurements():
        fields, tags = contract[(m.database, m.name)]
        for name, kind in m.field_types.items():
            assert fields.get(name) == kind.__name__, (m.name, name)
        assert set(m.tag_keys) <= tags, (m.name, set(m.tag_keys) - tags)


def test_media_stats_types_match_what_takeback_writes():
    """MediaStats is shared with Takeback (Kindle, Audible): identical types,
    or InfluxDB refuses one writer's points."""
    takeback = {
        "reading_session": {"minutes": float, "sessions": int, "pages": int, "title": str,
                            "local_hour": int, "local_weekday": int},
        "book_finished": {"count": int, "title": str, "local_hour": int, "local_weekday": int},
    }
    for m in (READING_SESSION, BOOK_FINISHED):
        for name, kind in m.field_types.items():
            assert takeback[m.name][name] is kind, (m.name, name)


def test_epoch_seconds_of_dates_and_datetimes():
    assert points.epoch_seconds(datetime(2025, 1, 1)) == epoch(2025, 1, 1)
    assert points.epoch_seconds(datetime(2025, 1, 1, 0, 0, 0, 999999, tzinfo=timezone.utc)) == epoch(2025, 1, 1)
