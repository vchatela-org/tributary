"""One run of a connector, recorded as a SyncRun whatever happens: read what
is new (the API since the cursor, the watched folder's new files, or an
uploaded file), turn it into line protocol (app/points.py), then write it
(app/influxdb.py) -- or, for a dry run, keep the lines to show instead.

The cursor only moves after a run that wrote everything: a failed run, or
one InfluxDB refused points of (a field type conflict), is read again by
the next run. Rewriting is harmless: a point with the same key overwrites
itself with the same values. A connector made of independent parts (several
accounts) may report some as failed (SyncResult.errors): the others are
written and the cursor saved -- the connector kept the failed parts' cursor
as it was -- and the run shows as failed.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app import influxdb
from app.config import settings
from app.connectors.base import Connector, ConnectorError
from app.influxdb import InfluxError
from app.models import Cursor, SyncRun, utcnow
from app.points import Point, encode

logger = logging.getLogger(__name__)

PREVIEW_MAX_LINES = 2000


def load_cursor(db: Session, key: str) -> Dict[str, Any]:
    row = db.get(Cursor, key)
    if row is None or not row.value_json:
        return {}
    try:
        value = json.loads(row.value_json)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def save_cursor(db: Session, key: str, value: Dict[str, Any]) -> None:
    row = db.get(Cursor, key) or Cursor(connector=key)
    row.value_json = json.dumps(value, sort_keys=True)
    db.add(row)


# ────────────────────────────────────────────────
# Reading
# ────────────────────────────────────────────────

def _folder_files(connector: Connector) -> Dict[str, Tuple[int, int]]:
    """The watched folder's files a connector accepts: name -> (size, mtime)."""
    folder = connector.watch_dir
    if not folder.is_dir():
        raise ConnectorError(f"Watched folder not found: {folder}.")
    files = {}
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.name.lower().endswith(connector.file_types):
            stat = path.stat()
            files[path.name] = (stat.st_size, stat.st_mtime_ns)
    return files


def _import(connector: Connector, path: Path, label: str) -> List[Point]:
    try:
        return list(connector.import_file(path))
    except ConnectorError as exc:
        raise ConnectorError(f"{label}: {exc}") from None
    except (OSError, ValueError, KeyError) as exc:
        raise ConnectorError(f"{label}: unreadable ({type(exc).__name__}: {exc}).") from None


def collect(connector: Connector, state: Dict[str, Any], full: bool = False, file: Optional[str] = None,
            filename: Optional[str] = None
            ) -> Tuple[List[Point], List[str], Optional[Dict[str, Any]], bool, List[str]]:
    """(points, summary, new cursor state -- None to keep it as is --,
    whether to drop the connector's own series before writing, the parts of
    the source that failed)."""
    if file:
        label = filename or Path(file).name
        points = _import(connector, Path(file), label)
        return points, [f"Imported {label}"], None, False, []

    if not connector.is_configured():
        raise ConnectorError(f"Not configured: set {', '.join(connector.required_env) or 'its settings'}.")
    points: List[Point] = []
    summary: List[str] = []
    errors: List[str] = []
    new_state = dict(state)
    rebuild = False

    if connector.api_configured():
        result = connector.sync(connector.full_cursor(state.get("sync")) if full else state.get("sync"))
        points.extend(result.points)
        summary.extend(result.summary)
        errors.extend(result.errors)
        new_state["sync"] = result.cursor

    if connector.watch_dir is not None:
        files = _folder_files(connector)
        known = {} if full else {name: tuple(sig) for name, sig in (state.get("files") or {}).items()}
        changed = [name for name, sig in files.items() if known.get(name) != sig]
        if connector.rebuild_own_source:
            # The folder is the source of truth: any change (a file added,
            # replaced or removed) rewrites the whole source from it.
            if changed or set(known) != set(files):
                rebuild = True
                changed = list(files)
                summary.append(f"{len(files)} file(s) re-read from the watched folder, own series rebuilt")
        elif changed:
            summary.append(f"{len(changed)} new or changed file(s) in the watched folder")
        for name in changed:
            points.extend(_import(connector, connector.watch_dir / name, name))
        new_state["files"] = {name: list(sig) for name, sig in files.items()}

    return points, summary, new_state, rebuild, errors


# ────────────────────────────────────────────────
# Writing
# ────────────────────────────────────────────────

def _write(connector: Connector, lines: Dict[str, List[str]], rebuild: bool) -> Tuple[Dict[str, int], int, Dict[str, int]]:
    """({database: points written}, points older than the retention,
    {reason: points refused}). Nothing to write, nothing to drop: no
    request at all."""
    if not lines and not rebuild:
        return {}, 0, {}
    databases = sorted({m.database for m in connector.measurements} | set(lines))
    if settings.influxdb_create_databases:
        influxdb.create_missing_databases(databases)
    if rebuild:
        for m in connector.measurements:
            influxdb.drop_source(m.database, m.name, connector.source)
    written: Dict[str, int] = {}
    expired = 0
    refused: Dict[str, int] = {}
    for database, db_lines in lines.items():
        for start in range(0, len(db_lines), influxdb.BATCH_LINES):
            batch = db_lines[start:start + influxdb.BATCH_LINES]
            dropped, reason = influxdb.write(database, batch)
            written[database] = written.get(database, 0) + len(batch) - dropped
            if reason:
                key = f"{database}: {reason}"
                refused[key] = refused.get(key, 0) + dropped
            else:
                expired += dropped
    return written, expired, refused


def _preview(lines: Dict[str, List[str]]) -> str:
    out: List[str] = []
    for database, db_lines in lines.items():
        out.append(f"# {database}")
        for line in db_lines:
            if len(out) >= PREVIEW_MAX_LINES:
                break
            out.append(line)
    total = sum(len(v) for v in lines.values())
    shown = sum(1 for line in out if not line.startswith("# "))
    if shown < total:
        out.append(f"# ... and {total - shown} more")
    return "\n".join(out)


def _by_database(counts: Dict[str, int]) -> str:
    return ", ".join(f"{database} {n}" for database, n in sorted(counts.items()))


def run(db: Session, connector: Connector, trigger: str = "schedule", dry_run: bool = False, full: bool = False,
        file: Optional[str] = None, filename: Optional[str] = None) -> SyncRun:
    """One run, recorded as a SyncRun whatever happens. `full`: ignore the
    cursor (everything the source has, rewritten); `file`: import that
    uploaded file only, deleted afterwards."""
    record = SyncRun(connector=connector.key, trigger=trigger, dry_run=dry_run, full=full, status="running")
    db.add(record)
    db.commit()
    try:
        if not dry_run and not influxdb.is_configured():
            raise ConnectorError("InfluxDB is not configured: set TRIBUTARY_INFLUXDB_URL (a dry run works without).")
        state = load_cursor(db, connector.key)
        points, summary, new_state, rebuild, errors = collect(connector, state, full, file, filename)
        lines = encode(points, connector.source, settings.influxdb_user, settings.tz)
        total = sum(len(v) for v in lines.values())
        what = (", ".join(summary) + ": ") if summary else ""
        failed = (" Failed: " + "; ".join(e.rstrip(". ") for e in errors) + ".") if errors else ""

        if dry_run:
            record.preview = _preview(lines)
            record.points = total
            record.stats_json = json.dumps({database: len(v) for database, v in lines.items()})
            if total or rebuild:
                record.message = (f"Dry run. {what}{total} point(s) would be written"
                                  + (f" ({_by_database({d: len(v) for d, v in lines.items()})})" if total else "")
                                  + (", after dropping its own series" if rebuild else "") + ".")
            else:
                record.message = "Dry run. Nothing new." if not summary else f"Dry run. {what}nothing to write."
            record.message += failed
            record.status = "error" if errors else "ok"
        else:
            written, expired, refused = _write(connector, lines, rebuild)
            record.points = sum(written.values())
            record.stats_json = json.dumps(written)
            if total or rebuild:
                record.message = f"{what}{record.points} point(s) written" + (
                    f" ({_by_database(written)})" if written else "") + "."
            else:
                record.message = "Nothing new." if not summary else f"{what}nothing to write."
            if expired:
                record.message += f" {expired} older than their database's retention, skipped."
            for reason, dropped in refused.items():
                record.message += f" {dropped} refused ({reason})."
            record.message += failed
            record.status = "error" if refused or errors else "ok"
            if not refused and new_state is not None:
                save_cursor(db, connector.key, new_state)
    except (ConnectorError, InfluxError) as exc:
        db.rollback()
        record.status, record.message = "error", str(exc)[:2000]
        logger.warning("%s run failed: %s", connector.label, exc)
    except Exception as exc:  # noqa: BLE001 -- recorded for the UI, never fatal to the scheduler
        logger.exception("%s run failed", connector.label)
        db.rollback()
        record.status, record.message = "error", f"Unexpected error: {type(exc).__name__}: {exc}"[:2000]
    finally:
        record.finished_at = utcnow()
        db.add(record)
        db.commit()
        if file:
            try:
                os.remove(file)
            except OSError:
                pass
    return record
