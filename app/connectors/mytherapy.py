"""MyTherapy (medication reminders): the scheduled doses of a folder of
exports, into BodyStats..medication.

Ported from a daily import job, writing exactly its series (same
measurement, tags, fields, types and times), so a run rewrites that history
in place and never duplicates it (docs/porting.md). source=mytherapy had
that job as its only writer.

MyTherapy has no API: the app exports "MyTherapy Archive.csv" (or a zip
holding it), the user saves the exports to a folder (a NAS share, mounted
read-only), and the connector reads them. The folder is the source of truth:
any change in it (a file added, replaced or removed) drops the connector's
own series (DROP SERIES FROM medication WHERE "source" = 'mytherapy') and
rewrites it from every export in it -- `rebuild_own_source`, safe because
nothing else writes source=mytherapy. A run with no new export changes
nothing. A folder with no export **fails the run** instead of dropping the
series with nothing to write back, as the job did; so does an export holding
no dose.

The exports are applied oldest first, each replacing what the older ones said
over its own date range (their last scheduled dose), as the job did: a dose
that went from unanswered to taken, or one deleted in the app, follows the
newest export.

A record is a scheduled dose:
  medication  at the dose's scheduled time, local (TRIBUTARY_TZ: a 12:00
              dose stays 12:00 across the DST changes), tags med = the name
              TRIBUTARY_MYTHERAPY_NAMES gives its alias, status = taken /
              missed / skipped (the app's confirmed / auto skipped /
              rejected). Fields: taken (1 when the dose was taken),
              app_status (the app's own word), dose and unit when the export
              carries them ("pill(s)" -> "pills"), and delay_min for a taken
              dose (scheduled time to the moment it was answered, in
              minutes; negative: answered early).
Two doses of one medication and status scheduled in the same second are
written a second apart, as the job wrote them (it bumped the second one).

Of the export's columns, `note` is not written: the job wrote it, but no
export ever carried one, so its series never held one (docs/schema.md;
free-form text stays out of InfluxDB). Rows of type `measurement` (weight
reminders: a scale already records weight) are skipped, as the job skipped
them, and so are doses scheduled before TRIBUTARY_MYTHERAPY_START_DATE (the
job's overlap with Medisafe, which covers the same doses until then). An
alias missing from TRIBUTARY_MYTHERAPY_NAMES fails the run, listing the
aliases: add them and run again. So do an unknown type or status, and a file
that isn't an export (named in the message). Nothing is ever kept from an
export but the doses: no name, no note, no id.

The job ran at 07:30 Europe/Paris; a Tributary connector runs every
TRIBUTARY_MYTHERAPY_INTERVAL_MINUTES (a day by default) from its own start.
"""
import collections
import csv
import io
import json
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_MYTHERAPY_"
SOURCE = "mytherapy"
# The export's columns, in order: a file whose header differs is not a
# MyTherapy export.
COLUMNS = ("actual_date", "scheduled_date", "type", "name", "value", "unit", "status", "note")
# The app's status -> the contract's.
STATUS = {"confirmed": "taken", "rejected": "skipped", "auto skipped": "missed"}

# The job's measurement, as it wrote it: `source` and `user` on every point
# (the core adds them; `user` is TRIBUTARY_INFLUXDB_USER, the job's one
# account). `note` and `strength` exist in the database, written by Medisafe's
# import: this connector writes neither.
MEDICATION = Measurement(
    database="BodyStats", name="medication", tags=("med", "status"),
    fields={"taken": int, "app_status": str, "dose": float, "unit": str, "delay_min": int},
)


class MyTherapySettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # A dose carries its own time and every run rewrites the whole series:
    # once a day is plenty.
    interval_minutes: int = 1440
    # The exports' alias -> the name stored in the `med` tag, JSON:
    # {"<alias>": "<name>"}. The mapping is personal (real medication names
    # stay out of MyTherapy, and out of this repository): it comes from the
    # environment. Read at each run, so a mistake fails the runs, not the
    # start.
    names: Optional[str] = None
    # Doses scheduled before this date are not imported (YYYY-MM-DD): the
    # job's overlap with another import, which covered the same doses until
    # then. Unset or empty: every dose the exports hold. A string, read at
    # each run like `names`, so an empty value never stops the app starting.
    start_date: Optional[str] = None


def parse_names(raw: Optional[str]) -> Dict[str, str]:
    """TRIBUTARY_MYTHERAPY_NAMES: {"<alias>": "<name>"}, the alias as the app
    spells it, trimmed."""
    if raw is None or not raw.strip():
        return {}
    name = f"{PREFIX}NAMES"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConnectorError(f"{name} is not valid JSON ({getattr(exc, 'msg', exc)}).") from None
    if not isinstance(data, dict):
        raise ConnectorError(f'{name} must be a JSON object: {{"<alias>": "<name>"}}.')
    return {str(alias).strip(): str(med).strip() for alias, med in data.items() if str(alias).strip()}


def parse_start_date(raw: Optional[str]) -> Optional[date]:
    """TRIBUTARY_MYTHERAPY_START_DATE: a date (YYYY-MM-DD), or None."""
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ConnectorError(f"{PREFIX}START_DATE must be a date (YYYY-MM-DD), not {raw!r}.") from None


def local_epoch(raw: str, tz: ZoneInfo) -> int:
    """An export time, to the second: the exports are local ("2026-08-14
    12:00:00", with no offset to read, though one would be honoured), and a
    12:00 dose stays 12:00 across the DST changes, unlike Medisafe's UTC."""
    try:
        moment = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        raise ConnectorError(f"unreadable date {raw!r} in the export.") from None
    if moment.tzinfo is not None:
        return int(moment.timestamp())
    return int(moment.replace(tzinfo=tz).timestamp())


def read_export(path: Path) -> List[Dict[str, str]]:
    """The rows of one export: the CSV itself, or the zip holding it."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist()
                         if name.lower().endswith(".csv") and not name.startswith("__MACOSX/")]
                if len(names) != 1:
                    raise ConnectorError(f"expected one .csv in the archive, found {len(names)}.")
                text = archive.read(names[0]).decode("utf-8-sig")
        else:
            text = path.read_text(encoding="utf-8-sig")
    except ConnectorError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(COLUMNS):
        raise ConnectorError(f"not a MyTherapy export: columns {reader.fieldnames}.")
    rows = []
    for number, row in enumerate(reader, start=2):
        if None in row:  # more columns than the header: a broken row
            raise ConnectorError(f"row {number} has more columns than the header.")
        rows.append({name: (value or "") for name, value in row.items()})
    return rows


def merge(exports: List[Tuple[Path, List[Dict[str, str]]]], tz: ZoneInfo) -> List[Dict[str, str]]:
    """The exports applied oldest first (by their last scheduled dose), each
    replacing the rows inside its own date range."""
    ordered = []
    for path, rows in exports:
        if not rows:
            continue
        times = [local_epoch(row["scheduled_date"], tz) for row in rows]
        ordered.append((max(times), path.stat().st_mtime, str(path), min(times), rows))
    ordered.sort(key=lambda export: export[:4])
    merged: List[Dict[str, str]] = []
    for last, _mtime, _path, first, rows in ordered:
        merged = [row for row in merged if not first <= local_epoch(row["scheduled_date"], tz) <= last]
        merged += rows
    return merged


def dose_points(rows: List[Dict[str, str]], names: Dict[str, str], start: Optional[date], tz: ZoneInfo
                ) -> List[Point]:
    """The doses of the merged exports, as the job turned them into points."""
    points: List[Point] = []
    seen = set()
    unmapped: collections.Counter = collections.Counter()
    start_epoch = int(datetime.combine(start, time(), tzinfo=tz).timestamp()) if start else None
    for row in rows:
        where = f"for the dose of {row['scheduled_date']!r}"
        if row["type"] == "measurement":
            continue  # a weight reminder: a scale records weight
        if row["type"] != "drug":
            raise ConnectorError(f"unknown type {row['type']!r} {where}.")
        scheduled = ts = local_epoch(row["scheduled_date"], tz)
        if start_epoch is not None and ts < start_epoch:
            continue
        alias = row["name"].strip()
        med = names.get(alias)
        if not med:
            unmapped[alias] += 1
            continue
        status = STATUS.get(row["status"])
        if status is None:
            raise ConnectorError(f"unknown status {row['status']!r} {where}.")
        # Two doses of one medication and status in the same second are written
        # a second apart, as the job wrote them.
        while (med, status, ts) in seen:
            ts += 1
        seen.add((med, status, ts))

        fields: Dict[str, Any] = {"taken": int(status == "taken"), "app_status": row["status"]}
        if row["value"].strip():
            try:
                fields["dose"] = float(row["value"])
            except ValueError:
                raise ConnectorError(f"unreadable dose {row['value']!r} {where}.") from None
        if row["unit"].strip():
            fields["unit"] = row["unit"].strip().replace("(s)", "s")
        if status == "taken":
            # From the dose's own scheduled time, not the bumped one: the job
            # computed the delay before deciding where to write the point.
            fields["delay_min"] = (local_epoch(row["actual_date"], tz) - scheduled) // 60
        points.append(Point(MEDICATION, datetime.fromtimestamp(ts, tz), {"med": med, "status": status}, fields))

    if unmapped:
        raise ConnectorError(
            f"aliases missing from {PREFIX}NAMES, add them and run again: "
            + ", ".join(f"{alias!r} ({count} dose(s))" for alias, count in sorted(unmapped.items())))
    return points


class MyTherapyConnector(Connector):
    key = SOURCE
    label = "MyTherapy"
    description = ("Scheduled doses (taken, missed, skipped) of the MyTherapy exports in a folder, "
                   "rebuilt from it at each run.")
    icon = "bi-capsule"
    settings_class = MyTherapySettings
    measurements = (MEDICATION,)
    required_env = (f"{PREFIX}WATCH_DIR", f"{PREFIX}NAMES")
    file_types = (".zip", ".csv")
    # The folder is the source of truth: a single upload can't be merged with
    # the exports around it, and rebuilding the series from it alone would
    # drop the rest.
    accepts_uploads = False
    rebuild_own_source = True

    def import_files(self, paths: Iterable[Path]) -> Iterable[Point]:
        tz = ZoneInfo(app_settings.tz)
        if not paths:
            raise ConnectorError("The watched folder holds no MyTherapy export (*.zip, *.csv): "
                                 "nothing to write back, so its series is left as it is.")
        exports = []
        for path in sorted(paths):
            try:
                rows = read_export(path)
            except ConnectorError as exc:
                raise ConnectorError(f"{path.name}: {exc}") from None
            if not rows:
                continue  # an empty export: nothing of it
            exports.append((path, rows))
        if not exports:
            raise ConnectorError("The watched folder's exports are empty: nothing to write back, "
                                 "so the series is left as it is.")
        settings = self.settings
        points = dose_points(merge(exports, tz), parse_names(settings.names),
                              parse_start_date(settings.start_date), tz)
        if not points:
            raise ConnectorError("The watched folder's exports hold no dose to import: nothing to write "
                                 "back, so the series is left as it is.")
        return points
