"""Connecting a source that grants access in a browser (OAuth 2.0: WHOOP).

  POST /connectors/{key}/connect      same-origin only; 303 to the source's
                                      authorization page, with a new state.
  GET  /connectors/{key}/callback     where the source sends the browser back.
                                      Deliberately *without* the same-origin
                                      check: the browser arrives from the
                                      source's site (Sec-Fetch-Site:
                                      cross-site). The single-use, 10-minute
                                      `state` protects it instead.
  POST /connectors/{key}/callback     the same, with the address the browser
                                      landed on pasted into a form (a plain
                                      http:// install, whose redirect URI must
                                      still be https://); same-origin only.
  POST /connectors/{key}/disconnect   same-origin only; revoke, then drop.

Every redirect is built from constants and the registered connector, never
from what the request carries, and every answer ends on the source's page. A
message shown there is one of this code's, never a value the source or the
browser sent.
"""
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from app import oauth, scheduler
from app.connectors.base import Connector, ConnectorError
from app.routers.common import connector_or_404, same_origin

router = APIRouter(include_in_schema=False)

PASTED_MAX = 4096
_CALLBACK_QUERY = re.compile(r"(/connectors/[^/?\s]+/callback)\?\S*")


class RedactCallbackQuery(logging.Filter):
    """The access log line of a callback without its query: the
    authorization code and the state are nobody's business, even single use."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_CALLBACK_QUERY.sub(r"\1?<redacted>", arg) if isinstance(arg, str) else arg
                                for arg in record.args)
        elif isinstance(record.msg, str):
            record.msg = _CALLBACK_QUERY.sub(r"\1?<redacted>", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactCallbackQuery())


def _oauth_connector(key: str) -> Connector:
    connector = connector_or_404(key)
    if not connector.uses_oauth:
        raise HTTPException(status_code=404, detail="Unknown connector")
    return connector


def _page(connector: Connector, notice: Optional[str] = None) -> RedirectResponse:
    """The source's own page, from its registered key; `notice`, a message of
    this code's, shown on top."""
    path = f"/connectors/{connector.key}"
    if notice:
        path += "?" + urlencode({"notice": notice})
    return RedirectResponse(path + "#connection", status_code=303)


def _denied(connector: Connector, error: str) -> str:
    if error == "access_denied":
        return f"Access was not granted on {connector.label}'s page: nothing was connected."
    return f"{connector.label} answered with an error instead of an authorization: nothing was connected."


def _finish(connector: Connector, code: Optional[str], state: Optional[str], error: Optional[str]
            ) -> RedirectResponse:
    if error:
        oauth.consume_state(connector.key, state)  # spent either way
        return _page(connector, _denied(connector, error))
    try:
        oauth.finish(connector, code, state)
    except ConnectorError as exc:
        return _page(connector, str(exc))
    # Connected: its schedule applies now, and its first run comes shortly.
    scheduler.reschedule(connector.key)
    return _page(connector)


@router.post("/connectors/{key}/connect", dependencies=[Depends(same_origin)])
def connect(key: str):
    connector = _oauth_connector(key)
    try:
        authorize = oauth.start(connector)
    except ConnectorError as exc:
        return _page(connector, str(exc))
    return RedirectResponse(authorize, status_code=303)


@router.get("/connectors/{key}/callback")
def callback(key: str, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    return _finish(_oauth_connector(key), code, state, error)


@router.post("/connectors/{key}/callback", dependencies=[Depends(same_origin)])
def pasted_callback(key: str, url: str = Form("")):
    """The address the browser landed on after granting access, pasted: its
    query is read as the callback's would be."""
    connector = _oauth_connector(key)
    query = parse_qs(urlparse(url.strip()[:PASTED_MAX]).query)

    def first(name: str) -> Optional[str]:
        values = query.get(name)
        return values[0] if values else None

    if not (first("code") or first("error")):
        return _page(connector, "That address holds no authorization: paste the whole address the browser "
                                "landed on, from https:// to the end.")
    return _finish(connector, first("code"), first("state"), first("error"))


@router.post("/connectors/{key}/disconnect", dependencies=[Depends(same_origin)])
def disconnect(key: str):
    connector = _oauth_connector(key)
    warning = oauth.disconnect(connector)
    scheduler.reschedule(connector.key)
    return _page(connector, warning)
