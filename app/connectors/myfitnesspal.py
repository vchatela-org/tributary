"""MyFitnessPal (food, exercise and weigh-ins): the records of an uploaded
export, into BodyStats..body_composition, ..nutrition and ..exercise.

Ported from a one-off import script, writing exactly its series (same
measurements, tags, fields, types and times), so importing the export again
rewrites that history in place and never duplicates it (docs/porting.md).
source=myfitnesspal had that script as its only writer.

The export is a zip of three CSVs (weigh-ins, food by meal, exercise), in the
app's language, so each file is recognised by its columns. It covers the date
range the user picked when exporting, which the app writes in each member's
name ("...-du-2019-01-01-au-2024-12-31.csv"). An import replaces each series
over the days its file covers (`rebuild_own_source` + FileImport.spans: the
range in the file's name, widened to that file's own records), for
source=myfitnesspal and TRIBUTARY_INFLUXDB_USER, as the script's --replace
did -- rather than adding to it or overwriting point by point: a workout the
export no longer has is one the app doesn't know any more, and a newer
export shifts the per-day workout timestamps. The days outside them are left
as they are: an export of the last month never empties the years before it.
An import that holds nothing to write back fails rather than leave the
series empty, as the script refused to as well. Upload only: a folder of
exports, each its own range, is not the whole history a watched folder's
rebuild would make it.

A weigh-in is imported only when no other source has a reading the same local
day within WEIGHT_TOLERANCE_KG: the app shows the readings the Withings and
Renpho scales synced into it, and those are in BodyStats under their own
source already. That reads InfluxDB (app.influxdb.query), as the script did,
even for a dry run -- and only ever its `user` tag's readings, over the
weigh-ins' days. It is also why
a weigh-in the scales reported on the same day *after* the export was
imported is not written again: the day's reading belongs to the scale's
source, and this import drops the copy.

Every record is stamped at 12:00 local (TRIBUTARY_TZ) on its date, as the
script did: a day lands on the right date whether a query groups days in UTC
or in Paris time. Several workouts on one date are a second apart, by their
position in the day. Of the export's columns, a food entry's 0 in a secondary
nutrient means "not reported" and is left absent (the macros always carry
their 0), the four vitamin/mineral ones are not written (no unit, a
percentage mixed with amounts), and Garmin's daily steps on its
calorie-adjustment rows are not imported (GarminStats has them). Nothing is
kept from an export but the records: no account, no id, no member name.
"""
import collections
import csv
import io
import re
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Dict, List, NamedTuple, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app import influxdb
from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport, Span
from app.points import Measurement, Point
from app.schema import BODY_COMPOSITION

PREFIX = "TRIBUTARY_MYFITNESSPAL_"
SOURCE = "myfitnesspal"
NOON = time(12, 0)
# The app stores weights to 0.1 kg; Withings readings carry more decimals.
WEIGHT_TOLERANCE_KG = 0.15

# The export is in the app's language (French here). Files are recognised by
# a header column: the member names are the app's language too, and some
# unzip tools mangle them.
KIND_BY_COLUMN = {"Poids": "weight", "Repas": "food", "Exercices": "exercise"}
# The date range the user picked, in each member's name.
NAMED_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

MEALS = {"Petit déjeuner": "breakfast", "Déjeuner": "lunch", "Dîner": "dinner", "Snacks": "snacks"}

# Macros are in every food entry, so 0 is a real 0.
FOOD_FIELDS = {
    "Calories": "energy_kcal",
    "Lipides (g)": "fat_g",
    "Glucides (g)": "carbs_g",
    "Protéines (g)": "protein_g",
}
# Many food entries leave these out and the export writes 0, so a 0 here means
# "not reported" and the field is left absent.
FOOD_FIELDS_NONZERO = {
    "Acides gras saturés": "saturated_fat_g",
    "Acides gras polyinsaturés": "polyunsaturated_fat_g",
    "Acides gras monoinsaturés": "monounsaturated_fat_g",
    "Acides gras trans": "trans_fat_g",
    "Cholestérol": "cholesterol_mg",
    "Sodium (mg)": "sodium_mg",
    "Potassium": "potassium_mg",
    "Fibres": "fiber_g",
    "Sucres": "sugar_g",
}
# First matching prefix of the lower-cased activity name wins.
CATEGORIES = (
    ("course", "run"),
    ("vélo elliptique", "elliptical"),
    ("vélo", "bike"),
    ("musculation", "strength"),
    ("aérobic", "aerobic"),
    ("marche", "walk"),
    ("randonnée", "hike"),
    ("natation", "swim"),
    ("escalade", "climb"),
    ("rafting", "paddle"),
)

# The columns each file must have: a renamed one fails the import rather than
# write its values as zeros (the script failed on it too).
COLUMNS = {
    "weight": ("Date", "Poids", "Body Fat %"),
    "food": ("Date", "Repas", *FOOD_FIELDS, *FOOD_FIELDS_NONZERO),
    "exercise": ("Date", "Exercices", "Calories liées aux exercices", "Minutes d'exercice"),
}

# The job's measurements, as it wrote them. `source` and `user` are on every
# point (the core adds them: `user` is TRIBUTARY_INFLUXDB_USER, the job's one
# account). body_composition is shared, so it is declared once in app/schema.py:
# this connector sets neither `scale` nor `user_id`, so its weigh-ins key on
# source+user only.
# Two rows of one meal on a day share a key: the later one wins, as the
# script's consecutive writes did.
NUTRITION = Measurement(
    database="BodyStats", name="nutrition", tags=("meal",),
    fields={name: float for name in (*FOOD_FIELDS.values(), *FOOD_FIELDS_NONZERO.values())},
    last_wins=True,
)
EXERCISE = Measurement(
    database="BodyStats", name="exercise", tags=("category",),
    fields={"activity": str, "energy_kcal": float, "duration_s": int},
)
MEASUREMENT_OF = {"weight": BODY_COMPOSITION, "food": NUTRITION, "exercise": EXERCISE}


class MyFitnessPalSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")


class Table(NamedTuple):
    """One CSV of the export: the range its file name gives, and its rows."""
    named: Optional[Span]
    rows: List[Dict[str, str]]


def _num(value: Optional[str]) -> Optional[float]:
    """A number of the export, or None for an empty cell (the script's num)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConnectorError(f"unreadable number {value!r} in the export.") from None


def _day(day: str) -> date:
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise ConnectorError(f"unreadable date {day!r} in the export.") from None


def noon(day: str, tz: ZoneInfo, offset_s: int = 0) -> datetime:
    """12:00 local on the export's date, as the script stamped every record: a
    day lands on the right date whether a query groups days in UTC or in Paris.
    `offset_s`: the seconds a second workout of the same day is shifted by."""
    return datetime.combine(_day(day), NOON).replace(tzinfo=tz) + timedelta(seconds=offset_s)


def _named_span(name: str) -> Optional[Span]:
    """The range a member's file name gives: exactly two dates in its base
    name. A folder's name is not read: a date there says nothing of the
    range, and a span too wide would drop days the export doesn't hold."""
    days = NAMED_DAY.findall(PurePosixPath(name).name)
    return Span(_day(days[0]), _day(days[1])) if len(days) == 2 else None


def read_export(path: Path) -> Dict[str, Table]:
    """{kind: Table} for the export's three CSVs, recognised by a header
    column, each with the columns the import reads."""
    tables: Dict[str, Table] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist()
                     if name.lower().endswith(".csv") and not name.startswith("__MACOSX/")]
            for name in names:
                reader = csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig"), newline=""))
                columns = reader.fieldnames or []
                kind = next((kind for column, kind in KIND_BY_COLUMN.items() if column in columns), None)
                if kind is None:
                    continue
                if kind in tables:
                    # Two exports in one zip: their ranges would be replaced
                    # with one of them only.
                    raise ConnectorError(f"the zip holds more than one {kind} CSV: import one export at a time.")
                missing = [column for column in COLUMNS[kind] if column not in columns]
                if missing:
                    raise ConnectorError(f"the {kind} CSV has no column {', '.join(map(repr, missing))}: "
                                         "has the app changed its export?")
                # A row with more columns than the header (a stray comma) is
                # read for the ones it has, as the script read it.
                tables[kind] = Table(_named_span(name), [{k: (v or "") for k, v in row.items() if k is not None}
                                                         for row in reader])
    except (OSError, ValueError, KeyError, csv.Error, zipfile.BadZipFile) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None
    missing = set(KIND_BY_COLUMN.values()) - set(tables)
    if missing:
        raise ConnectorError(f"not a MyFitnessPal export: no {', '.join(sorted(missing))} CSV "
                             f"(the zip holds {len(names)} .csv file(s)).")
    return tables


def table_span(table: Table) -> Optional[Span]:
    """The days one CSV covers: the range its name gives, widened to its own
    records' dates (a name without one: the records' dates alone). None for
    a CSV with neither: its series is left as it is."""
    days = sorted({_day(row["Date"]) for row in table.rows if row.get("Date")}
                  | (set(table.named) if table.named else set()))
    return Span(days[0], days[-1]) if days else None


def local_date(raw: str, tz: ZoneInfo) -> str:
    """An InfluxDB RFC3339 time, as the local date the script compared on."""
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise ConnectorError(f"unreadable time {raw!r} in InfluxDB.") from None
    return moment.astimezone(tz).date().isoformat()


def other_weigh_ins(user: str, span: Span, tz: ZoneInfo) -> Dict[str, List[float]]:
    """{local date: [weight_kg, ...]} from every source except this one, for
    this `user`, over the days of `span`: what the scales already reported,
    the script's dedup read."""
    if not influxdb.is_configured():
        raise ConnectorError("the weigh-in dedup reads the other sources' readings in InfluxDB, even for a "
                             "dry run: set TRIBUTARY_INFLUXDB_URL.")
    start, end = span.bounds(tz)
    statement = (f'SELECT "weight_kg" FROM "body_composition" '
                 f'WHERE "user" = {influxdb.quote(user)} AND "source" != {influxdb.quote(SOURCE)} '
                 f'AND time >= {influxdb.time_literal(start)} AND time < {influxdb.time_literal(end)}')
    try:
        payload = influxdb.query(statement, BODY_COMPOSITION.database)
    except influxdb.InfluxError as exc:
        if "database not found" in str(exc):
            return {}  # a fresh InfluxDB: no other source has a reading yet
        raise ConnectorError(str(exc)) from None
    days: Dict[str, List[float]] = collections.defaultdict(list)
    for result in payload.get("results") or []:
        for series in result.get("series") or []:
            columns = series.get("columns") or []
            if "weight_kg" not in columns:
                continue
            weight_at = columns.index("weight_kg")
            for row in series.get("values") or []:
                weight = row[weight_at] if weight_at < len(row) else None
                if row and row[0] is not None and weight is not None:
                    days[local_date(row[0], tz)].append(float(weight))
    return days


def weight_points(rows: List[Dict[str, str]], other_days: Dict[str, List[float]], tz: ZoneInfo) -> List[Point]:
    """The app's own weigh-ins: those no other source already has that day.
    Two on one day share a key: the later one's fields win, as the script's
    consecutive writes did (body_composition is shared, not last_wins)."""
    by_day: Dict[str, Dict[str, float]] = {}
    for row in rows:
        weight = _num(row.get("Poids"))
        if weight is None:
            continue
        if any(abs(weight - known) <= WEIGHT_TOLERANCE_KG for known in other_days.get(row["Date"], ())):
            continue
        fields = {"weight_kg": weight}
        fat = _num(row.get("Body Fat %"))
        if fat is not None:
            fields["body_fat_pct"] = fat
        by_day.setdefault(row["Date"], {}).update(fields)
    return [Point(BODY_COMPOSITION, noon(day, tz), {}, fields) for day, fields in by_day.items()]


def food_points(rows: List[Dict[str, str]], tz: ZoneInfo) -> List[Point]:
    """One point per meal, the macros always and a secondary nutrient only when
    the entry reported one (its 0 means "not reported")."""
    points: List[Point] = []
    for row in rows:
        fields = {field: (_num(row.get(column)) or 0.0) for column, field in FOOD_FIELDS.items()}
        for column, field in FOOD_FIELDS_NONZERO.items():
            value = _num(row.get(column))
            if value:
                fields[field] = value
        meal = MEALS.get(row.get("Repas"))
        if meal is None:
            raise ConnectorError(f"unknown meal {row.get('Repas')!r} on {row.get('Date')}.")
        points.append(Point(NUTRITION, noon(row["Date"], tz), {"meal": meal}, fields))
    return points


def category(activity: str) -> str:
    """The contract's category for the app's activity name."""
    if activity.endswith("calorie adjustment"):
        return "adjustment"
    low = activity.lower()
    return next((cat for prefix, cat in CATEGORIES if low.startswith(prefix)), "other")


def exercise_points(rows: List[Dict[str, str]], tz: ZoneInfo) -> List[Point]:
    """One point per workout, the app's duplicates dropped, as the script did:
    rows identical in date, activity, calories and minutes (a linked app that
    pushed the same run again, up to 11 times a day), and same-day workouts
    with the same calories (two linked apps each pushing every workout) --
    the shortest kept, whose duration fits the calories.
    A calorie adjustment is not a workout and keeps no duration."""
    entries: List[Tuple[str, str, str, float, Optional[float]]] = []
    seen = set()
    for row in rows:
        # Names holding a comma arrive wrapped in a second pair of quotes.
        activity = " ".join((row.get("Exercices") or "").strip('"').replace("\xa0", " ").split())
        kcal = _num(row.get("Calories liées aux exercices")) or 0.0
        minutes = _num(row.get("Minutes d'exercice"))
        key = (row["Date"], activity, kcal, minutes)
        if key in seen:
            continue
        seen.add(key)
        entries.append((row["Date"], activity, category(activity), kcal, minutes))

    keep: Dict[object, int] = {}
    for i, (day, _activity, cat, kcal, minutes) in enumerate(entries):
        if cat == "adjustment" or kcal <= 0:
            keep[i] = i
            continue
        rank = (minutes or 0, cat == "aerobic")
        best = keep.get((day, kcal))
        if best is None or rank < (entries[best][4] or 0, entries[best][2] == "aerobic"):
            keep[(day, kcal)] = i

    points: List[Point] = []
    index: collections.Counter = collections.Counter()
    for i in sorted(keep.values()):
        day, activity, cat, kcal, minutes = entries[i]
        fields = {"activity": activity, "energy_kcal": kcal}
        # Adjustment rows always say 1 minute: a placeholder, not a duration.
        if cat != "adjustment" and minutes:
            fields["duration_s"] = int(minutes * 60)
        points.append(Point(EXERCISE, noon(day, tz, index[day]), {"category": cat}, fields))
        index[day] += 1
    return points


class MyFitnessPalConnector(Connector):
    key = SOURCE
    label = "MyFitnessPal"
    description = ("The weigh-ins, food and exercise of a MyFitnessPal export, imported in the UI: "
                   "the days it covers, per day (a scale already has the weigh-ins the app synced).")
    icon = "bi-activity"
    category = "Nutrition & fitness"
    settings_class = MyFitnessPalSettings
    measurements = (BODY_COMPOSITION, NUTRITION, EXERCISE)
    file_types = (".zip",)
    # The export is the app's history over the days it covers: importing it
    # replaces each series over its file's days (FileImport.spans), as the
    # script's own run did with --replace over the whole history. A record
    # the export no longer has is one the app no longer has either.
    rebuild_own_source = True

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: each export covers its own date range, so a folder of
        # them is not the whole history a watched folder's rebuild assumes.
        return None

    def import_file(self, path: Path) -> FileImport:
        tables = read_export(path)
        user = (app_settings.influxdb_user or "").strip()
        if not user:
            # The dedup reads the other sources' readings for this `user`; the
            # weigh-ins it must skip are the ones the scales already reported.
            # With no user tag there is nothing to tell apart, and every synced
            # weigh-in would be imported a second time.
            raise ConnectorError(
                "TRIBUTARY_INFLUXDB_USER is not set: the weigh-in dedup reads the other sources' "
                "readings for this user, so without it the weigh-ins the scales already reported would "
                "be imported again. Set it and import again.")
        tz = ZoneInfo(app_settings.tz)
        spans = {MEASUREMENT_OF[kind].name: span for kind, table in tables.items()
                 if (span := table_span(table)) is not None}
        weighed = any(_num(row.get("Poids")) is not None for row in tables["weight"].rows)
        other_days = other_weigh_ins(user, spans[BODY_COMPOSITION.name], tz) if weighed else {}
        points = (weight_points(tables["weight"].rows, other_days, tz)
                  + food_points(tables["food"].rows, tz)
                  + exercise_points(tables["exercise"].rows, tz))
        if not points:
            raise ConnectorError("This export holds nothing to import: nothing to write back, "
                                 "so the series is left as it is.")
        return FileImport(points, spans)
