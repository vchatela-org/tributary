"""Medisafe (medication reminders): the doses of an uploaded export, into
BodyStats..medication.

Ported from a one-off import script, writing exactly its series (same
measurement, tags, fields, types and times), so an import rewrites that
history in place and never duplicates it (docs/porting.md). source=medisafe
had that script as its only writer.

Medisafe has no API: the app's dose history export (`Doses_<account>_000.csv`,
or a zip holding it) is imported in the UI (*Dry run* first). An import
replaces the series over the days the export covers (`rebuild_own_source` +
FileImport.spans: its first to its last dose, for source=medisafe and
TRIBUTARY_INFLUXDB_USER, as the script's --replace did) rather than adding to
it or overwriting point by point: a dose whose status changed (pending ->
taken) is another series, so the old point would stay. The days outside the
export are left as they are. An import that holds no dose fails rather than
leave the series empty.

A dose:
  medication  at the dose's scheduled time, which the export gives in UTC
              (every schedule moves by an hour on the DST changes: a 12:00
              Paris dose is 11:00 in winter, 10:00 in summer), tags med =
              the export's name, renamed or left out per
              TRIBUTARY_MEDISAFE_NAMES, and status = taken / skipped / missed
              / unlogged (the app's taken / dismissed / missed, and pending or
              snooze: a reminder nobody answered says nothing about the dose).
              Fields: taken (1 when the dose was taken), app_status (the
              app's own word), dose and unit when the export carries them,
              strength (the medication's, "10 mg"), delay_min for a taken dose
              (scheduled time to the moment it was recorded, in minutes;
              negative: early), and with TRIBUTARY_MEDISAFE_NOTES the dose's
              note.

Medisafe exports each medication under its *current* name, back to its first
dose; a dose change makes a new schedule under the same name, so two doses of
one name and status in the same second are written a second apart, as the
script wrote them. Of the export's columns, `group_id` (the schedule's id) and
`condition` are not written. The names are personal: their renaming lives in
the environment, never in this repository, and no name is ever logged.
"""
import csv
import io
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport, Span
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_MEDISAFE_"
SOURCE = "medisafe"
# The columns an import reads: a file without them is not a doses export.
COLUMNS = ("name", "Scheduled for", "Recorded on", "dose_status", "dosage", "unit", "med_strength", "notes")
# The app's status -> the contract's.
STATUS = {"taken": "taken", "dismissed": "skipped", "missed": "missed", "pending": "unlogged",
          "snooze": "unlogged"}

# The script's measurement, as it wrote it: `source` and `user` on every point
# (the core adds them; `user` is TRIBUTARY_INFLUXDB_USER, the script's one
# account). The same measurement as MyTherapy's, which writes neither
# `strength` nor `note`; the types are the same.
MEDICATION = Measurement(
    database="BodyStats", name="medication", tags=("med", "status"),
    fields={"taken": int, "app_status": str, "dose": float, "unit": str, "delay_min": int,
            "strength": str, "note": str},
)


class MedisafeSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # The export's name -> the name stored in the `med` tag, or null to leave
    # that medication out, JSON: {"<export name>": "<name>" | null}. A name
    # not in it is stored as the export spells it. Personal (real medication
    # names): it comes from the environment. Read at each import, so a
    # mistake fails the import, not the start.
    names: Optional[str] = None
    # Also write each dose's note (free text the user typed: off by default).
    notes: bool = False


def parse_names(raw: Optional[str]) -> Dict[str, Optional[str]]:
    """TRIBUTARY_MEDISAFE_NAMES: {"<export name>": "<name>" | null}, trimmed."""
    if raw is None or not raw.strip():
        return {}
    name = f"{PREFIX}NAMES"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConnectorError(f"{name} is not valid JSON ({getattr(exc, 'msg', exc)}).") from None
    if not isinstance(data, dict):
        raise ConnectorError(f'{name} must be a JSON object: {{"<export name>": "<name>" or null}}.')
    names: Dict[str, Optional[str]] = {}
    for old, new in data.items():
        if new is not None and (not isinstance(new, str) or not new.strip()):
            raise ConnectorError(f"{name}: each value is a name, or null to leave the medication out.")
        names[str(old).strip()] = new.strip() if new is not None else None
    return names


def utc_epoch(raw: str) -> int:
    """An export time ("2024-01-17 06:00:00", UTC though it doesn't say so),
    to the second."""
    try:
        moment = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        raise ConnectorError(f"unreadable time {raw!r} in the export.") from None
    return int(moment.replace(tzinfo=timezone.utc).timestamp() if moment.tzinfo is None else moment.timestamp())


def read_export(path: Path) -> List[Dict[str, str]]:
    """The rows of one export: the CSV itself, or the zip holding one
    `Doses_*.csv`."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [name for name in archive.namelist()
                           if PurePosixPath(name).name.startswith("Doses_") and name.lower().endswith(".csv")
                           and not name.startswith("__MACOSX/")]
                if len(members) != 1:
                    raise ConnectorError(f"expected one Doses_*.csv in the archive, found {len(members)}.")
                text = archive.read(members[0]).decode("utf-8-sig")
        else:
            text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ConnectorError(f"not a Medisafe doses export: no column {', '.join(map(repr, missing))}.")
        # A row with more columns than the header (a stray comma) is read for
        # the ones it has, as the script read it.
        return [{name: (value or "") for name, value in row.items() if name is not None} for row in reader]
    except ConnectorError:
        raise
    except (OSError, ValueError, KeyError, csv.Error, zipfile.BadZipFile) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None


def dose_points(rows: List[Dict[str, str]], names: Dict[str, Optional[str]], notes: bool) -> List[Point]:
    """The doses of an export, as the script turned them into points."""
    points: List[Point] = []
    seen = set()
    for row in rows:
        where = f"for the dose of {row['Scheduled for']!r}"
        export_name = row["name"].strip()
        med = names.get(export_name, export_name)
        if med is None:
            continue  # left out by TRIBUTARY_MEDISAFE_NAMES
        status = STATUS.get(row["dose_status"])
        if status is None:
            raise ConnectorError(f"unknown dose_status {row['dose_status']!r} {where}.")
        scheduled = ts = utc_epoch(row["Scheduled for"])
        # Two schedules of one name can share a slot (a dose change's last
        # dose and the next schedule's first), and a renamed medication can
        # meet another's: a second apart, as the script wrote them.
        while (med, status, ts) in seen:
            ts += 1
        seen.add((med, status, ts))

        fields: Dict[str, Any] = {"taken": int(status == "taken"), "app_status": row["dose_status"]}
        if row["dosage"]:
            try:
                fields["dose"] = float(row["dosage"])
            except ValueError:
                raise ConnectorError(f"unreadable dosage {row['dosage']!r} {where}.") from None
        texts = [("unit", "unit"), ("med_strength", "strength")] + ([("notes", "note")] if notes else [])
        for column, field in texts:
            if row[column].strip():
                fields[field] = row[column].strip()
        if status == "taken":
            # From the dose's own scheduled time, not the shifted one.
            fields["delay_min"] = (utc_epoch(row["Recorded on"]) - scheduled) // 60
        points.append(Point(MEDICATION, datetime.fromtimestamp(ts, timezone.utc), {"med": med, "status": status},
                            fields))
    return points


def export_span(rows: List[Dict[str, str]], points: List[Point], tz: ZoneInfo) -> Span:
    """The local days (TRIBUTARY_TZ) from the export's first dose to its last,
    the ones left out included: what it covers."""
    days: List[date] = [datetime.fromtimestamp(utc_epoch(row["Scheduled for"]), tz).date() for row in rows]
    days += [point.time.astimezone(tz).date() for point in points]
    return Span(min(days), max(days))


class MedisafeConnector(Connector):
    key = SOURCE
    label = "Medisafe"
    description = ("The scheduled doses (taken, missed, skipped, unlogged) of a Medisafe export, imported in "
                   "the UI: the days it covers, renamed per TRIBUTARY_MEDISAFE_NAMES.")
    icon = "bi-capsule-pill"
    settings_class = MedisafeSettings
    measurements = (MEDICATION,)
    file_types = (".zip", ".csv")
    # An export replaces the series over the days it covers (FileImport.spans),
    # as the script's --replace did: a dose whose status changed is another
    # series, and the old point would stay.
    rebuild_own_source = True

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: an import replaces the days its export covers, which a
        # watched folder's whole-source rebuild doesn't do.
        return None

    def import_file(self, path: Path) -> FileImport:
        rows = read_export(path)
        points = dose_points(rows, parse_names(self.settings.names), self.settings.notes)
        if not points:
            raise ConnectorError("This export holds no dose to import: nothing to write back, "
                                 "so the series is left as it is.")
        return FileImport(points, {MEDICATION.name: export_span(rows, points, ZoneInfo(app_settings.tz))})
