from typing import List, Optional

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
