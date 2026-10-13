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
import hashlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import influxdb
from app.config import settings
from app.connectors.base import Connector, ConnectorError, FileImport, Span
from app.influxdb import InfluxError
from app.models import Cursor, StagedUpload, SyncRun, utcnow
from app.points import Point, encode

logger = logging.getLogger(__name__)

PREVIEW_MAX_LINES = 2000

# What a run drops of the connector's own series before writing: None for
# nothing, else {measurement name: the days to replace, None for all of it}.
Drop = Optional[Dict[str, Optional[Span]]]


# ────────────────────────────────────────────────
# A checked upload, kept for its import
#
# An upload is *checked* (a dry run) before it is imported, so the user can
# read what it would write. The check keeps its file, and records what it
# produced, so "Import for real" applies exactly that -- no second upload, and
# nothing applied that was never reviewed. Everything here is about that
# promise; the file is the user's data and lives at most
# TRIBUTARY_STAGED_UPLOAD_TTL_HOURS.
# ────────────────────────────────────────────────

class StaleCheck(ConnectorError):
    """A checked upload that no longer stands for the file or the settings: it is
    dropped rather than applied, and the user is asked to check the file again.
    Told apart from any other failure, which keeps the check so the import can be
    retried without uploading again."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def points_sha256(lines: Dict[str, List[str]]) -> str:
    """What a check produced, whole (the stored preview is capped, this is
    not): two runs with the same digest write the same points."""
    return _sha("\n".join(f"{database}\x00{line}" for database in sorted(lines) for line in lines[database]))


def config_sha256(connector: Connector) -> str:
    """The settings an import reads, apart from the file: the connector's own
    import_fingerprint, the time zone, and the source it tags. Not the `user`:
    that is compared on its own, so a change to it can be named in the
    refusal."""
    return _sha(json.dumps({"connector": connector.key, "tz": settings.tz,
                            "fingerprint": connector.import_fingerprint()},
                           sort_keys=True, default=str))


def _unlink(path: Any) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def key_of(connector) -> str:
    """A connector, or its key: the staging helpers take either."""
    return connector.key if hasattr(connector, "key") else str(connector)


def staged_upload(db: Session, connector) -> Optional[StagedUpload]:
    """The checked upload this connector is holding, if any."""
    return db.get(StagedUpload, key_of(connector))


def stage_upload(db: Session, connector: Connector, path: Path, filename: str,
                 lines: Dict[str, List[str]], dry_run_id: Optional[int]) -> StagedUpload:
    """Keep a checked upload: its file, and what the check produced."""
    row = db.get(StagedUpload, connector.key)
    previous = row.path if row is not None else None
    if row is None:
        row = StagedUpload(connector=connector.key)
    row.path, row.filename = str(path), filename
    row.file_sha256 = file_sha256(path)
    row.points_sha256 = points_sha256(lines)
    row.config_sha256 = config_sha256(connector)
    row.user = settings.influxdb_user
    row.dry_run_id = dry_run_id
    row.created_at = utcnow()
    db.add(row)
    if previous and previous != str(path):
        _unlink(previous)  # a second check replaces the first
    return row


def discard_staged(db: Session, connector: Connector) -> bool:
    """Drop the checked upload (the user asked, or it is out of date)."""
    row = db.get(StagedUpload, connector.key)
    if row is None:
        return False
    db.delete(row)
    _unlink(row.path)
    return True


def purge_staged(db: Session, ttl_hours: Optional[float] = None) -> int:
    """Drop the checked uploads past their TTL, and the ones whose file is gone
    (the data folder was rebuilt, a restart landed on another volume): an
    expired check must never be importable. Called at startup and after every
    upload or import."""
    ttl = settings.staged_upload_ttl_hours if ttl_hours is None else ttl_hours
    cutoff = utcnow() - timedelta(hours=max(0.0, ttl))
    dropped = 0
    for row in db.query(StagedUpload).all():
        if not Path(row.path).exists() or (row.created_at is not None and row.created_at < cutoff):
            db.delete(row)
            _unlink(row.path)
            dropped += 1
    return dropped


def verify_stage(db: Session, connector: Connector) -> StagedUpload:
    """The checked upload this connector may import, or StaleCheck saying why
    not. Read-only: the callers drop what no longer stands (verify_or_discard),
    so the button can never apply a diff the user did not see."""
    stage = db.get(StagedUpload, connector.key)
    if stage is None:
        raise StaleCheck("Nothing was checked yet: upload the export and check it first.")
    if stage.created_at is not None and utcnow() - stage.created_at > timedelta(
            hours=max(0.0, settings.staged_upload_ttl_hours)):
        raise StaleCheck("The checked file expired: upload it and check it again.")
    if not Path(stage.path).exists():
        raise StaleCheck("The checked file is gone (its lifetime ended, or the data folder changed): "
                         "upload it again.")
    if file_sha256(Path(stage.path)) != stage.file_sha256:
        raise StaleCheck("The file is not the one that was checked: check it again.")
    if config_sha256(connector) != stage.config_sha256:
        raise StaleCheck("This connector's settings changed since the file was checked "
                         "(the names map, the dates, or the time zone): check it again.")
    if (settings.influxdb_user or None) != (stage.user or None):
        raise StaleCheck("TRIBUTARY_INFLUXDB_USER changed since the file was checked: check it again.")
    return stage


def verify_or_discard(db: Session, connector: Connector) -> StagedUpload:
    """verify_stage, dropping a check that no longer stands -- its file and its
    row -- so it can never be imported later, and saying so to the caller."""
    try:
        return verify_stage(db, connector)
    except StaleCheck:
        discard_staged(db, connector)
        db.commit()
        raise


def _verify_points(connector: Connector, lines: Dict[str, List[str]], stage: StagedUpload) -> None:
    """Last gate, after re-reading the file: the import writes exactly what the
    check showed, or it does not run."""
    if points_sha256(lines) != stage.points_sha256:
        raise StaleCheck("The file no longer yields what was checked: the export, this connector's settings, "
                         "or the readings already in InfluxDB changed in between. Check it again.")


def _after_upload(db: Session, connector: Connector, file: Optional[str], dry_run: bool, ok: bool) -> None:
    """What becomes of an uploaded file, in one place:
      - a check that succeeded: kept, as this connector's staged upload (the
        TTL applies; an import or a discard removes it);
      - a check that failed: deleted -- there is nothing to import;
      - an import: the check it applied -- and only that one -- is consumed once
        it wrote; a failed import keeps it, so "Import for real" can be retried
        without uploading again. A direct import of another file leaves a check
        still waiting for its own file alone.
    """
    if not file:
        return
    row = db.get(StagedUpload, connector.key)
    if dry_run:
        if not ok:
            if row is not None and row.path == str(file):
                discard_staged(db, connector)
            _unlink(file)
        return
    consumed = ok and row is not None and row.path == str(file)
    if consumed:
        discard_staged(db, connector)
    if not consumed and (row is None or row.path != str(file)):
        _unlink(file)


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


def _import(connector: Connector, path: Path, label: str) -> FileImport:
    try:
        result = connector.import_file(path)
        return result if isinstance(result, FileImport) else FileImport(list(result))
    except ConnectorError as exc:
        raise ConnectorError(f"{label}: {exc}") from None
    except (OSError, ValueError, KeyError) as exc:
        raise ConnectorError(f"{label}: unreadable ({type(exc).__name__}: {exc}).") from None


def _whole(connector: Connector) -> Dict[str, Optional[Span]]:
    return {m.name: None for m in connector.measurements}


def _upload_drop(connector: Connector, imported: FileImport) -> Drop:
    """What an uploaded file replaces: nothing, the whole series, or the
    days it covers of each measurement it has a span for -- this user's: a
    span is replaced for TRIBUTARY_INFLUXDB_USER, so without one it would
    reach every user's points, and the rewritten ones would carry none."""
    if not connector.rebuild_own_source:
        return None
    if imported.spans is None:
        return _whole(connector)
    declared = {m.name: m for m in connector.measurements}
    unknown = set(imported.spans) - set(declared)
    if unknown:
        raise ValueError(f"span(s) for undeclared measurement(s) {sorted(unknown)}")
    if not settings.influxdb_user and any(declared[name].tagged_user for name in imported.spans):
        raise ConnectorError("TRIBUTARY_INFLUXDB_USER is not set: an import replaces the days it covers of "
                             "this user's points, and without one it would replace every user's. "
                             "Set it and import again.")
    return dict(imported.spans) or None


def collect(connector: Connector, state: Dict[str, Any], full: bool = False, file: Optional[str] = None,
            filename: Optional[str] = None
            ) -> Tuple[List[Point], List[str], Optional[Dict[str, Any]], Drop, List[str]]:
    """(points, summary, new cursor state -- None to keep it as is --, what
    of the connector's own series to drop before writing, the parts of the
    source that failed)."""
    if file:
        label = connector.upload_label(filename or Path(file).name)
        imported = _import(connector, Path(file), label)
        # `rebuild_own_source`: an uploaded file that holds a connector's
        # history (an export) replaces its series rather than adding to it or
        # overwriting point by point -- a dose the export no longer has is one
        # the source no longer knows. All of it, or only the days the file
        # covers (FileImport.spans: an export of a date range). The connector
        # raises before this when the file holds nothing to write back, so the
        # series is never left empty.
        return imported.points, [f"Imported {label}"], None, _upload_drop(connector, imported), []

    if not connector.is_configured():
        raise ConnectorError(f"Not configured: set {', '.join(connector.required_env) or 'its settings'}.")
    points: List[Point] = []
    summary: List[str] = []
    errors: List[str] = []
    new_state = dict(state)
    rebuild: Drop = None

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
                rebuild = _whole(connector)
                changed = list(files)
                summary.append(f"{len(files)} file(s) re-read from the watched folder, own series rebuilt")
        elif changed:
            summary.append(f"{len(changed)} new or changed file(s) in the watched folder")
        for name in changed:
            points.extend(_import(connector, connector.watch_dir / name, name).points)
        new_state["files"] = {name: list(sig) for name, sig in files.items()}

    return points, summary, new_state, rebuild, errors


# ────────────────────────────────────────────────
# Writing
# ────────────────────────────────────────────────

def _dropped(drop: Drop) -> str:
    """The run message's mention of the drop."""
    if drop is None:
        return ""
    spans = set(drop.values())
    if spans == {None}:
        return ", after dropping its own series"
    if len(spans) == 1:
        span, = spans
        return f", after dropping its own series from {span.first} to {span.last}"
    return ", after dropping its own series (" + "; ".join(
        f"{name} from {span.first} to {span.last}" if span else f"{name}: all of it"
        for name, span in drop.items()) + ")"


def _write(connector: Connector, lines: Dict[str, List[str]], rebuild: Drop
           ) -> Tuple[Dict[str, int], int, Dict[str, int]]:
    """({database: points written}, points older than the retention,
    {reason: points refused}). Nothing to write, nothing to drop: no
    request at all."""
    if not lines and rebuild is None:
        return {}, 0, {}
    databases = sorted({m.database for m in connector.measurements} | set(lines))
    if settings.influxdb_create_databases:
        influxdb.create_missing_databases(databases)
    for m in connector.measurements:
        if rebuild is None or m.name not in rebuild:
            continue
        span = rebuild[m.name]
        if span is None:
            influxdb.drop_source(m.database, m.name, connector.source)
        else:
            # The days the file covers, of this connector's points only: its
            # source and the `user` the core tags them with.
            influxdb.drop_source(m.database, m.name, connector.source, *span.bounds(ZoneInfo(settings.tz)),
                                 user=settings.influxdb_user if m.tagged_user else None)
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
        file: Optional[str] = None, filename: Optional[str] = None,
        promoted_from: Optional[int] = None) -> SyncRun:
    """One run, recorded as a SyncRun whatever happens. `full`: ignore the
    cursor (everything the source has, rewritten); `file`: import that uploaded
    file; `promoted_from`: import what this dry run checked, without uploading
    it again -- the run re-verifies the staged file before it writes, so what
    the user reviewed and what is written cannot drift apart."""
    record = SyncRun(connector=connector.key, trigger=trigger, dry_run=dry_run, full=full, status="running",
                     promoted_from=promoted_from)
    db.add(record)
    db.commit()
    stage = None
    try:
        if promoted_from is not None:
            # Re-verified here, not only when the button was pressed: a promote
            # may wait behind another run, and nothing may be applied that the
            # check no longer stands for.
            stage = verify_or_discard(db, connector)
            file, filename, trigger = stage.path, stage.filename, "upload"
            record.trigger = trigger
        if not dry_run and not influxdb.is_configured():
            raise ConnectorError("InfluxDB is not configured: set TRIBUTARY_INFLUXDB_URL (a dry run works without).")
        state = load_cursor(db, connector.key)
        points, summary, new_state, rebuild, errors = collect(connector, state, full, file, filename)
        lines = encode(points, connector.source, settings.influxdb_user, settings.tz)
        if stage is not None and not dry_run:
            _verify_points(connector, lines, stage)
        total = sum(len(v) for v in lines.values())
        what = (", ".join(summary) + ": ") if summary else ""
        failed = (" Failed: " + "; ".join(e.rstrip(". ") for e in errors) + ".") if errors else ""

        if dry_run:
            record.preview = _preview(lines)
            record.points = total
            record.stats_json = json.dumps({database: len(v) for database, v in lines.items()})
            if total or rebuild is not None:
                record.message = (f"Dry run. {what}{total} point(s) would be written"
                                  + (f" ({_by_database({d: len(v) for d, v in lines.items()})})" if total else "")
                                  + _dropped(rebuild) + ".")
            else:
                record.message = "Dry run. Nothing new." if not summary else f"Dry run. {what}nothing to write."
            record.message += failed
            record.status = "error" if errors else "ok"
            if file and record.status == "ok":
                # A check that worked keeps its file, so "Import for real" does
                # not ask for it again. The name stored is the masked one the
                # run shows (upload_label), never the account number a file may
                # carry.
                stage_upload(db, connector, Path(file),
                             connector.upload_label(filename or Path(file).name), lines, record.id)
        else:
            written, expired, refused = _write(connector, lines, rebuild)
            record.points = sum(written.values())
            record.stats_json = json.dumps(written)
            if total or rebuild is not None:
                record.message = f"{what}{record.points} point(s) written" + (
                    f" ({_by_database(written)})" if written else "") + _dropped(rebuild) + "."
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
    except StaleCheck as exc:
        # The check no longer stands: drop it, file included, rather than leave
        # something importable that was never reviewed.
        db.rollback()
        record.status, record.message = "error", str(exc)[:2000]
        discard_staged(db, connector)
        logger.warning("%s: the checked file is out of date: %s", connector.label, exc)
    except (ConnectorError, InfluxError) as exc:
        # Anything else (InfluxDB down, a bad export): the check is kept, so
        # "Import for real" can be retried without uploading again.
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
        # One place decides what becomes of the uploaded file (app/runner.py
        # _after_upload): a check keeps it for its import, an import consumes it.
        _after_upload(db, connector, file, dry_run, record.status != "error")
        # And any other check past its TTL goes here too: an upload or an import
        # is the moment the app is certainly writing, so an expired check never
        # waits for the next restart to be dropped.
        purge_staged(db)
        db.commit()
    return record


def promote(db: Session, connector: Connector) -> SyncRun:
    """Import what was checked, without uploading the file again. Raises
    ConnectorError (the UI shows it, nothing runs) when the check no longer
    stands for the file or the settings."""
    stage = verify_or_discard(db, connector)
    return run(db, connector, trigger="upload", promoted_from=stage.dry_run_id)
