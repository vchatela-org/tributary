"""Refresh now: fetch what a connector reads, then run it.

A connector that reads other writers' data (the WHOOP models connector reads
the Whoop connector's series, fitbit-grafana's and garmin-grafana's) runs on
what is in InfluxDB at that moment. Before bed that may be hours old: the Whoop
connector runs once a day, and a compatible tool fetches on its own clock. A
refresh is one button for the whole chain, in this order:

  1. the **refresh hooks** of the compatible tools it reads, at once: a POST to
     the address the deployment gave for that tool (TRIBUTARY_<TOOL>_REFRESH_URL),
     answered once the tool's fetch is written. A tool with no hook is read as
     it is (its freshness line says how old that is);
  2. a run of each Tributary connector it reads, one after the other;
  3. its own run.

Every run of a refresh is recorded like any other, with trigger "refresh". A
failed hook or run is shown as failed, and the chain goes on: the last step
still runs, on what is there.

The hook (docs/compatible-tools.md, "Refresh hook"): `POST <url>`, no body,
`Authorization: Bearer <token>` when a token is set. A 2xx means the fetch is
done and written; anything else, a redirect included (never followed: it would
take the token along), or no answer within TRIBUTARY_REFRESH_TIMEOUT_SECONDS,
is a failed step. The answer's body is never read, and nothing of it is logged.
The address comes from the environment only, never from a request.

What a refresh cannot force: the device syncing to its phone app (open the app
first), and a source still finishing what it holds (Google Health can keep a
Fitbit night short for hours after the wake-up).

One chain per connector at a time: a second request while one runs joins it.
Its state lives in memory (a restart forgets it; its runs stay recorded).
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app import connectors, freshness, scheduler
from app.__version__ import __version__
from app.categories import FITBIT_GRAFANA, GARMIN_GRAFANA, ExternalTool
from app.config import settings
from app.connectors.base import Connector
from app.database import SessionLocal
from app.models import SyncRun, utcnow

logger = logging.getLogger(__name__)

TRIGGER = "refresh"
# How often a chain looks whether its run has ended (tests shorten it).
POLL_SECONDS = 0.5


class Tool(NamedTuple):
    """A compatible tool a refresh may ask to fetch: its card, and where its
    hook's settings are read (at each call, so a test or a restart sees a
    change)."""
    tool: ExternalTool
    url: Callable[[], Optional[str]]
    token: Callable[[], Optional[str]]


def _secret(value) -> Optional[str]:
    return value.get_secret_value() if value is not None else None


# By the name of the tool's freshness probe (app/freshness.py), the name a
# connector gives in refresh_inputs().
TOOLS: Dict[str, Tool] = {
    "garmin-grafana": Tool(GARMIN_GRAFANA, lambda: settings.garmin_grafana_refresh_url,
                           lambda: _secret(settings.garmin_grafana_refresh_token)),
    "fitbit-grafana": Tool(FITBIT_GRAFANA, lambda: settings.fitbit_grafana_refresh_url,
                           lambda: _secret(settings.fitbit_grafana_refresh_token)),
}


# ────────────────────────────────────────────────
# The hook
# ────────────────────────────────────────────────

class _NoRedirect(HTTPRedirectHandler):
    """A redirect is a failed hook: urllib would send the token along, to any
    host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect)


def call_hook(url: str, token: Optional[str] = None, timeout: Optional[float] = None) -> Tuple[bool, str]:
    """POST to a tool's refresh hook. (whether it answered 2xx, what to show).
    The message names the outcome only: never the address, the token or a byte
    of the answer."""
    timeout = settings.refresh_timeout_seconds if timeout is None else timeout
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "its refresh address is not an http(s) URL"
    headers = {"User-Agent": f"Tributary/{__version__}", "Content-Length": "0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    try:
        with _opener.open(Request(url, data=b"", headers=headers, method="POST"), timeout=timeout) as answer:
            status = answer.status
    except HTTPError as exc:
        exc.close()
        if 300 <= exc.code < 400:
            return False, f"answered {exc.code}, a redirect (not followed)"
        return False, f"answered {exc.code}"
    except (TimeoutError, OSError, URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return False, f"did not answer within {timeout:g} s"
        return False, "could not be reached"
    if not 200 <= status < 300:
        return False, f"answered {status}"
    return True, f"fetched in {time.monotonic() - started:.0f} s"


# ────────────────────────────────────────────────
# The chain
# ────────────────────────────────────────────────

@dataclass
class Step:
    kind: str  # "hook" (a tool asked to fetch), "tool" (one read as it is), "run" (a Tributary run)
    key: str  # the tool's probe name, or the connector's key
    label: str
    state: str = "waiting"  # waiting, running, done, failed, skipped
    message: str = ""
    finished_at: Optional[datetime] = None  # naive UTC
    run_id: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "label": self.label, "state": self.state,
                "message": self.message, "run_id": self.run_id,
                "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None}


@dataclass
class Chain:
    key: str
    steps: List[Step]
    started_at: datetime = field(default_factory=utcnow)  # naive UTC
    finished_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def failed(self) -> bool:
        return any(step.state == "failed" for step in self.steps)


_chains: Dict[str, Chain] = {}
_lock = threading.Lock()


def inputs(connector: Connector) -> Tuple[List[str], List[str]]:
    """(the Tributary connectors, the compatible tools) a connector reads,
    restricted to the ones that exist."""
    keys, tools = connector.refresh_inputs()
    return ([k for k in keys if k != connector.key and connectors.get(k) is not None],
            [t for t in tools if t in TOOLS])


def refreshable(connector: Optional[Connector]) -> bool:
    """Whether a refresh means more than "Run now": it reads others' data."""
    if connector is None:
        return False
    keys, tools = inputs(connector)
    return bool(keys or tools)


def plan(connector: Connector) -> List[Step]:
    """The steps a refresh of `connector` takes, in order."""
    keys, tools = inputs(connector)
    steps = []
    for name in tools:
        tool = TOOLS[name]
        if tool.url():
            steps.append(Step("hook", name, f"Ask {tool.tool.label} to fetch now"))
        else:
            steps.append(Step("tool", name, f"Read {tool.tool.label}'s data as it is", state="skipped",
                              message="no refresh hook set for it"))
    for key in keys:
        other = connectors.get(key)
        if other.is_configured():
            steps.append(Step("run", key, f"Run {other.label}"))
        else:
            steps.append(Step("run", key, f"Run {other.label}", state="skipped", message="not configured"))
    steps.append(Step("run", connector.key, f"Run {connector.label}"))
    return steps


def status(key: str) -> Optional[Chain]:
    """The current or last chain of this connector (None: none since the start)."""
    with _lock:
        return _chains.get(key)


def start(connector: Connector) -> Chain:
    """Start a refresh, or join the one running."""
    with _lock:
        current = _chains.get(connector.key)
        if current is not None and current.running:
            return current
        chain = Chain(connector.key, plan(connector))
        _chains[connector.key] = chain
    threading.Thread(target=_run_chain, args=(chain,), name=f"refresh-{connector.key}", daemon=True).start()
    return chain


def _hook(step: Step) -> None:
    tool = TOOLS[step.key]
    step.state = "running"
    try:
        ok, message = call_hook(tool.url() or "", tool.token())
    except Exception as exc:  # noqa: BLE001 -- a hook never breaks the chain
        ok, message = False, f"failed ({type(exc).__name__})"
    step.state, step.message, step.finished_at = ("done" if ok else "failed"), message, utcnow()
    logger.info("Refresh: %s %s", tool.tool.label, message)


def _wait_for_run(key: str, since: datetime, deadline: float) -> Optional[SyncRun]:
    """The refresh run of `key` started at or after `since`, once it ended
    (None: none ended before the deadline)."""
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            row = (db.query(SyncRun)
                   .filter(SyncRun.connector == key, SyncRun.trigger == TRIGGER, SyncRun.started_at >= since,
                           SyncRun.status != "running")
                   .order_by(SyncRun.id.desc()).first())
            if row is not None:
                db.expunge(row)
                return row
        time.sleep(POLL_SECONDS)
    return None


def _run(step: Step) -> None:
    step.state = "running"
    since = utcnow().replace(microsecond=0)
    if not scheduler.request(step.key, trigger=TRIGGER):
        step.state, step.message, step.finished_at = "failed", "could not be queued", utcnow()
        return
    row = _wait_for_run(step.key, since, time.monotonic() + settings.refresh_timeout_seconds)
    step.finished_at = utcnow()
    if row is None:
        step.state = "failed"
        step.message = f"still not done after {settings.refresh_timeout_seconds:g} s (it may still be queued)"
        return
    step.run_id = row.id
    step.state = "done" if row.status == "ok" else "failed"
    step.message = (row.message or "")[:300]


def _run_chain(chain: Chain) -> None:
    try:
        hooks = [step for step in chain.steps if step.kind == "hook"]
        if hooks:
            with ThreadPoolExecutor(max_workers=len(hooks), thread_name_prefix="refresh-hook") as pool:
                list(pool.map(_hook, hooks))
            # The freshness lines show what the hooks brought, not a minute-old answer.
            freshness.clear()
        for step in chain.steps:
            if step.kind == "run" and step.state == "waiting":
                _run(step)
    except Exception:  # noqa: BLE001 -- the chain must end, whatever happens
        logger.exception("Refresh of %s failed", chain.key)
        for step in chain.steps:
            if step.state in ("waiting", "running"):
                step.state, step.message, step.finished_at = "failed", "interrupted", utcnow()
    finally:
        chain.finished_at = utcnow()


def as_dict(connector: Connector, chain: Optional[Chain]) -> Dict[str, Any]:
    """What the API answers: the chain, or the steps a refresh would take."""
    if chain is None:
        return {"connector": connector.key, "state": "idle", "started_at": None, "finished_at": None,
                "steps": [step.as_dict() for step in plan(connector)]}
    return {"connector": connector.key,
            "state": "running" if chain.running else ("failed" if chain.failed else "done"),
            "started_at": chain.started_at.isoformat() + "Z",
            "finished_at": chain.finished_at.isoformat() + "Z" if chain.finished_at else None,
            "steps": [step.as_dict() for step in chain.steps]}


def reset() -> None:
    """Forget every chain (tests)."""
    with _lock:
        _chains.clear()
