from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app import connectors, crud
from app.__version__ import __version__
from app.database import get_db
from app.routers.common import connector_or_404, queue_run, queue_upload, same_origin
from app.schemas import ConnectorOut, MeasurementOut, RunDetail, RunOut

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health")
def health():
    """Liveness: answers as soon as the server listens, whatever the runs do."""
    return {"status": "ok", "version": __version__}


def connector_out(db: Session, connector) -> ConnectorOut:
    last = crud.latest_runs(db, connector.key, limit=1)
    return ConnectorOut(
        key=connector.key, label=connector.label, description=connector.description,
        configured=connector.is_configured(),
        schedule=connector.schedule_label() if connector.is_configured() else None,
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
    """Import an export file into a file connector (queued; deleted after)."""
    connector = connector_or_404(key)
    filename = queue_upload(connector, file, dry_run=dry_run)
    return {"status": "queued", "connector": key, "file": filename, "dry_run": dry_run}
