# Portions adapted from kryoseu/whoops (https://github.com/kryoseu/whoops),
# Apache License 2.0; modified. See NOTICE and LICENSES/Apache-2.0.txt.
"""The WHOOP data export (the app's More > App Settings > Data Export, a zip
of CSV files), read into plain rows.

Adapted from Whoops' export import (`src/whoop_export.py` and
`src/models/export.py`, from its fork's CSV import, offered upstream as
kryoseu/whoops#5): the same files, the same columns and converters (times in
the row's "Cycle timezone" to UTC, minutes to milliseconds, `true`/`false`),
the same checks (UTF-8, the columns present, the "re-saved by a spreadsheet"
hint, 50 MB per file) and the same rule for a key seen twice (the last row
wins). What changed: plain dicts instead of SQLAlchemy models, a file on disk
instead of an upload stream, a lone CSV recognised by its columns (an upload
loses its name), the journal's `Notes` column never read, and no cell value in
an error message.

It imports nothing of Tributary's. The export has no WHOOP ids: rows are keyed
by their start time.
"""
import csv
import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple

MAX_FILE_BYTES = 50 * 1024 * 1024

_TIMEZONE = re.compile(r"^UTC(?:(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})|Z)?$")


class ExportFormatError(ValueError):
    """The file is not a readable WHOOP data export. The message names files,
    lines and columns, never a cell's value."""


def parse_timezone(value: str) -> timezone:
    """The export's 'Cycle timezone' column, e.g. 'UTC+01:00' or 'UTCZ'."""
    match = _TIMEZONE.match(value.strip())
    if not match:
        raise ValueError("not a time zone like UTC+01:00")
    if not match["sign"]:
        return timezone.utc
    offset = timedelta(hours=int(match["hours"]), minutes=int(match["minutes"]))
    return timezone(-offset if match["sign"] == "-" else offset)


# Converters receive a non-empty CSV value and the row's timezone.
def _datetime(value: str, tz: timezone) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("not a date and time") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _float(value: str, _tz: timezone) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError("not a number") from None


def _int(value: str, _tz: timezone) -> int:
    return round(_float(value, _tz))


def _minutes_to_ms(value: str, _tz: timezone) -> int:
    return round(_float(value, _tz) * 60_000)


def _bool(value: str, _tz: timezone) -> bool:
    lowered = value.lower()
    if lowered not in ("true", "false"):
        raise ValueError("not true or false")
    return lowered == "true"


def _text(value: str, _tz: timezone) -> str:
    return value


class Table(NamedTuple):
    """One CSV of the export: its file name, its columns (attribute -> (CSV
    header, converter)), the attributes keying a row, and a header only it
    has (a lone CSV is recognised by it)."""
    name: str
    filename: str
    columns: Dict[str, Tuple[str, Callable]]
    key: Tuple[str, ...]
    marker: str

    def required_headers(self) -> List[str]:
        return ["Cycle timezone"] + [header for header, _ in self.columns.values()]


_CYCLE_COLUMNS = {
    "cycle_start": ("Cycle start time", _datetime),
    "cycle_end": ("Cycle end time", _datetime),
}

_SLEEP_COLUMNS = {
    "sleep_onset": ("Sleep onset", _datetime),
    "wake_onset": ("Wake onset", _datetime),
    "performance_perc": ("Sleep performance %", _int),
    "respiratory_rate": ("Respiratory rate (rpm)", _float),
    "asleep_ms": ("Asleep duration (min)", _minutes_to_ms),
    "total_in_bed_ms": ("In bed duration (min)", _minutes_to_ms),
    "total_light_sleep_ms": ("Light sleep duration (min)", _minutes_to_ms),
    "total_slow_wave_sleep_ms": ("Deep (SWS) duration (min)", _minutes_to_ms),
    "total_rem_sleep_ms": ("REM duration (min)", _minutes_to_ms),
    "total_awake_ms": ("Awake duration (min)", _minutes_to_ms),
    "sleep_need_ms": ("Sleep need (min)", _minutes_to_ms),
    "sleep_debt_ms": ("Sleep debt (min)", _minutes_to_ms),
    "efficiency_perc": ("Sleep efficiency %", _int),
    "consistency_perc": ("Sleep consistency %", _int),
}

CYCLES = Table(
    name="cycles", filename="physiological_cycles.csv", key=("cycle_start",), marker="Recovery score %",
    columns={
        **_CYCLE_COLUMNS,
        "recovery_score": ("Recovery score %", _int),
        "resting_heart_rate": ("Resting heart rate (bpm)", _int),
        "hrv_rmssd_ms": ("Heart rate variability (ms)", _float),
        "skin_temp_celsius": ("Skin temp (celsius)", _float),
        "spo2_perc": ("Blood oxygen %", _float),
        "strain": ("Day Strain", _float),
        "energy_kcal": ("Energy burned (cal)", _float),
        "max_heart_rate": ("Max HR (bpm)", _int),
        "avg_heart_rate": ("Average HR (bpm)", _int),
        **_SLEEP_COLUMNS,
    })

SLEEPS = Table(
    name="sleeps", filename="sleeps.csv", key=("sleep_onset",), marker="Nap",
    columns={**_CYCLE_COLUMNS, **_SLEEP_COLUMNS, "nap": ("Nap", _bool)})

WORKOUTS = Table(
    name="workouts", filename="workouts.csv", key=("workout_start",), marker="Workout start time",
    columns={
        **_CYCLE_COLUMNS,
        "workout_start": ("Workout start time", _datetime),
        "workout_end": ("Workout end time", _datetime),
        "duration_ms": ("Duration (min)", _minutes_to_ms),
        "sport_name": ("Activity name", _text),
        "strain": ("Activity Strain", _float),
        "energy_kcal": ("Energy burned (cal)", _float),
        "max_heart_rate": ("Max HR (bpm)", _int),
        "avg_heart_rate": ("Average HR (bpm)", _int),
        "zone_one_perc": ("HR Zone 1 %", _float),
        "zone_two_perc": ("HR Zone 2 %", _float),
        "zone_three_perc": ("HR Zone 3 %", _float),
        "zone_four_perc": ("HR Zone 4 %", _float),
        "zone_five_perc": ("HR Zone 5 %", _float),
        "gps_enabled": ("GPS enabled", _bool),
    })

# One Journal answer. WHOOP attaches the answers given on waking to the cycle
# that starts with that night's sleep, so an entry relates to the recovery of
# the same cycle. The `Notes` column (free text) is never read.
JOURNAL = Table(
    name="journal", filename="journal_entries.csv", key=("cycle_start", "question"), marker="Question text",
    columns={
        **_CYCLE_COLUMNS,
        "question": ("Question text", _text),
        "answered_yes": ("Answered yes", _bool),
    })

TABLES = (CYCLES, SLEEPS, WORKOUTS, JOURNAL)
BY_FILENAME = {table.filename: table for table in TABLES}


def from_csv_row(table: Table, row: Dict[str, Optional[str]]) -> Dict:
    """One row as {attribute: value} (None for an empty cell), plus `tz`, its
    time zone (Whoops' `ExportRow.from_csv_row`)."""
    try:
        tz = parse_timezone(row.get("Cycle timezone") or "")
    except ValueError as exc:
        raise ValueError(f"column 'Cycle timezone': {exc}") from None
    values: Dict = {}
    for attr, (header, convert) in table.columns.items():
        raw = (row.get(header) or "").strip()
        try:
            values[attr] = convert(raw, tz) if raw else None
        except ValueError as exc:
            raise ValueError(f"column {header!r}: {exc}") from None
    for attr in table.key:
        if values[attr] is None:
            raise ValueError(f"column {table.columns[attr][0]!r} is empty")
    values["tz"] = tz
    return values


def _no_cycle(table: Table, row: Dict[str, Optional[str]]) -> bool:
    """A journal answer WHOOP gave no cycle at all: no start and no time zone
    (some custom questions are answered outside a journal). There is no day to
    put it on, so it is counted and left out rather than failing the import."""
    return (table.name == "journal" and bool((row.get("Question text") or "").strip())
            and not (row.get("Cycle start time") or "").strip()
            and not (row.get("Cycle timezone") or "").strip())


def _parse_csv(table: Table, filename: str, data: bytes, unplaced: List[int]) -> Iterator[Dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ExportFormatError(f"{filename}: not a UTF-8 CSV file.") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = reader.fieldnames or []
    missing = [h for h in table.required_headers() if h not in headers]
    if missing:
        message = f"{filename}: no column {', '.join(map(repr, missing))}."
        if len(headers) == 1 and ";" in headers[0]:
            message += " The file looks re-saved by a spreadsheet app: upload the original from the WHOOP zip."
        raise ExportFormatError(message)
    for row in reader:
        if _no_cycle(table, row):
            unplaced[0] += 1  # a one-element counter: a generator cannot return one
            continue
        try:
            yield from_csv_row(table, row)
        except ValueError as exc:
            raise ExportFormatError(f"{filename} line {reader.line_num}: {exc}.") from None


def _table_of_csv(data: bytes) -> Optional[Table]:
    """The table a lone CSV is, by a column only it has."""
    try:
        first = data[:65536].decode("utf-8-sig", errors="replace").splitlines()[0]
    except IndexError:
        return None
    headers = next(csv.reader([first]), [])
    found = [table for table in TABLES if table.marker in headers]
    return found[0] if len(found) == 1 else None


def _files(path: Path) -> Iterator[Tuple[Table, str, bytes]]:
    """(table, name, content) of every export CSV in the upload: the zip's
    members, by name, or the lone CSV, by its columns."""
    if path.stat().st_size > MAX_FILE_BYTES and not zipfile.is_zipfile(path):
        raise ExportFormatError(f"the file is larger than {MAX_FILE_BYTES // 2**20} MB.")
    if not zipfile.is_zipfile(path):
        data = path.read_bytes()
        table = _table_of_csv(data)
        if table is None:
            raise ExportFormatError("not a WHOOP export file: upload the export zip, or one of its CSV files ("
                                    + ", ".join(BY_FILENAME) + ").")
        yield table, table.filename, data
        return
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                table = BY_FILENAME.get(posixpath.basename(info.filename))
                if table is None or info.filename.startswith("__MACOSX/"):
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    raise ExportFormatError(f"{table.filename} is larger than {MAX_FILE_BYTES // 2**20} MB.")
                yield table, table.filename, archive.read(info)
    except zipfile.BadZipFile as exc:
        raise ExportFormatError(f"unreadable zip ({exc}).") from None


def read_export(path: Path) -> Tuple[Dict[str, List[Dict]], int]:
    """({table name: rows}, the journal answers with no cycle) for each export
    CSV the upload holds. Rows sharing a key collapsed, the last one winning
    (Whoops' `read_export`). A journal answer with no cycle has no date to be
    placed on: it is not returned as a row, only counted."""
    records: Dict[str, Dict[tuple, Dict]] = {}
    unplaced = [0]
    for table, filename, content in _files(path):
        rows = records.setdefault(table.name, {})
        for row in _parse_csv(table, filename, content, unplaced):
            rows[tuple(row[attr] for attr in table.key)] = row
    if not records:
        raise ExportFormatError("no WHOOP export file in the zip: it should hold " + ", ".join(BY_FILENAME) + ".")
    return {name: list(rows.values()) for name, rows in records.items()}, unplaced[0]
