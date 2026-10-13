"""Medisafe (medication reminders): the doses of an uploaded export, into
BodyStats..medication.

Ported from a one-off import script, writing exactly its series (same
measurement, tags, fields, types and times), so an import rewrites that
history in place and never duplicates it (docs/porting.md). source=medisafe
had that script as its only writer.

Medisafe has no API: the app's dose history export (`Doses_<account>_000.csv`,
or a zip holding it, or its parts) is imported in the UI (*Dry run* first).
An import replaces the series over the days the export covers
(`rebuild_own_source` + FileImport.spans: its first to its last dose, up to
TRIBUTARY_MEDISAFE_END_DATE, for source=medisafe and TRIBUTARY_INFLUXDB_USER,
as the script's --replace did) rather than adding to it or overwriting point
by point: a dose whose status changed (pending -> taken) is another series,
so the old point would stay. The days outside the export are left as they
are. An import that holds no dose fails rather than leave the series empty.

A dose:
  medication  at the dose's scheduled time, which the export gives in UTC
              (every schedule moves by an hour on the DST changes: a 12:00
              Paris dose is 11:00 in winter, 10:00 in summer), tags med =
              the export's name, renamed or left out per
              TRIBUTARY_MEDISAFE_NAMES, and status = taken / skipped / missed
              / unlogged (the app's taken / dismissed / missed, and pending,
              snooze or deleted: a reminder nobody answered, or a dose the
              user removed in the app, says nothing about the dose -- so a
              re-import of a current export rewrites a dose an earlier one
              left as taken, which is intended).
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
the environment, never in this repository, and no name is ever logged; nor is
the account number of the export's file name (upload_label masks it).
"""
import csv
import io
import json
import re
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport, Span
from app.points import Point
from app.schema import MEDICATION

PREFIX = "TRIBUTARY_MEDISAFE_"
SOURCE = "medisafe"
# The columns an import reads: a file without them is not a doses export.
COLUMNS = ("name", "Scheduled for", "Recorded on", "dose_status", "dosage", "unit", "med_strength", "notes")
# The app's status -> the contract's.
STATUS = {"taken": "taken", "dismissed": "skipped", "missed": "missed",
          "pending": "unlogged", "snooze": "unlogged", "deleted": "unlogged"}

# The export's file name carries the account number.
ACCOUNT_IN_NAME = re.compile(r"^(Doses_)[^_]+(_)", re.IGNORECASE)

# The script's measurement (app/schema.py, shared with MyTherapy), as it wrote
# it: `source` and `user` on every point (the core adds them; `user` is
# TRIBUTARY_INFLUXDB_USER, the script's one account).


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
    # Doses scheduled after this date (YYYY-MM-DD) are not imported, and an
    # import replaces nothing after it: the day another app took the doses
    # over, whose own import holds the next ones. Unset or empty: every dose
    # the export holds. A string, read at each import, so an empty value
    # never stops the app starting.
    end_date: Optional[str] = None


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


def parse_end_date(raw: Optional[str]) -> Optional[date]:
    """TRIBUTARY_MEDISAFE_END_DATE: a date (YYYY-MM-DD), or None."""
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ConnectorError(f"{PREFIX}END_DATE must be a date (YYYY-MM-DD), not {raw!r}.") from None


def utc_epoch(raw: str) -> int:
    """An export time ("2024-01-17 06:00:00", UTC though it doesn't say so),
    to the second."""
    try:
        moment = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        raise ConnectorError(f"unreadable time {raw!r} in the export.") from None
    return int(moment.replace(tzinfo=timezone.utc).timestamp() if moment.tzinfo is None else moment.timestamp())


def _rows(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        raise ConnectorError(f"not a Medisafe doses export: no column {', '.join(map(repr, missing))}.")
    # A row with more columns than the header (a stray comma) is read for the
    # ones it has, as the script read it.
    return [{name: (value or "") for name, value in row.items() if name is not None} for row in reader]


def read_export(path: Path) -> List[Dict[str, str]]:
    """The rows of one export: the CSV itself, or the zip holding it -- or its
    parts (Doses_<account>_000.csv, _001...), read as one, in order."""
    try:
        if not zipfile.is_zipfile(path):
            return _rows(path.read_text(encoding="utf-8-sig"))
        with zipfile.ZipFile(path) as archive:
            parts = sorted(name for name in archive.namelist()
                           if PurePosixPath(name).name.startswith("Doses_") and name.lower().endswith(".csv"))
            if not parts:
                raise ConnectorError("no Doses_*.csv in the archive.")
            return [row for part in parts for row in _rows(archive.read(part).decode("utf-8-sig"))]
    except ConnectorError:
        raise
    except (OSError, ValueError, KeyError, csv.Error, zipfile.BadZipFile) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None


def dose_points(rows: List[Dict[str, str]], names: Dict[str, Optional[str]], notes: bool,
                end: Optional[int] = None) -> Tuple[List[Point], Optional[Tuple[int, int]]]:
    """The doses of an export, as the script turned them into points, and the
    first and last time it covers (its left-out doses included; None for
    none). `end`: an epoch from which doses are not imported."""
    points: List[Point] = []
    seen = set()
    first = last = None

    def cover(ts: int) -> None:
        nonlocal first, last
        first = ts if first is None else min(first, ts)
        last = ts if last is None else max(last, ts)

    for row in rows:
        where = f"for the dose of {row['Scheduled for']!r}"
        export_name = row["name"].strip()
        med = names.get(export_name, export_name)
        if med is None:
            # Left out by TRIBUTARY_MEDISAFE_NAMES, unread as the script did;
            # its day is still the export's.
            try:
                scheduled = utc_epoch(row["Scheduled for"])
            except ConnectorError:
                continue
            if end is None or scheduled < end:
                cover(scheduled)
            continue
        scheduled = ts = utc_epoch(row["Scheduled for"])
        if end is not None and scheduled >= end:
            continue
        status = STATUS.get(row["dose_status"])
        if status is None:
            raise ConnectorError(f"unknown dose_status {row['dose_status']!r} {where}.")
        # Two schedules of one name can share a slot (a dose change's last
        # dose and the next schedule's first), and a renamed medication can
        # meet another's: a second apart, as the script wrote them.
        while (med, status, ts) in seen:
            ts += 1
        seen.add((med, status, ts))
        cover(scheduled)
        cover(ts)

        fields: Dict[str, Any] = {"taken": int(status == "taken"), "app_status": row["dose_status"]}
        if row["dosage"].strip():
            try:
                fields["dose"] = float(row["dosage"])
            except ValueError:
                raise ConnectorError(f"unreadable dosage {row['dosage']!r} {where}.") from None
        texts = [("unit", "unit"), ("med_strength", "strength")] + ([("notes", "note")] if notes else [])
        for column, field in texts:
            if row[column].strip():
                fields[field] = row[column].strip()
        if status == "taken" and row["Recorded on"].strip():
            # From the dose's own scheduled time, not the shifted one. A taken
            # dose the export gives no record time for has no delay.
            fields["delay_min"] = (utc_epoch(row["Recorded on"]) - scheduled) // 60
        points.append(Point(MEDICATION, datetime.fromtimestamp(ts, timezone.utc), {"med": med, "status": status},
                            fields))
    return points, (first, last) if first is not None else None


class MedisafeConnector(Connector):
    key = SOURCE
    label = "Medisafe"
    description = ("The scheduled doses (taken, missed, skipped, unlogged) of a Medisafe export, imported in "
                   "the UI: the days it covers, renamed per TRIBUTARY_MEDISAFE_NAMES.")
    icon = "bi-capsule-pill"
    category = "Medication"
    settings_class = MedisafeSettings
    measurements = (MEDICATION,)
    file_types = (".zip", ".csv")
    # An export replaces the series over the days it covers (FileImport.spans),
    # as the script's --replace did: a dose whose status changed is another
    # series, and the old point would stay.
    rebuild_own_source = True

    def import_fingerprint(self) -> Dict[str, Any]:
        """Renaming a medication, writing the notes or moving the end date all
        change what an import writes: any of them invalidates a checked
        upload."""
        return {"names": self.settings.names, "notes": self.settings.notes,
                "end_date": self.settings.end_date}

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: an import replaces the days its export covers, which a
        # watched folder's whole-source rebuild doesn't do.
        return None

    def upload_label(self, filename: str) -> str:
        return ACCOUNT_IN_NAME.sub(r"\1<account>\2", filename)

    def import_file(self, path: Path) -> FileImport:
        tz = ZoneInfo(app_settings.tz)
        end_day = parse_end_date(self.settings.end_date)
        end = int(datetime.combine(end_day + timedelta(days=1), time(), tzinfo=tz).timestamp()) if end_day else None
        points, covered = dose_points(read_export(path), parse_names(self.settings.names), self.settings.notes, end)
        if not points:
            raise ConnectorError("This export holds no dose to import: nothing to write back, "
                                 "so the series is left as it is.")
        first, last = (datetime.fromtimestamp(ts, tz).date() for ts in covered)
        return FileImport(points, {MEDICATION.name: Span(first, last)})
