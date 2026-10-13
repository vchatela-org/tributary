"""What the pages show, built once here so the web routes and the polled HTML
fragments cannot drift apart.

Everything the overview needs about one connector: the state to show (running,
queued, checked, failing, never run, not configured, OK), when it last ran and
what it wrote, when it runs next, and what it accepts.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app import crud, preferences, scheduler
from app.categories import CATEGORIES, EXTERNAL_TOOLS, hue_for
from app.connectors.base import Connector
from app.models import StagedUpload, SyncRun

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
    the compatible tools that feed that domain (Sleep has no connector of
    Tributary's yet -- Garmin and Whoop cover it). A category with neither is
    left out entirely: never an empty heading."""
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
