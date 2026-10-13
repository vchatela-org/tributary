"""The pages.

Nothing here reloads itself: the overview polls a fragment and swaps the status
regions in place, so a run in flight never yanks a file picker out from under
the user, and several sources can be queued one after another. Every write ends
back on the page it came from (`safe_back`), never on a separate one.
"""
from datetime import timedelta
from math import ceil
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import connectors, crud, influxdb, preferences, scheduler, schedule, views
from app.categories import CATEGORIES, EXTERNAL_TOOLS
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import ConnectorSchedule, utcnow
from app.routers.common import (connector_or_404, discard_staged, queue_promote, queue_run, queue_upload,
                                safe_back, same_origin)
from app.templating import templates

router = APIRouter(include_in_schema=False)

RUNS_PER_PAGE = 50


def _overview(db: Session) -> dict:
    """Everything the overview shows, shared by the page and the polled
    fragment so the two cannot drift apart."""
    sections = views.sections(db)
    return {
        "sections": sections,
        "counters": views.counters(sections),
        "active": [{"run": run, "connector": connectors.get(run.connector)}
                   for run in scheduler.running(db)],
        "queued": sorted(scheduler.pending_keys()),
        "influxdb_configured": influxdb.is_configured(),
        "influxdb_user": settings.influxdb_user,
        "ttl_hours": settings.staged_upload_ttl_hours,
    }


def _connector_page(db: Session, connector) -> dict:
    item = views.card(db, connector)
    resolved = item["schedule"]
    staged = item["staged"]
    return {
        "item": item,
        "connector_obj": connector,
        "resolved": resolved,
        "presets": schedule.presets_for(resolved.cron if resolved else None),
        "default_label": schedule.describe(connector.default_schedule) if connector.default_schedule
        else "Manual only",
        "clock": schedule.clock_of(resolved.cron if resolved else None),
        "weekday": schedule.weekday_of(resolved.cron if resolved else None),
        "custom": (resolved.cron if resolved else None) or (connector.default_schedule or "0 3 * * *"),
        "next_runs": [schedule.to_utc(moment) for moment in
                      schedule.next_runs(resolved.cron, settings.tz)] if resolved else [],
        "runs": crud.latest_runs(db, connector.key, RUNS_PER_PAGE),
        "total_runs": crud.count_runs(db, connector.key),
        "staged_expires": staged.created_at + timedelta(hours=settings.staged_upload_ttl_hours)
        if staged is not None else None,
        "influxdb_user": settings.influxdb_user,
        "ttl_hours": settings.staged_upload_ttl_hours,
    }


# ────────────────────────────────────────────────
# Pages
# ────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def index(request: Request, show: Optional[str] = None, db: Session = Depends(get_db)):
    hidden = views.hidden_cards(db) if show == "hidden" else []
    return templates.TemplateResponse(request, "index.html", {
        **_overview(db), "hidden_cards": hidden, "showing_hidden": show == "hidden",
    })


@router.get("/connectors/{key}", response_class=HTMLResponse)
def connector_page(request: Request, key: str, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "connector.html",
                                      _connector_page(db, connector_or_404(key)))


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, connector: Optional[str] = None, status: Optional[str] = None,
              page: int = Query(1, ge=1), db: Session = Depends(get_db)):
    current = connector_or_404(connector) if connector else None
    total = crud.count_runs(db, connector, status)
    return templates.TemplateResponse(request, "runs.html", {
        "current": current,
        "status": status or "",
        "connectors": connectors.all_connectors(),
        "runs": crud.latest_runs(db, connector, RUNS_PER_PAGE, (page - 1) * RUNS_PER_PAGE, status),
        "page": page,
        "pages": max(1, ceil(total / RUNS_PER_PAGE)),
        "total": total,
    })


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return templates.TemplateResponse(request, "run.html", {
        "run": run, "connector": connectors.get(run.connector),
        "promoted_from": crud.get_run(db, run.promoted_from) if run.promoted_from else None,
    })


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    """Which sources are shown, and which are left out. Hiding is display only:
    a hidden connector keeps being scheduled and keeps filling its series, and
    it stays listed here to be brought back."""
    return templates.TemplateResponse(request, "settings.html", {
        "cards": [views.card(db, c) for c in connectors.all_connectors()],
        "categories": CATEGORIES,
        "tools": EXTERNAL_TOOLS,
        "tools_outside": sorted({t.label for tools in EXTERNAL_TOOLS.values() for t in tools}),
        "influxdb_configured": influxdb.is_configured(),
    })


# ────────────────────────────────────────────────
# Polled fragments: the page stays where it is
# ────────────────────────────────────────────────

@router.get("/partials/status", response_class=HTMLResponse)
def status_partial(request: Request, db: Session = Depends(get_db)):
    """The status bits of every card, and the activity line. Swapped in place by
    static/app.js; never the whole page, and never a region holding a file
    input."""
    return templates.TemplateResponse(request, "_status.html", _overview(db))


@router.get("/partials/runs", response_class=HTMLResponse)
def runs_partial(request: Request, connector: Optional[str] = None, status: Optional[str] = None,
                 page: int = Query(1, ge=1), db: Session = Depends(get_db)):
    current = connector_or_404(connector) if connector else None
    total = crud.count_runs(db, connector, status)
    return templates.TemplateResponse(request, "_runs.html", {
        "current": current,
        "status": status or "",
        "runs": crud.latest_runs(db, connector, RUNS_PER_PAGE, (page - 1) * RUNS_PER_PAGE, status),
        "page": page,
        "pages": max(1, ceil(total / RUNS_PER_PAGE)),
        "total": total,
    })


# ────────────────────────────────────────────────
# Writes (each one ends back on the page it came from)
# ────────────────────────────────────────────────

@router.post("/connectors/{key}/run", dependencies=[Depends(same_origin)])
def run_now(request: Request, key: str, dry_run: bool = Form(False), full: bool = Form(False)):
    """The "Run now" / "Dry run" / "Full resync" buttons: queue, back to the page."""
    queue_run(connector_or_404(key), dry_run=dry_run, full=full)
    return RedirectResponse(safe_back(request), status_code=303)


@router.post("/connectors/{key}/check", dependencies=[Depends(same_origin)])
def check_file(request: Request, key: str, file: UploadFile = File(...)):
    """Check an export: import nothing, keep the file, show what it would write.
    Then either "Import for real" or "Discard". Back to the page, so the next
    card's file picker is right there."""
    queue_upload(connector_or_404(key), file, dry_run=True)
    return RedirectResponse(safe_back(request), status_code=303)


@router.post("/connectors/{key}/import", dependencies=[Depends(same_origin)])
def import_checked(request: Request, key: str):
    """Import what was checked, without uploading the file again. A check that
    no longer stands (the file changed, a setting did, it expired) is refused
    here with a 409 saying so, and no run is queued."""
    queue_promote(connector_or_404(key))
    return RedirectResponse(safe_back(request), status_code=303)


@router.post("/connectors/{key}/discard", dependencies=[Depends(same_origin)])
def discard_checked(request: Request, key: str):
    """Drop a checked upload; the file goes with it."""
    discard_staged(connector_or_404(key))
    return RedirectResponse(safe_back(request), status_code=303)


@router.post("/connectors/{key}/schedule", dependencies=[Depends(same_origin)])
def set_schedule(request: Request, key: str, preset: str = Form(""), at: str = Form(""),
                 weekday: str = Form(""), custom: str = Form(""), cron: str = Form(""),
                 reset: bool = Form(False)):
    """Save a schedule from the editor: a preset with its time, a custom cron, or
    *Manual only*. An expression that does not parse is refused with a 400."""
    connector = connector_or_404(key)
    if reset:
        # "Reset to default": forget the saved value, so the environment or the
        # connector's own default applies again.
        with SessionLocal() as db:
            row = db.get(ConnectorSchedule, connector.key)
            if row is not None:
                db.delete(row)
                db.commit()
        scheduler.reschedule(connector.key)
        return RedirectResponse(safe_back(request, f"/connectors/{key}"), status_code=303)
    if preset:
        try:
            saved = schedule.build_cron(preset, at, weekday, custom) or ""
        except schedule.CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    elif cron:
        try:
            saved = schedule.validate(cron)
        except schedule.CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    else:
        raise HTTPException(status_code=400, detail="Pick how often it should run.")
    with SessionLocal() as db:
        row = db.get(ConnectorSchedule, connector.key) or ConnectorSchedule(connector=connector.key)
        row.cron, row.updated_at = saved, utcnow()
        db.add(row)
        db.commit()
    # The thread re-plans this connector: the new schedule applies now, not
    # after the previous deadline.
    scheduler.reschedule(connector.key)
    return RedirectResponse(safe_back(request, f"/connectors/{key}"), status_code=303)


@router.post("/settings", dependencies=[Depends(same_origin)])
def save_settings(request: Request, visible: List[str] = Form(default=[]), reset: bool = Form(False),
                  db: Session = Depends(get_db)):
    """The settings form: the sources the overview should show (its checkboxes),
    so the ones left unchecked are the hidden ones. `reset` shows everything
    again."""
    known = [c.key for c in connectors.all_connectors()]
    if reset:
        visible = known
    preferences.set_hidden_connectors(db, [key for key in known if key not in set(visible)], known)
    db.commit()
    return RedirectResponse(safe_back(request, "/settings"), status_code=303)
