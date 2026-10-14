"""The pages.

Nothing here reloads itself: the overview polls a fragment and swaps the status
regions in place, so a run in flight never yanks a file picker out from under
the user, and several sources can be queued one after another. Every write ends
back on the page it came from (`safe_back`), never on a separate one.
"""
from datetime import timedelta
from math import ceil
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import connectors, crud, freshness, influxdb, preferences, scheduler, schedule, views
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
        "next_up": views.next_up(sections),
        "connectors_by_key": views.by_key(),
        "influxdb_configured": influxdb.is_configured(),
        "influxdb_user": settings.influxdb_user,
        "ttl_hours": settings.staged_upload_ttl_hours,
    }


def _connector_page(db: Session, connector) -> dict:
    item = views.card(db, connector)
    # The whole resolution, not just when it is scheduled: the page says whether a
    # value was saved here (*Manual only*) or none applies at all.
    resolved = scheduler.resolved_for(db, connector)
    staged = item["staged"]
    return {
        "item": item,
        "connector_obj": connector,
        "resolved": resolved,
        "presets": schedule.presets_for(resolved.cron),
        "default_label": schedule.describe(connector.default_schedule) if connector.default_schedule
        else "Manual only",
        "clock": schedule.clock_of(resolved.cron),
        "weekday": schedule.weekday_of(resolved.cron),
        "custom": resolved.cron or (connector.default_schedule or "0 3 * * *"),
        "next_runs": [schedule.to_utc(moment) for moment in
                      schedule.next_runs(resolved.cron, settings.tz)] if resolved.cron else [],
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
    showing = show == "hidden"
    overview = _overview(db)
    return templates.TemplateResponse(request, "index.html", {
        **overview, "hidden_cards": views.hidden_cards(db) if showing else [],
        "showing_hidden": showing, "has_hidden": bool(preferences.hidden_connectors(db)),
        # The page's, not _overview's: the polled fragment has no tool card, so
        # a poll never reads InfluxDB for it.
        "tool_freshness": freshness.for_tools(t for section in overview["sections"] for t in section["tools"]),
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
        "connectors_by_key": views.by_key(),
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
    hidden = preferences.hidden_connectors(db)
    return templates.TemplateResponse(request, "settings.html", {
        "cards": [views.card(db, c, hidden=c.key in hidden) for c in connectors.all_connectors()],
        "categories": CATEGORIES,
        "tools": EXTERNAL_TOOLS,
        "tools_outside": sorted({t.label for tools in EXTERNAL_TOOLS.values() for t in tools}),
        "tool_freshness": freshness.for_tools(t for tools in EXTERNAL_TOOLS.values() for t in tools),
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
        "connectors_by_key": views.by_key(),
        "runs": crud.latest_runs(db, connector, RUNS_PER_PAGE, (page - 1) * RUNS_PER_PAGE, status),
        "page": page,
        "pages": max(1, ceil(total / RUNS_PER_PAGE)),
        "total": total,
    })


# ────────────────────────────────────────────────
# Writes (each one ends back on the page it came from)
# ────────────────────────────────────────────────

# A refusal a form may hit (nothing checked, a check out of date, a bad cron, a
# file too big): the write ends back on the page that posted it, with the reason
# shown there, never as a raw JSON body.
_REFUSALS = (400, 409, 413)


def _back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(safe_back(request, default), status_code=303)


def _refused(request: Request, exc: HTTPException, default: str = "/") -> RedirectResponse:
    path = safe_back(request, default)
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}{urlencode({'notice': str(exc.detail)})}", status_code=303)


def _write(request: Request, work, default: str = "/") -> RedirectResponse:
    """Run one form's write; a refusal returns to the page it came from."""
    try:
        work()
    except HTTPException as exc:
        if exc.status_code in _REFUSALS:
            return _refused(request, exc, default)
        raise
    return _back(request, default)


@router.post("/connectors/{key}/run", dependencies=[Depends(same_origin)])
def run_now(request: Request, key: str, dry_run: bool = Form(False), full: bool = Form(False)):
    """The "Run now" / "Dry run" / "Full resync" buttons: queue, back to the page."""
    return _write(request, lambda: queue_run(connector_or_404(key), dry_run=dry_run, full=full))


@router.post("/connectors/{key}/check", dependencies=[Depends(same_origin)])
def check_file(request: Request, key: str, file: UploadFile = File(...)):
    """Check an export: import nothing, keep the file, show what it would write.
    Then either "Import for real" or "Discard". Back to the page, so the next
    card's file picker is right there."""
    return _write(request, lambda: queue_upload(connector_or_404(key), file, dry_run=True))


@router.post("/connectors/{key}/import", dependencies=[Depends(same_origin)])
def import_checked(request: Request, key: str):
    """Import what was checked, without uploading the file again. A check that no
    longer stands (the file changed, a setting did, it expired) is refused, with
    the reason shown back on the page and no run queued."""
    return _write(request, lambda: queue_promote(connector_or_404(key)))


@router.post("/connectors/{key}/discard", dependencies=[Depends(same_origin)])
def discard_checked(request: Request, key: str):
    """Drop a checked upload; the file goes with it."""
    return _write(request, lambda: discard_staged(connector_or_404(key)))


@router.post("/connectors/{key}/schedule", dependencies=[Depends(same_origin)])
def set_schedule(request: Request, key: str, preset: str = Form(""), at: str = Form(""),
                 weekday: str = Form(""), custom: str = Form(""), cron: str = Form(""),
                 reset: bool = Form(False)):
    """Save a schedule from the editor: a preset with its time, a custom cron, or
    *Manual only*. An expression that does not parse comes back as a notice on the
    page."""
    connector = connector_or_404(key)
    # Built from the registered connector, not the URL: the same path, and
    # never a value the request chose.
    default = f"/connectors/{connector.key}"
    if reset:
        # "Reset to default": forget the saved value, so the environment or the
        # connector's own default applies again.
        with SessionLocal() as db:
            row = db.get(ConnectorSchedule, connector.key)
            if row is not None:
                db.delete(row)
                db.commit()
        scheduler.reschedule(connector.key)
        return _back(request, default)
    try:
        if preset:
            saved = schedule.build_cron(preset, at, weekday, custom) or ""
        elif cron:
            saved = schedule.validate(cron)
        else:
            raise schedule.CronError("Pick how often it should run.")
    except schedule.CronError as exc:
        return _refused(request, HTTPException(status_code=400, detail=str(exc)), default)
    with SessionLocal() as db:
        row = db.get(ConnectorSchedule, connector.key) or ConnectorSchedule(connector=connector.key)
        row.cron, row.updated_at = saved, utcnow()
        db.add(row)
        db.commit()
    # The thread re-plans this connector: the new schedule applies now, not
    # after the previous deadline.
    scheduler.reschedule(connector.key)
    return _back(request, default)


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
    return _back(request, "/settings")
