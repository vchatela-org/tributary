"""The JSON API. The same shapes the pages render, so a script can drive
everything the UI does: read the sources, run one, check an upload and import
what it checked, set a schedule, choose what the overview shows.
"""
import logging
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app import connectors, crud, preferences, scheduler, schedule
from app.__version__ import __version__
from app.config import settings
from app.database import get_db
from app.models import ConnectorSchedule, StagedUpload, utcnow
from app.routers.common import (connector_or_404, discard_staged, queue_promote, queue_run, queue_upload,
                                same_origin)
from app.schemas import (ConnectorOut, MeasurementOut, PreferencesIn, PresetOut, RunDetail, RunOut,
                         ScheduleIn, ScheduleOut, StagedOut)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health")
def health():
    """Liveness: answers as soon as the server listens, whatever the runs do."""
    return {"status": "ok", "version": __version__}


# ────────────────────────────────────────────────
# Reading
# ────────────────────────────────────────────────

def staged_out(db: Session, staged: Optional[StagedUpload]) -> Optional[StagedOut]:
    if staged is None:
        return None
    check = crud.get_run(db, staged.dry_run_id) if staged.dry_run_id else None
    return StagedOut(
        filename=staged.filename,
        created_at=staged.created_at,
        expires_at=staged.created_at + timedelta(hours=settings.staged_upload_ttl_hours),
        points=check.points if check is not None else None,
    )


def connector_out(db: Session, connector) -> ConnectorOut:
    last = crud.latest_runs(db, connector.key, limit=1)
    resolved = scheduler.resolved_for(db, connector)
    return ConnectorOut(
        key=connector.key, label=connector.label, description=connector.description,
        icon=connector.icon, category=connector.category,
        configured=connector.is_configured(),
        hidden=connector.key in preferences.hidden_connectors(db),
        schedule=resolved.label if resolved.scheduled else None,
        schedule_cron=resolved.cron,
        schedule_source=resolved.source,
        next_run=resolved.next_run,
        staged=staged_out(db, db.get(StagedUpload, connector.key)),
        accepts_files=connector.accepts_files, file_types=list(connector.file_types),
        required_env=list(connector.required_env),
        measurements=[MeasurementOut(database=m.database, name=m.name, tags=list(m.tag_keys),
                                     fields={k: t.__name__ for k, t in m.field_types.items()})
                      for m in connector.measurements],
        last_run=RunOut.model_validate(last[0]) if last else None,
    )


@router.get("/connectors", response_model=List[ConnectorOut])
def list_connectors(db: Session = Depends(get_db)):
    return [connector_out(db, c) for c in connectors.all_connectors()]


@router.get("/runs", response_model=List[RunOut])
def list_runs(connector: Optional[str] = None, limit: int = Query(20, ge=1, le=200),
              offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """The latest runs, most recent first."""
    return crud.latest_runs(db, connector, limit, offset)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: Session = Depends(get_db)):
    """One run; a dry run's `preview` is the line protocol it would have written."""
    run = crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return run


# ────────────────────────────────────────────────
# Schedules
# ────────────────────────────────────────────────

def schedule_out(db: Session, connector) -> ScheduleOut:
    resolved = scheduler.resolved_for(db, connector)
    default = connector.default_schedule
    return ScheduleOut(
        cron=resolved.cron, label=resolved.label, source=resolved.source,
        source_label=resolved.source_label, next_run=resolved.next_run,
        next_runs=[schedule.to_utc(moment) for moment in
                   schedule.next_runs(resolved.cron, settings.tz)] if resolved.cron else [],
        note=resolved.note,
        default_cron=default,
        default_label=schedule.describe(default) if default else "Manual only",
        presets=[PresetOut(**preset) for preset in schedule.presets_for(resolved.cron)],
        at=schedule.clock_of(resolved.cron),
        weekday=schedule.weekday_of(resolved.cron),
        custom=resolved.cron or (default or "0 3 * * *"),
    )


def _store_cron(db: Session, connector, cron: str) -> None:
    row = db.get(ConnectorSchedule, connector.key)
    if row is None:
        row = ConnectorSchedule(connector=connector.key)
    row.cron = cron
    row.updated_at = utcnow()
    db.add(row)
    db.commit()
    # The thread re-plans this connector: a new schedule takes effect now, not
    # after the previous deadline.
    scheduler.reschedule(connector.key)


def _cron_from(payload: ScheduleIn) -> str:
    """The expression to store. An empty string means *Manual only*."""
    if payload.preset is not None:
        if payload.preset == "manual":
            return ""
        try:
            return schedule.build_cron(payload.preset, payload.at, payload.weekday, payload.custom) or ""
        except schedule.CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if payload.cron is not None:
        try:
            return schedule.validate(payload.cron)
        except schedule.CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    raise HTTPException(status_code=400, detail="Give a cron expression or a preset.")


@router.get("/connectors/{key}/schedule", response_model=ScheduleOut)
def get_schedule(key: str, db: Session = Depends(get_db)):
    """When this connector runs, where the value comes from, and the next runs."""
    return schedule_out(db, connector_or_404(key))


@router.put("/connectors/{key}/schedule", response_model=ScheduleOut, dependencies=[Depends(same_origin)])
def put_schedule(key: str, payload: ScheduleIn, db: Session = Depends(get_db)):  # noqa: D401
    """Set the schedule: `{"cron": "0 3 * * *"}`, or a preset the server expands
    (`{"preset": "daily", "at": "03:10"}`), or `{"preset": "manual"}` for none at
    all. It overrides TRIBUTARY_<KEY>_SCHEDULE and the connector's default; GET
    still says which of them would apply otherwise."""
    connector = connector_or_404(key)
    if payload.reset:
        row = db.get(ConnectorSchedule, connector.key)
        if row is not None:
            db.delete(row)
            db.commit()
            scheduler.reschedule(connector.key)
        return schedule_out(db, connector)
    _store_cron(db, connector, _cron_from(payload))
    return schedule_out(db, connector)


@router.post("/connectors/{key}/import", status_code=202, dependencies=[Depends(same_origin)])
def import_checked(key: str):
    """Import the export that was checked, without uploading it again. 409 with
    the reason when the check no longer stands for the file or the settings."""
    connector = connector_or_404(key)
    dry_run_id = queue_promote(connector)
    return {"status": "queued", "connector": key, "promoted_from": dry_run_id}


@router.delete("/connectors/{key}/staged", dependencies=[Depends(same_origin)])
def discard_checked(key: str):
    """Drop a checked upload (the file goes with it)."""
    return {"discarded": discard_staged(connector_or_404(key))}


# ────────────────────────────────────────────────
# Running
# ────────────────────────────────────────────────

@router.post("/connectors/{key}/run", status_code=202, dependencies=[Depends(same_origin)])
def request_run(key: str, dry_run: bool = False, full: bool = False):
    """Queue a run now; it runs in the background, see GET /api/runs.
    `dry_run`: show the line protocol, write nothing; `full`: ignore the
    cursor (everything the source has, rewritten -- harmless, the same
    points overwrite themselves)."""
    connector = connector_or_404(key)
    queue_run(connector, dry_run=dry_run, full=full)
    return {"status": "queued", "connector": key, "dry_run": dry_run, "full": full}


@router.post("/connectors/{key}/upload", status_code=202, dependencies=[Depends(same_origin)])
def upload(key: str, file: UploadFile = File(...), dry_run: bool = False):
    """Import an export file into a file connector (queued; deleted after).
    `dry_run`: check it without writing -- the file is then kept, and
    POST /connectors/{key}/import applies exactly what the check showed."""
    connector = connector_or_404(key)
    filename = queue_upload(connector, file, dry_run=dry_run)
    return {"status": "queued", "connector": key, "file": filename, "dry_run": dry_run}


# ────────────────────────────────────────────────
# What the overview shows
# ────────────────────────────────────────────────

@router.get("/preferences")
def get_preferences(db: Session = Depends(get_db)):
    return {"hidden": sorted(preferences.hidden_connectors(db))}


@router.post("/preferences", dependencies=[Depends(same_origin)])
def set_preferences(payload: PreferencesIn, db: Session = Depends(get_db)):
    """Choose which connectors the overview shows. Display only: a hidden
    connector keeps running and keeps filling its series."""
    hidden = preferences.set_hidden_connectors(
        db, payload.hidden, [c.key for c in connectors.all_connectors()])
    db.commit()
    return {"hidden": sorted(hidden)}
