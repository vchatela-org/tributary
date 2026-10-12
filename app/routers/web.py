from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import connectors, crud, influxdb, scheduler
from app.config import settings
from app.database import get_db
from app.routers.common import connector_or_404, queue_run, queue_upload, safe_back, same_origin
from app.templating import templates

router = APIRouter(include_in_schema=False)

RUNS_PER_PAGE = 50


def _busy(db: Session) -> bool:
    """A run is going on or queued: the page refreshes itself."""
    return scheduler.has_pending() or any(r.status == "running" for r in crud.latest_runs(db, limit=5))


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    items = [{"connector": c, "configured": c.is_configured(), "runs": crud.latest_runs(db, c.key, limit=5)}
             for c in connectors.all_connectors()]
    return templates.TemplateResponse(request, "index.html", {
        "items": items,
        "influxdb_configured": influxdb.is_configured(),
        "influxdb_user": settings.influxdb_user,
        "busy": _busy(db),
    })


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, connector: Optional[str] = None, page: int = Query(1, ge=1),
              db: Session = Depends(get_db)):
    current = connector_or_404(connector) if connector else None
    total = crud.count_runs(db, connector)
    return templates.TemplateResponse(request, "runs.html", {
        "current": current,
        "connectors": connectors.all_connectors(),
        "runs": crud.latest_runs(db, connector, RUNS_PER_PAGE, (page - 1) * RUNS_PER_PAGE),
        "page": page,
        "pages": max(1, ceil(total / RUNS_PER_PAGE)),
        "busy": _busy(db),
    })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return templates.TemplateResponse(request, "run.html", {
        "run": run, "connector": connectors.get(run.connector), "busy": run.status == "running",
    })


@router.post("/connectors/{key}/run", dependencies=[Depends(same_origin)])
def run_now(request: Request, key: str, dry_run: bool = Form(False), full: bool = Form(False)):
    """The "Run now" / "Dry run" / "Full resync" buttons: queue, back to the page."""
    queue_run(connector_or_404(key), dry_run=dry_run, full=full)
    return RedirectResponse(safe_back(request), status_code=303)


@router.post("/connectors/{key}/upload", dependencies=[Depends(same_origin)])
def upload(key: str, file: UploadFile = File(...), dry_run: bool = Form(False)):
    queue_upload(connector_or_404(key), file, dry_run=dry_run)
    return RedirectResponse(f"/runs?connector={key}", status_code=303)
