"""OAuth 2.0, authorization code grant: for a source that grants access in a
browser rather than with a key in the environment (WHOOP, the first).

What the core keeps:
  - the grant, in `oauth_tokens`: one row per connector, its access and refresh
    tokens sealed together (app/secretbox.py: AES-256-GCM, bound to the
    connector's key), never in plain text. No secret key, no connection: there
    is no plain-text fallback. A rotated pair is committed **the moment it
    arrives**, in a session of its own -- in a dry run, and in a run that fails
    afterwards -- since a source that rotates refresh tokens kills the old one
    when the new one is issued;
  - each `state` sent to an authorization page, in `oauth_states`: single use,
    10 minutes, its SHA-256 only.

A connector that uses it implements:
  oauth_problem()             what is missing to start a connection (the app's
                              settings), or None;
  oauth_authorize_url(state)  the source's authorization page;
  oauth_exchange(code)        the token response for a code (a dict);
  oauth_refresh(token)        the token response for a refresh token; raises
                              GrantRevoked when the source refuses it
                              (`invalid_grant`): the user must reconnect;
  oauth_revoke(access_token)  revokes the grant at the source ("Disconnect").
and calls access_token() for each run, which refreshes when the access token
expires within a minute -- or when asked to, after a 401.

A lock per connector serialises the refresh, the callback and "Disconnect":
two refreshes of one rotating token would kill each other's.

Tokens are never logged, returned, previewed or rendered: the page reads
status(), which holds none.
"""
import hashlib
import json
import secrets
import string
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, Optional

from app import secretbox
from app.connectors.base import ConnectorError
from app.database import SessionLocal
from app.models import OAuthState, OAuthToken, utcnow

STATE_TTL = timedelta(minutes=10)
# The access token is refreshed when it expires sooner than this.
REFRESH_MARGIN = timedelta(seconds=60)
# A `state` like the one Whoops sends WHOOP (16 letters and digits), drawn from
# a cryptographic source.
STATE_ALPHABET = string.ascii_letters + string.digits
STATE_LENGTH = 16

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class OAuthError(ConnectorError):
    """A connection that can't be made or used, with a message for the UI.
    Never a token in it."""


class GrantRevoked(Exception):
    """The source refused the refresh token (`invalid_grant`): revoked, expired
    or already used. Only a new authorization helps."""


@dataclass
class Grant:
    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[datetime]  # naive UTC
    scope: Optional[str]

    @classmethod
    def from_response(cls, data: Any, label: str, refresh_token: Optional[str] = None,
                      scope: Optional[str] = None) -> "Grant":
        """A token endpoint's answer. A refresh answer without a refresh token
        keeps the one it was given (a source that doesn't rotate), and one
        without a scope the scope granted before."""
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise OAuthError(f"{label} answered without an access token.")
        expires_in = data.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            expires_at = utcnow() + timedelta(seconds=int(expires_in))
        renewed = data.get("refresh_token")
        granted = data.get("scope")
        return cls(access_token=data["access_token"],
                   refresh_token=renewed if isinstance(renewed, str) and renewed else refresh_token,
                   expires_at=expires_at, scope=granted[:255] if isinstance(granted, str) and granted else scope)

    def due(self) -> bool:
        """Whether the access token expires within the margin. One the source
        gave no lifetime for is used until it is refused (a 401): refreshing
        it at every use would spend a single-use refresh token each time."""
        return self.expires_at is not None and self.expires_at - utcnow() <= REFRESH_MARGIN


@contextmanager
def lock(key: str) -> Iterator[None]:
    with _locks_guard:
        held = _locks.setdefault(key, threading.Lock())
    with held:
        yield


def reconnect(label: str) -> str:
    return f"Reconnect {label} on its page."


# ────────────────────────────────────────────────
# The grant, sealed
# ────────────────────────────────────────────────

def has_grant(key: str) -> bool:
    """Whether a grant is stored (without opening it)."""
    with SessionLocal() as db:
        return db.get(OAuthToken, key) is not None


def save(key: str, grant: Grant) -> None:
    """Store a grant, sealed, and commit now: its own session, whatever the
    caller's does next (a dry run, a failed run rolls back theirs)."""
    payload = json.dumps({"access_token": grant.access_token, "refresh_token": grant.refresh_token})
    sealed = secretbox.seal(payload.encode("utf-8"), key)
    with SessionLocal() as db:
        row = db.get(OAuthToken, key) or OAuthToken(connector=key)
        row.ciphertext, row.expires_at, row.scope, row.updated_at = sealed, grant.expires_at, grant.scope, utcnow()
        db.add(row)
        db.commit()


def load(key: str, label: str) -> Optional[Grant]:
    """The stored grant, or None. One sealed with the previous key (a rotation)
    is sealed again with the current one."""
    with SessionLocal() as db:
        row = db.get(OAuthToken, key)
        if row is None:
            return None
        try:
            plain, stale = secretbox.open_(row.ciphertext, key)
        except secretbox.SecretKeyError as exc:
            raise OAuthError(f"The stored {label} connection can't be read: {exc}") from None
        except secretbox.Unreadable:
            raise OAuthError(f"The stored {label} connection can't be decrypted with the secret key set now "
                             f"(it changed, or was lost): set the key it was saved with, or reconnect {label} "
                             "on its page.") from None
        try:
            data = json.loads(plain)
        except ValueError:
            raise OAuthError(f"The stored {label} connection is unreadable: {reconnect(label)}") from None
        grant = Grant(access_token=data.get("access_token") or "", refresh_token=data.get("refresh_token"),
                      expires_at=row.expires_at, scope=row.scope)
        if stale:
            row.ciphertext = secretbox.seal(plain, key)
            db.add(row)
            db.commit()
        return grant


def forget(key: str) -> bool:
    with SessionLocal() as db:
        row = db.get(OAuthToken, key)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


def status(connector) -> Dict[str, Any]:
    """What the source's page shows of the connection: never a token."""
    with SessionLocal() as db:
        row = db.get(OAuthToken, connector.key)
        out: Dict[str, Any] = {
            "app_problem": connector.oauth_problem(),
            "key_problem": secretbox.problem(),
            "connected": row is not None,
            "readable": False, "expires_at": None, "scope": None, "updated_at": None,
        }
        if row is not None:
            out.update(readable=secretbox.readable(row.ciphertext, connector.key), expires_at=row.expires_at,
                       scope=row.scope, updated_at=row.updated_at)
        return out


# ────────────────────────────────────────────────
# Connecting
# ────────────────────────────────────────────────

def _digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _purge_states(db) -> None:
    db.query(OAuthState).filter(OAuthState.created_at < utcnow() - STATE_TTL).delete()


def start(connector) -> str:
    """The authorization page to send the browser to, with a new single-use
    state. Raises OAuthError when a connection can't be made: the app isn't
    registered (its settings), or no secret key could seal what comes back."""
    problem = connector.oauth_problem() or secretbox.problem()
    if problem:
        raise OAuthError(problem)
    state = "".join(secrets.choice(STATE_ALPHABET) for _ in range(STATE_LENGTH))
    with SessionLocal() as db:
        _purge_states(db)
        db.add(OAuthState(state_sha256=_digest(state), connector=connector.key, created_at=utcnow()))
        db.commit()
    return connector.oauth_authorize_url(state)


def consume_state(key: str, state: Optional[str]) -> bool:
    """Whether `state` is one this app sent for this connector, under 10
    minutes ago. Single use: it is deleted whatever the answer."""
    if not state or len(state) > 128:
        return False
    with SessionLocal() as db:
        row = db.get(OAuthState, _digest(state))
        if row is None:
            return False
        db.delete(row)
        _purge_states(db)
        db.commit()
        return row.connector == key and utcnow() - row.created_at <= STATE_TTL


def finish(connector, code: Optional[str], state: Optional[str]) -> None:
    """The browser is back from the authorization page: check the state, trade
    the code for a grant, store it."""
    if not consume_state(connector.key, state):
        raise OAuthError("This authorization is unknown, already used or older than 10 minutes: "
                         f"connect {connector.label} again.")
    if not code or len(code) > 2048:
        raise OAuthError(f"{connector.label} sent no authorization code back: connect again.")
    problem = connector.oauth_problem() or secretbox.problem()
    if problem:
        raise OAuthError(problem)
    with lock(connector.key):
        grant = Grant.from_response(connector.oauth_exchange(code), connector.label)
        save(connector.key, grant)


# ────────────────────────────────────────────────
# Using it
# ────────────────────────────────────────────────

def access_token(connector, force_refresh: bool = False) -> str:
    """An access token valid for at least a minute: the stored one, or a
    refreshed one (its new pair stored before this returns). `force_refresh`:
    the source refused the current one (a 401)."""
    label = connector.label
    with lock(connector.key):
        grant = load(connector.key, label)
        if grant is None:
            raise OAuthError(f"{label} is not connected: connect it on its page.")
        if force_refresh or grant.due() or not grant.access_token:
            grant = _refresh(connector, grant)
        return grant.access_token


def _refresh(connector, grant: Grant) -> Grant:
    label = connector.label
    if not grant.refresh_token:
        raise OAuthError(f"The {label} connection has no refresh token: {reconnect(label)}")
    try:
        answer = connector.oauth_refresh(grant.refresh_token)
    except GrantRevoked:
        # Dead for good: keeping it would only fail every run the same way.
        forget(connector.key)
        raise OAuthError(f"{label} refused the stored connection (invalid_grant): {reconnect(label)}") from None
    renewed = Grant.from_response(answer, label, refresh_token=grant.refresh_token, scope=grant.scope)
    save(connector.key, renewed)
    return renewed


def disconnect(connector) -> Optional[str]:
    """Revoke the grant at the source, then drop it here. Dropped whatever the
    source answers; returns what to tell the user when the source could not
    be told (None when it was)."""
    label = connector.label
    with lock(connector.key):
        warning = None
        try:
            grant = load(connector.key, label)
            if grant is not None:
                if grant.due() or not grant.access_token:
                    grant = _refresh(connector, grant)
                connector.oauth_revoke(grant.access_token)
        except (ConnectorError, GrantRevoked) as exc:
            warning = (f"Disconnected here, but {label} could not be told ({exc}). If Tributary is still "
                       f"listed among the apps of your {label} account, remove it there.")
        forget(connector.key)
        return warning
