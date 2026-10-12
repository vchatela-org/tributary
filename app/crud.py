from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import SyncRun


def latest_runs(db: Session, connector: Optional[str] = None, limit: int = 20, offset: int = 0) -> List[SyncRun]:
    query = db.query(SyncRun)
    if connector:
        query = query.filter(SyncRun.connector == connector)
    return query.order_by(SyncRun.id.desc()).offset(offset).limit(limit).all()


def count_runs(db: Session, connector: Optional[str] = None) -> int:
    query = db.query(SyncRun)
    if connector:
        query = query.filter(SyncRun.connector == connector)
    return query.count()


def get_run(db: Session, run_id: int) -> Optional[SyncRun]:
    return db.get(SyncRun, run_id)
