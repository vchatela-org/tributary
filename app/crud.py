from typing import Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import SyncRun


def _filtered(db: Session, connector: Optional[str], status: Optional[str]):
    query = db.query(SyncRun)
    if connector:
        query = query.filter(SyncRun.connector == connector)
    if status:
        query = query.filter(SyncRun.status == status)
    return query


def latest_runs(db: Session, connector: Optional[str] = None, limit: int = 20, offset: int = 0,
                status: Optional[str] = None) -> List[SyncRun]:
    return _filtered(db, connector, status).order_by(SyncRun.id.desc()).offset(offset).limit(limit).all()


def count_runs(db: Session, connector: Optional[str] = None, status: Optional[str] = None) -> int:
    return _filtered(db, connector, status).count()


def get_run(db: Session, run_id: int) -> Optional[SyncRun]:
    return db.get(SyncRun, run_id)


def latest_upload_runs(db: Session, connectors: Iterable[str], successful_only: bool = False
                       ) -> Dict[str, SyncRun]:
    """The newest file upload/check per connector, without one query per source.

    With `successful_only`, restrict the result to completed real imports. The
    unfiltered form also includes checks and failures for the checklist's
    secondary activity state.
    """
    keys = list(connectors)
    if not keys:
        return {}
    latest = (db.query(SyncRun.connector, func.max(SyncRun.id).label("run_id"))
              .filter(SyncRun.connector.in_(keys), SyncRun.trigger == "upload"))
    if successful_only:
        latest = latest.filter(SyncRun.dry_run.is_(False), SyncRun.status == "ok")
    ids = [row.run_id for row in latest.group_by(SyncRun.connector).all()]
    if not ids:
        return {}
    runs = db.query(SyncRun).filter(SyncRun.id.in_(ids)).all()
    return {run.connector: run for run in runs}
