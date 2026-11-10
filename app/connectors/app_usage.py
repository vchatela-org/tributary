"""App Usage (Android screen time, by a0soft): the phone's usage from the app's
backup, into DigitalStats..screen_time, ..app_usage, ..app_session and ..unlock.

App Usage has no API. Its backup (made by hand or daily by the app,
`AUM_V4_<date>_<time>.zip`) is a zip of tab-separated, quoted CSVs, the
app's own tables:
  version.csv   "Created by App Usage ..." and `version=<n>`;
  pkgs.csv      one row per package: `pid` (the backup's own id, which the
                other tables use), pkg_name, app_name, category and
                user_category (a `cid` of categories.csv, -1 for none);
  categories.csv  cid -> name (the app's: GAME, SOCIAL..., OTHERS for -1);
  usage.csv     one row per app and day: usage_time (ms in the foreground),
                access_count (launches), noty_count (notifications, 0 unless
                the app's notification tracking is on);
  checky.csv    one row per day: ccount (unlocks), ctime (ms the screen was on);
  act.csv       the event log: `time` (epoch ms), `type`, `pid`, `duration`
                (ms), `type_flag`. Type 7 is an app in the foreground (pid,
                from `time` for `duration`); type 5 the screen turned on, an
                unlock when its flag has UNLOCKED_FLAG (otherwise the screen
                woke and went off again locked: the time glanced at); type 6
                the screen off until then; the others are the app's own
                markers;
and battery.csv, installs.csv, the icons, which are not read.

A day in usage.csv and checky.csv is `year << 9 | day of the year` (0-based:
`2026 << 9 | 272` is 2026-09-30), a local date; a day's row appears once the day
is over, in a later backup.

What is written:
  screen_time   one point per day, at local midnight (TRIBUTARY_TZ): unlocks
                and screen_on_s (checky.csv), app_s and launches (usage.csv
                summed over the apps), notifications.
  app_usage     one point per app and day, at local midnight: foreground_s,
                launches, notifications. Tags: `app` (the name the phone
                shows), `package`, `category` (the kind of use, Tributary's
                vocabulary: app_usage_categories.py says how it is chosen).
  app_session   one point per foreground session (act.csv, type 7), at its
                start, *local time*: duration_s and count (1; two sessions of
                an app starting in the same second add up). Same tags.
  unlock        one point per unlock (type 5 with UNLOCKED_FLAG), at its time,
                *local time*: count (1). They add up to checky.csv's count.
The launcher counts as an app (the home screen in the foreground): a dashboard
leaves its package out where it wants the time spent in apps only.

The backup is the app's history (as far back as its retention keeps it): an
import replaces each series over the days the backup covers
(`rebuild_own_source` + FileImport.spans), for source=app_usage and
TRIBUTARY_INFLUXDB_USER, so an app renamed or moved to another category does
not leave its old series behind; the days before the backup's first one are
left as they are. Uploading every backup or one a week gives the same series.
Upload only: a folder of daily backups, each the whole history so far, is not
what a watched folder's rebuild assumes.

Nothing is read but numbers, the apps' names and packages and the categories'
names: the backup holds no message, title or URL, and its places (place.csv,
the geofences of the app's "at home / at work" feature) are not read.
"""
import csv
import io
import json
import math
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.connectors import play_store
from app.connectors.app_usage_categories import CATEGORIES, categorize, decided_without_play
from app.connectors.play_store import category_of
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport, Span
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_APP_USAGE_"
SOURCE = "app_usage"

# The tables read, each with the columns it must have.
COLUMNS = {
    "pkgs.csv": ("pid", "pkg_name", "app_name", "category", "user_category"),
    "categories.csv": ("cid", "name"),
    "usage.csv": ("pid", "time", "usage_time", "access_count", "noty_count"),
    "checky.csv": ("time", "ccount", "ctime"),
    "act.csv": ("time", "type", "pid", "duration", "type_flag"),
}
# act.csv's event types read.
APP_FOREGROUND = "7"
SCREEN_ON = "5"
# The bit of a type-5 event's flag that says it was unlocked (0x600, or 0x500
# with another unlock method). 0x900 and 0xA00 are the screen woken (a
# notification, a glance at the time) and turned off again, locked.
UNLOCKED_FLAG = 0x400
# A member larger than this is not an App Usage table (act.csv is ~1 MB a month).
MAX_MEMBER_BYTES = 512 * 2**20

SCREEN_TIME = Measurement(
    database="DigitalStats", name="screen_time", tags=(),
    fields={"unlocks": int, "screen_on_s": int, "app_s": int, "launches": int, "notifications": int},
)
APP_USAGE = Measurement(
    database="DigitalStats", name="app_usage", tags=("app", "package", "category"),
    fields={"foreground_s": int, "launches": int, "notifications": int},
)
APP_SESSION = Measurement(
    database="DigitalStats", name="app_session", tags=("app", "package", "category"),
    fields={"duration_s": float, "count": int},
    local_time=True,
)
UNLOCK_EVENT = Measurement(
    database="DigitalStats", name="unlock", tags=(),
    fields={"count": int},
    local_time=True,
)


class App(NamedTuple):
    name: str
    package: str
    category: str

    @property
    def tags(self) -> Dict[str, str]:
        return {"app": self.name, "package": self.package, "category": self.category}


def day_of(key: str) -> date:
    """usage.csv / checky.csv's day: `year << 9 | day of the year`, 0-based."""
    value = int(key)
    year, offset = value >> 9, value & 0x1FF
    if not 1970 <= year <= 9999 or offset > 365:
        raise ValueError(f"not an App Usage day: {key!r}")
    day = date(year, 1, 1) + timedelta(days=offset)
    if day.year != year:
        raise ValueError(f"not an App Usage day: {key!r}")
    return day


def _ms(value: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"negative duration or count: {value!r}")
    return number


def read_backup(path: Path) -> Dict[str, List[Dict[str, str]]]:
    """{table name: rows} of the tables in COLUMNS, from the backup zip."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = {Path(info.filename).name: info for info in archive.infolist() if not info.is_dir()}
            if "version.csv" not in names or not all(name in names for name in COLUMNS):
                raise ConnectorError("this is not an App Usage backup (AUM_V4_<date>.zip, with usage.csv, "
                                     "checky.csv, act.csv and pkgs.csv).")
            tables = {}
            for name, columns in COLUMNS.items():
                info = names[name]
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ConnectorError(f"{name} is larger than {MAX_MEMBER_BYTES // 2**20} MB.")
                text = archive.read(info).decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text), delimiter="\t")
                missing = [c for c in columns if c not in (reader.fieldnames or ())]
                if missing:
                    raise ConnectorError(f"{name} has no {', '.join(missing)} column: a backup of another "
                                         "version of App Usage?")
                tables[name] = list(reader)
            return tables
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None


def parse_categories(value: Optional[str]) -> Dict[str, str]:
    """TRIBUTARY_APP_USAGE_CATEGORIES: {"<package>": "<category>"}, each one a
    word of CATEGORIES."""
    if not value or not value.strip():
        return {}
    try:
        mapping = json.loads(value)
    except ValueError:
        raise ConnectorError(f"{PREFIX}CATEGORIES is not valid JSON.") from None
    if not isinstance(mapping, dict):
        raise ConnectorError(f'{PREFIX}CATEGORIES must be a JSON object: {{"<package>": "<category>"}}.')
    out = {}
    for package, category in mapping.items():
        word = category.strip().lower() if isinstance(category, str) else None
        if word not in CATEGORIES:
            raise ConnectorError(f"{PREFIX}CATEGORIES: {category!r} is not a category, use one of "
                                 f"{', '.join(CATEGORIES)}.")
        out[str(package).strip()] = word
    return out


def _packages(tables: Dict[str, List[Dict[str, str]]]) -> Dict[str, Dict[str, str]]:
    """pid -> its pkgs.csv row, for the rows that name a package."""
    return {row["pid"]: row for row in tables["pkgs.csv"] if row["pkg_name"].strip()}


def _chosen_name(row: Dict[str, str], names: Dict[str, str]) -> Optional[str]:
    return names.get(row["user_category"]) if row["user_category"] not in ("", "-1") else None


def to_look_up(tables: Dict[str, List[Dict[str, str]]], mine: Dict[str, str]) -> Set[str]:
    """The packages Google Play is asked about: the ones the backup shows in use
    (a day's usage, a session) that no earlier rule decides."""
    names = {row["cid"]: row["name"] for row in tables["categories.csv"]}
    rows = _packages(tables)
    used = {row["pid"] for row in tables["usage.csv"]} | {
        row["pid"] for row in tables["act.csv"] if row["type"] == APP_FOREGROUND}
    return {rows[pid]["pkg_name"].strip() for pid in used if pid in rows
            and not decided_without_play(rows[pid]["pkg_name"].strip(), _chosen_name(rows[pid], names), mine)}


def read_apps(tables: Dict[str, List[Dict[str, str]]], mine: Dict[str, str],
              play: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, App]:
    """pid -> App, its category chosen by app_usage_categories.categorize.
    `play`: Google Play's category per package (its own words), when looked up."""
    names = {row["cid"]: row["name"] for row in tables["categories.csv"]}
    apps = {}
    for pid, row in _packages(tables).items():
        package = row["pkg_name"].strip()
        category = categorize(package, _chosen_name(row, names), names.get(row["category"]), mine,
                              category_of((play or {}).get(package)))
        apps[pid] = App(name=row["app_name"].strip() or package, package=package, category=category)
    return apps


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz)


def _at(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(math.floor(epoch_ms / 1000), timezone.utc)


def _span(days: Iterable[date]) -> Optional[Span]:
    days = sorted(set(days))
    return Span(days[0], days[-1]) if days else None


class AppUsageSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # {"<package>": "<category>"}, JSON: your own apps' categories, over the
    # built-in ones (app_usage_categories.py). Read at each import, so a
    # mistake fails the import, not the start.
    categories: Optional[str] = None
    # Ask Google Play for the category of the apps nothing else decides
    # (play_store.py): off by default, as it sends their package names to
    # play.google.com.
    play_lookup: bool = False


class AppUsageConnector(Connector):
    key = SOURCE
    label = "App Usage"
    description = ("Your Android phone's screen time from the App Usage app's backup, imported in the UI: "
                   "per day, per app, and each session and unlock.")
    icon = "bi-phone"
    category = "Productivity"
    settings_class = AppUsageSettings
    measurements = (SCREEN_TIME, APP_USAGE, APP_SESSION, UNLOCK_EVENT)
    file_types = (".zip",)
    # The backup is the app's history over the days it covers: an import
    # replaces each series over them (FileImport.spans).
    rebuild_own_source = True

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: each daily backup holds the whole history so far, so a
        # folder of them is not what a watched folder's rebuild assumes.
        return None

    def import_fingerprint(self) -> Dict[str, Any]:
        """The categories decide the `category` tag: changing them invalidates a
        checked upload rather than applying a diff the user never saw."""
        return {"categories": self.settings.categories, "play_lookup": self.settings.play_lookup}

    def import_file(self, path: Path) -> FileImport:
        from app.config import settings

        tz = ZoneInfo(settings.tz)
        mine = parse_categories(self.settings.categories)
        tables = read_backup(path)
        play = (play_store.lookup(to_look_up(tables, mine), settings.data_dir)
                if self.settings.play_lookup else None)
        apps = read_apps(tables, mine, play.genres if play else None)
        points: List[Point] = []
        unknown: Set[str] = set()

        # Per app and day; the day's totals summed over the apps.
        totals: Dict[date, Dict[str, int]] = {}
        usage_days: List[date] = []
        for row in tables["usage.csv"]:
            app = apps.get(row["pid"])
            if app is None:
                unknown.add(row["pid"])
                continue
            day = day_of(row["time"])
            foreground_ms, launches, notifications = (_ms(row[c]) for c in ("usage_time", "access_count",
                                                                             "noty_count"))
            usage_days.append(day)
            points.append(Point(APP_USAGE, _midnight(day, tz), app.tags,
                                {"foreground_s": foreground_ms // 1000, "launches": launches,
                                 "notifications": notifications}))
            total = totals.setdefault(day, {"app_ms": 0, "launches": 0, "notifications": 0})
            total["app_ms"] += foreground_ms
            total["launches"] += launches
            total["notifications"] += notifications

        checky = {day_of(row["time"]): row for row in tables["checky.csv"]}
        for day in sorted(set(totals) | set(checky)):
            fields: Dict[str, int] = {}
            if day in checky:
                fields["unlocks"] = _ms(checky[day]["ccount"])
                fields["screen_on_s"] = _ms(checky[day]["ctime"]) // 1000
            if day in totals:
                fields["app_s"] = totals[day]["app_ms"] // 1000
                fields["launches"] = totals[day]["launches"]
                fields["notifications"] = totals[day]["notifications"]
            points.append(Point(SCREEN_TIME, _midnight(day, tz), {}, fields))

        session_days: List[date] = []
        unlock_days: List[date] = []
        sessions = unlocks = 0
        for row in tables["act.csv"]:
            kind = row["type"]
            if kind not in (APP_FOREGROUND, SCREEN_ON):
                continue
            moment = _at(_ms(row["time"]))
            day = moment.astimezone(tz).date()
            if kind == SCREEN_ON:
                if not int(row["type_flag"] or 0) & UNLOCKED_FLAG:
                    continue
                unlocks += 1
                unlock_days.append(day)
                points.append(Point(UNLOCK_EVENT, moment, {}, {"count": 1}))
                continue
            app = apps.get(row["pid"])
            if app is None:
                unknown.add(row["pid"])
                continue
            duration = _ms(row["duration"])
            session_days.append(day)
            if not duration:
                continue
            sessions += 1
            points.append(Point(APP_SESSION, moment, app.tags,
                                {"duration_s": round(duration / 1000, 1), "count": 1}))

        if not points:
            raise ConnectorError("This backup holds nothing to import: nothing to write back, "
                                 "so the series is left as it is.")
        spans = {m.name: span for m, span in (
            (APP_USAGE, _span(usage_days)),
            (SCREEN_TIME, _span(list(totals) + list(checky))),
            (APP_SESSION, _span(session_days)),
            (UNLOCK_EVENT, _span(unlock_days)),
        ) if span is not None}
        days = len(set(totals) | set(checky))
        summary = [f"{days} day(s), {len(apps)} app(s) known, {sessions} session(s), "
                   f"{unlocks} unlock(s)"]
        if unknown:
            summary.append(f"{len(unknown)} package id(s) not in pkgs.csv left out")
        if play:
            summary.extend(play.summary)
        return FileImport(points, spans, summary)
