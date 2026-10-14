# Portions adapted from kryoseu/whoops (https://github.com/kryoseu/whoops),
# Apache License 2.0; modified. See NOTICE and LICENSES/Apache-2.0.txt.
"""A small client for the WHOOP developer API v2, standard library only.

Adapted from Whoops' `src/whoop.py` (kryoseu/whoops, `WhoopClient`): the same
endpoints, scopes, authorization URL, token requests (client id and secret in
the POST body; `scope=offline` on a refresh) and `nextToken` paging. What
changed: no `requests` (urllib, as the rest of Tributary), a timeout on every
request, redirects refused (a token or the client secret would follow them),
`limit=25` and an explicit `start`, 429 answers waited out from
`X-RateLimit-Reset`, 5xx retried, a 401 answered with one token refresh, and
no Flask: the caller hands the client id, secret and redirect URI in.

It imports nothing of Tributary's, so it could become a library of its own.
Errors carry a message meant for a UI, never a token, a secret or a body.

API (https://developer.whoop.com/api):
  authorize  https://api.prod.whoop.com/oauth/oauth2/auth
  token      https://api.prod.whoop.com/oauth/oauth2/token
  GET        /developer/v2/cycle, /recovery, /activity/sleep, /activity/workout
             ?limit=25&start=<ISO 8601>&nextToken=<from the last page>
  DELETE     /developer/v2/user/access   (revokes the grant)
Limits: 100 requests a minute, 10,000 a day (X-RateLimit-* headers, 429).
"""
import http.client
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ENDPOINT = "https://api.prod.whoop.com/"
API_VERSION = "developer/v2"
# The data Tributary reads, and `offline` for a refresh token. Never
# read:profile (name, email) nor read:body_measurement.
SCOPES = "offline read:recovery read:cycles read:sleep read:workout"
PAGE_LIMIT = 25
TIMEOUT_SECONDS = 30
# 429 and 5xx: retried after 2, 4, 8 s, or after X-RateLimit-Reset when longer.
# A reset further than RATE_LIMIT_WAIT_MAX away is the daily limit: the run
# stops, and the next one reads it again.
RETRIES = 3
RATE_LIMIT_WAIT_MAX = 90
_RETRYABLE = {429, 500, 502, 503, 504}
# An OAuth error code, as RFC 6749 spells them: anything else in an error body
# is not repeated.
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")
# Waits between retries (tests replace it).
_sleep = time.sleep

RESOURCES = {"cycle": "cycle", "recovery": "recovery", "sleep": "activity/sleep", "workout": "activity/workout"}


class WhoopError(Exception):
    """A request WHOOP refused or never answered."""


class Unauthorized(WhoopError):
    """401 on a data request: the access token is not (or no longer) valid."""


class InvalidGrant(WhoopError):
    """The token endpoint refused the code or the refresh token
    (`invalid_grant`): only a new authorization helps."""


class InvalidClient(WhoopError):
    """The token endpoint refused the app's client id or secret."""


class _NoRedirect(HTTPRedirectHandler):
    """A redirect is an error: urllib would send the token along, to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect)


def _url(path: str) -> str:
    return API_ENDPOINT.rstrip("/") + "/" + path.lstrip("/")


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The page that asks the member to grant access (Whoops'
    `authorization_url`, with the state given by the caller)."""
    query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                       "scope": SCOPES, "state": state})
    return f"{_url('oauth/oauth2/auth')}?{query}"


def _error_code(exc: HTTPError) -> Optional[str]:
    """The OAuth `error` of a refusal, when it is one."""
    try:
        data = json.loads(exc.read(4096) or b"{}")
    except (ValueError, OSError):
        return None
    code = data.get("error") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.match(code) else None


def _token_request(fields: Dict[str, str], user_agent: str) -> Dict[str, Any]:
    """POST to the token endpoint (Whoops' `_post_token_request`). Not
    retried: a refresh token is single use, and a lost answer is not worth a
    second try of a dead one."""
    request = Request(_url("oauth/oauth2/token"), data=urlencode(fields).encode("ascii"), method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                               "User-Agent": user_agent})
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except HTTPError as exc:
        code = _error_code(exc)
        if code == "invalid_grant":
            raise InvalidGrant("WHOOP refused the grant (invalid_grant).") from None
        if code == "invalid_client" or exc.code == 401:
            raise InvalidClient("WHOOP refused the app's credentials (invalid_client).") from None
        raise WhoopError(f"WHOOP's token endpoint answered {exc.code}" + (f" ({code})." if code else ".")) from None
    except URLError as exc:
        raise WhoopError(f"WHOOP unreachable: {exc.reason}") from None
    except ValueError as exc:
        raise WhoopError(f"Unreadable answer from WHOOP's token endpoint: {type(exc).__name__}.") from None
    except (http.client.HTTPException, OSError) as exc:
        raise WhoopError(f"Connection to WHOOP lost: {type(exc).__name__}.") from None
    if not isinstance(data, dict):
        raise WhoopError("Unexpected answer from WHOOP's token endpoint.")
    return data


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str,
                  user_agent: str = "whoop-api") -> Dict[str, Any]:
    """The token response for an authorization code (Whoops' `set_tokens`)."""
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                           "client_id": client_id, "client_secret": client_secret}, user_agent)


def refresh_token(client_id: str, client_secret: str, token: str, user_agent: str = "whoop-api") -> Dict[str, Any]:
    """The token response for a refresh token (Whoops' `refresh_token`). WHOOP
    rotates it: the answer's refresh token replaces this one, which is dead
    from now on."""
    return _token_request({"grant_type": "refresh_token", "refresh_token": token, "scope": "offline",
                           "client_id": client_id, "client_secret": client_secret}, user_agent)


def revoke_access(access_token: str, user_agent: str = "whoop-api") -> None:
    """DELETE /v2/user/access: the member's grant to this app ends."""
    request = Request(_url(f"{API_VERSION}/user/access"), method="DELETE",
                      headers={"Authorization": f"Bearer {access_token}", "User-Agent": user_agent})
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS):
            return
    except HTTPError as exc:
        raise WhoopError(f"WHOOP answered {exc.code} to the revocation.") from None
    except URLError as exc:
        raise WhoopError(f"WHOOP unreachable: {exc.reason}") from None
    except (http.client.HTTPException, OSError) as exc:
        raise WhoopError(f"Connection to WHOOP lost: {type(exc).__name__}.") from None


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rate_limit_wait(exc: HTTPError) -> Optional[int]:
    raw = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
    try:
        return max(0, int(float(raw))) if raw is not None else None
    except ValueError:
        return None


class Client:
    """The data endpoints. `token(force)` returns an access token: the
    current one, or a new one when `force` (WHOOP refused the last one)."""

    def __init__(self, token: Callable[[bool], str], user_agent: str = "whoop-api"):
        self._token = token
        self._user_agent = user_agent
        self._refreshed = False
        self.requests = 0

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        url = _url(path) + "?" + urlencode(params)
        attempt = 0
        while True:
            wait = None
            headers = {"Authorization": f"Bearer {self._token(False)}", "Accept": "application/json",
                       "User-Agent": self._user_agent}
            self.requests += 1
            try:
                with _opener.open(Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 401:
                    if self._refreshed:
                        raise Unauthorized(f"WHOOP refused the access token on /{path} (401), even refreshed.") \
                            from None
                    # Once per run: a token WHOOP invalidated early (a new
                    # authorization, a refresh elsewhere).
                    self._refreshed = True
                    self._token(True)
                    continue
                if exc.code == 403:
                    raise WhoopError(f"WHOOP refused /{path} (403): was its scope granted? Reconnect to grant "
                                     "it.") from None
                if exc.code not in _RETRYABLE or attempt >= RETRIES:
                    raise WhoopError(f"WHOOP answered {exc.code} on /{path}.") from None
                if exc.code == 429:
                    wait = _rate_limit_wait(exc)
                    if wait is not None and wait > RATE_LIMIT_WAIT_MAX:
                        raise WhoopError(f"WHOOP's rate limit is reached on /{path} for {wait // 60} more "
                                         "minute(s) (its daily limit?): the next run reads it again.") from None
            except URLError as exc:
                if attempt >= RETRIES:
                    raise WhoopError(f"WHOOP unreachable: {exc.reason}") from None
            except ValueError as exc:
                raise WhoopError(f"Unreadable answer from WHOOP on /{path}: {type(exc).__name__}.") from None
            except (http.client.HTTPException, OSError) as exc:
                if attempt >= RETRIES:
                    raise WhoopError(f"Connection to WHOOP lost on /{path}: {type(exc).__name__}.") from None
            attempt += 1
            _sleep(max(2 ** attempt, wait or 0))

    def pages(self, resource: str, start: Optional[datetime] = None) -> Iterator[List[Dict[str, Any]]]:
        """Each page of a collection, newest first (Whoops'
        `_fetch_paginated`): every record since `start` (inclusive), or the
        whole history without one."""
        path = f"{API_VERSION}/{RESOURCES[resource]}"
        params: Dict[str, Any] = {"limit": PAGE_LIMIT}
        if start is not None:
            params["start"] = iso(start)
        seen = set()
        while True:
            data = self._get(path, params)
            if not isinstance(data, dict):
                raise WhoopError(f"Unexpected answer from WHOOP on /{path}.")
            yield [record for record in data.get("records") or [] if isinstance(record, dict)]
            next_token = data.get("next_token") or data.get("nextToken")
            if not next_token:
                return
            if next_token in seen:
                raise WhoopError(f"WHOOP paged /{path} in a loop.")
            seen.add(next_token)
            params = {**params, "nextToken": next_token}

    def records(self, resource: str, start: Optional[datetime] = None) -> List[Dict[str, Any]]:
        return [record for page in self.pages(resource, start) for record in page]
