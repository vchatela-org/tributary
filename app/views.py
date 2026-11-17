"""What the pages show, built once here so the web routes and the polled HTML
fragments cannot drift apart.

Everything the overview needs about one connector: the state to show (running,
queued, checked, failing, never run, not configured, OK), when it last ran and
what it wrote, when it runs next, and what it accepts.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import crud, preferences, scheduler
from app.categories import CATEGORIES, EXTERNAL_TOOLS, hue_for
from app.config import settings
from app.connectors.base import Connector
from app.models import MonthlyImportAcknowledgment, StagedUpload, SyncRun, utcnow

# The states a connector card can be in, and how they read. Ordered by what the
# user needs to see first; the first match wins.
STATES = {
    "running": "Running",
    "queued": "Queued",
    "checked": "Checked, not imported",
    "failing": "Failing",
    "new": "Never run yet",
    "unconfigured": "Not configured",
    "ok": "OK",
}


def input_label(connector) -> str:
    """How the source's data arrives, in the card's words."""
    api, files = connector.api_configured(), connector.accepts_files
    watched = connector.watch_dir is not None
    if api and watched:
        return "from the API and a watched folder"
    if api:
        return "from the API"
    if watched:
        return "from a watched folder"
    if files:
        return "from an export you import"
    return "no input configured"


def state_of(connector, last: Optional[SyncRun], staged: Optional[StagedUpload],
             pending: bool, running: bool) -> str:
    if running:
        return "running"
    if staged is not None:
        return "checked"
    if pending:
        return "queued"
    # An API source missing its settings is the only one that has something to
    # set up. A source that is only imported into has nothing to configure: it
    # is never "not configured", whatever it has or has not imported yet.
    if connector.has_api and not connector.api_configured():
        return "unconfigured"
    if last is None:
        return "new"
    return "failing" if last.status == "error" else "ok"


def card(db: Session, connector: Connector, hidden: bool = False,
         pending_keys: Optional[set] = None, running_keys: Optional[set] = None) -> Dict[str, Any]:
    resolved = scheduler.resolved_for(db, connector)
    last = (crud.latest_runs(db, connector.key, limit=1) or [None])[0]
    staged = db.get(StagedUpload, connector.key)
    state = state_of(connector, last, staged,
                     pending=bool(pending_keys and connector.key in pending_keys),
                     running=bool(running_keys and connector.key in running_keys))
    return {
        "connector": connector,
        "key": connector.key,
        "label": connector.label,
        "description": connector.description,
        "icon": connector.icon,
        "category": connector.category,
        "hue": hue_for(connector.category),
        "state": state,
        "state_label": STATES[state],
        "configured": connector.is_configured(),
        "schedule": resolved if resolved.scheduled else None,
        "accepts_files": connector.accepts_files,
        "has_api": connector.has_api,
        # Whether a schedule can apply to this source at all: it reads an API or
        # a watched folder. An upload-only source cannot: it is imported by hand
        # when a file arrives, and the page says so instead of offering an editor
        # whose value nothing would apply.
        "schedule_capable": connector.has_api or connector.watch_dir is not None,
        "file_types": connector.file_types,
        "required_env": connector.required_env,
        # What to do when the variables alone don't say it (connect on the page).
        "setup_hint": connector.setup_hint() if state == "unconfigured" else None,
        "measurements": connector.measurements,
        "input_label": input_label(connector),
        "last_run": last,
        "staged": staged,
        "hidden": hidden,
    }


def sections(db: Session) -> List[Dict[str, Any]]:
    """Tributary's connectors grouped by category, each with the compatible
    tools that feed the same databases.

    A category is shown when it has something under it: a visible connector, or
    the compatible tools that feed that domain (a category whose connectors are
    all hidden still shows garmin-grafana under Sleep). A category with neither
    is left out entirely: never an empty heading."""
    hidden = preferences.hidden_connectors(db)
    pending_keys = scheduler.pending_keys()
    running_keys = {run.connector for run in scheduler.running(db)}
    by_category: Dict[str, List[Dict[str, Any]]] = {name: [] for name in CATEGORIES}
    for connector in all_ordered():
        if connector.key in hidden:
            continue
        by_category.setdefault(connector.category, []).append(
            card(db, connector, hidden=False, pending_keys=pending_keys, running_keys=running_keys))
    out = []
    for name in CATEGORIES:
        cards = by_category.get(name, [])
        tools = EXTERNAL_TOOLS.get(name, ())
        if not cards and not tools:
            continue  # nothing under this heading: not even one is shown
        out.append({"name": name, "cards": cards, "tools": tools})
    return out


def next_up(sections_: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The soonest scheduled run among the sources shown, for the activity bar:
    {"card", "at"}, or None when nothing is scheduled."""
    upcoming = [(c["schedule"].next_run, c) for section in sections_ for c in section["cards"]
                if c["schedule"] is not None and c["schedule"].next_run is not None]
    if not upcoming:
        return None
    at, first = min(upcoming, key=lambda pair: pair[0])
    return {"card": first, "at": at}


def nav(db: Session) -> List[Dict[str, Any]]:
    """The sources the sidebar lists, the shown ones in category order: the
    same cards as the overview, so a dot there and a pill here agree."""
    hidden = preferences.hidden_connectors(db)
    pending_keys = scheduler.pending_keys()
    running_keys = {run.connector for run in scheduler.running(db)}
    ordered = sorted((c for c in all_ordered() if c.key not in hidden),
                     key=lambda c: (hue_for(c.category) or len(CATEGORIES) + 1))
    return [card(db, c, pending_keys=pending_keys, running_keys=running_keys) for c in ordered]


def hidden_cards(db: Session) -> List[Dict[str, Any]]:
    """The connectors the user hid, for the settings page: always listed there,
    so hiding is never a one-way door."""
    hidden = preferences.hidden_connectors(db)
    return [card(db, c) for c in all_ordered() if c.key in hidden]


def all_ordered() -> List[Connector]:
    from app import connectors
    return connectors.all_connectors()


def by_key() -> Dict[str, Connector]:
    """The registered connectors, by key: what names a run's source in the
    activity line and the run history, wherever a page shows a run out of its
    connector's context."""
    return {connector.key: connector for connector in all_ordered()}


def counters(sections_: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"total": 0, "ok": 0, "failing": 0, "checked": 0, "active": 0}
    for section in sections_:
        for c in section["cards"]:
            out["total"] += 1
            if c["state"] == "failing":
                out["failing"] += 1
            elif c["state"] == "checked":
                out["checked"] += 1
            elif c["state"] in ("running", "queued"):
                out["active"] += 1
            elif c["state"] == "ok":
                out["ok"] += 1
    return out


MONTHLY_IMPORT_STATES = {
    "never": "Never imported",
    "due": "To import",
    "done": "Done this month",
    "no_data": "No data this month",
}


def _import_activity(latest: Optional[SyncRun], staged: Optional[StagedUpload],
                     running: Optional[SyncRun]) -> Optional[Dict[str, Any]]:
    """Current file activity, kept separate from whether the month is done."""
    if running is not None:
        return {"state": "running", "label": "Checking file" if running.dry_run else "Importing",
                "run": running}
    if latest is not None and latest.status == "error":
        return {"state": "failing", "label": "Check failed" if latest.dry_run else "Import failed",
                "run": latest}
    if staged is not None:
        return {"state": "checked", "label": "Checked, not imported", "run": None}
    return None


def monthly_import_period(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The current local month as a storage key and naive-UTC run boundaries."""
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(settings.tz)
    local = moment.astimezone(zone)
    month_start = datetime(local.year, local.month, 1, tzinfo=zone)
    next_month = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=zone)
    return {
        "key": local.strftime("%Y-%m"),
        "label": local.strftime("%B %Y"),
        "start": month_start.astimezone(timezone.utc).replace(tzinfo=None),
        "end": next_month.astimezone(timezone.utc).replace(tzinfo=None),
    }


def import_checklist(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The selected file imports and their state in the current local month.

    Completion comes either from a successful, non-dry upload or from the
    user's source-specific no-data acknowledgment. Activity remains separate:
    a failed re-import can need attention without erasing either completion.
    """
    period = monthly_import_period(now)
    file_connectors = [connector for connector in all_ordered() if connector.accepts_files]
    selected = preferences.monthly_import_connectors(db, [c.key for c in file_connectors])
    enabled = [connector for connector in file_connectors if connector.key in selected]
    keys = [connector.key for connector in enabled]

    latest_uploads = crud.latest_upload_runs(db, keys)
    last_imports = crud.latest_upload_runs(db, keys, successful_only=True)
    staged = {row.connector: row for row in
              db.query(StagedUpload).filter(StagedUpload.connector.in_(keys)).all()} if keys else {}
    acknowledgments = {row.connector: row for row in db.query(MonthlyImportAcknowledgment)
                       .filter(MonthlyImportAcknowledgment.connector.in_(keys),
                               MonthlyImportAcknowledgment.month == period["key"]).all()} if keys else {}
    running = {run.connector: run for run in scheduler.running(db)
               if run.connector in selected and run.trigger == "upload"}

    month_start_utc, next_month_utc = period["start"], period["end"]

    items = []
    for connector in enabled:
        last_import = last_imports.get(connector.key)
        completed_at = (last_import.finished_at or last_import.started_at) if last_import else None
        imported = completed_at is not None and month_start_utc <= completed_at < next_month_utc
        acknowledgment = acknowledgments.get(connector.key)
        done = imported or acknowledgment is not None
        state = "done" if imported else ("no_data" if acknowledgment is not None
                                         else ("never" if last_import is None else "due"))
        checked = staged.get(connector.key)
        items.append({
            "connector": connector,
            "key": connector.key,
            "label": connector.label,
            "description": connector.description,
            "icon": connector.icon,
            "category": connector.category,
            "hue": hue_for(connector.category),
            "file_types": connector.file_types,
            "state": state,
            "state_label": MONTHLY_IMPORT_STATES[state],
            "done": done,
            "last_import": last_import,
            "acknowledgment": acknowledgment,
            "activity": _import_activity(latest_uploads.get(connector.key), checked, running.get(connector.key)),
            "staged": checked,
            "staged_expires": checked.created_at + timedelta(hours=settings.staged_upload_ttl_hours)
            if checked is not None else None,
        })
    return {
        "items": items,
        "remaining": [item for item in items if not item["done"]],
        "completed": [item for item in items if item["done"]],
        "month": period["label"],
    }
