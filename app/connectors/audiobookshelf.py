"""Audiobookshelf (self-hosted audiobook and podcast server): the API key
user's listening sessions and finished books, into MediaStats.

API (checked against Audiobookshelf 2.37), auth `Authorization: Bearer <API key>`:
  GET /api/me/listening-sessions?itemsPerPage=&page=
      paged, most recently updated first. Paging stops at the first page
      with nothing updated within TRIBUTARY_ABS_LOOKBACK_DAYS of the newest
      update already synced: sessions listened offline in the mobile apps
      are uploaded later with the device's own update time, so they can
      land behind the newest one. The ids of the sessions in that window
      are kept, so a session is written again only when it changed.
  GET /api/me                       mediaProgress: finished books.
  POST /api/items/batch/get         the books' current titles, read only
      when there is something to write (or, when the server refuses that,
      GET /api/libraries and /api/libraries/{id}/items?minified=1).

Privacy: /api/me also returns the user's old non-expiring token and email,
and a session's deviceInfo carries an IP address. None of it is kept: each
response is reduced to the few values a point needs as soon as it is read,
and nothing of the payloads is stored. The cursor holds timestamps and
opaque ids: the recent sessions', and those of the few books finished
without a date (with the time chosen for them).

Mapping:
  reading_session  one per session, at its start. minutes = timeListening
                   (the seconds actually played, not end minus start, which
                   a seek inflates); kind = podcast when mediaType is
                   "podcast" or the session has an episodeId, else
                   audiobook; device = deviceName, else manufacturer +
                   model, else browser + system, else the client's name;
                   title = the book's current library title, or the show
                   for a podcast episode.
  book_finished    one per finished book (mediaProgress.isFinished, never
                   for an episode), at finishedAt, else at its lastUpdate
                   when first seen -- kept from then on, as lastUpdate moves
                   with any later progress. Whenever a finished book is new
                   or changed, all of them are written again: books finished
                   in the same second share a point (count summed), which a
                   lone rewrite would otherwise overwrite with count=1.
Times are epoch milliseconds, UTC.
"""
import http.client
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Point
from app.schema import BOOK_FINISHED, READING_SESSION

SESSIONS_PAGE_SIZE = 100
ITEMS_PAGE_SIZE = 200
BATCH_IDS = 100
TIMEOUT_SECONDS = 30
DAY_MS = 86_400_000

# What names a device, from a session's deviceInfo. Its ipAddress, ids and
# user id are never read.
_DEVICE_KEYS = ("deviceName", "manufacturer", "model", "browserName", "osName", "osVersion", "deviceType",
                "clientName", "clientVersion")


class AudiobookshelfSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix="TRIBUTARY_ABS_", env_file=".env", extra="ignore")

    # The server's base URL, e.g. http://audiobookshelf:80
    url: Optional[str] = None
    # An API key (Settings > API Keys in Audiobookshelf), for the user whose
    # listening is synced.
    token: Optional[SecretStr] = None
    # How far back a sync looks for sessions uploaded late by the mobile
    # apps (listened offline). Longer offline listening needs a full resync.
    lookback_days: int = 30


class ApiError(ConnectorError):
    """Audiobookshelf answered with an HTTP error."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _ms(value: Any) -> Optional[datetime]:
    """Epoch milliseconds -> aware UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def device_label(info: Any) -> Optional[str]:
    """The name a listener knows a device by: the app's device name, else
    its make and model (the mobile apps), else browser and system (the web
    player), else the client's name."""
    if not isinstance(info, dict):
        return None
    info = {k: info.get(k) for k in _DEVICE_KEYS if isinstance(info.get(k), str) and info.get(k)}
    name = info.get("deviceName") or " ".join(filter(None, (info.get("manufacturer"), info.get("model"))))
    if not name:
        name = " · ".join(filter(None, (info.get("browserName"), info.get("osName"))))
    return name or info.get("clientName") or None


def _slim_session(session: Dict) -> Dict:
    """The values a point needs, nothing else (no IP, no chapters)."""
    meta = session.get("mediaMetadata") if isinstance(session.get("mediaMetadata"), dict) else {}
    return {
        "id": str(session.get("id") or ""),
        "libraryItemId": session.get("libraryItemId"),
        "podcast": session.get("mediaType") == "podcast" or bool(session.get("episodeId")),
        "seconds": _number(session.get("timeListening")),
        "startedAt": session.get("startedAt"),
        "updatedAt": int(_number(session.get("updatedAt"))),
        "title": meta.get("title") or session.get("displayTitle"),
        "device": device_label(session.get("deviceInfo")),
    }


def _finished_books(me: Dict) -> List[Dict]:
    """Finished books of /api/me's mediaProgress, reduced to what a point
    needs: the rest of /api/me (token, email...) is never kept."""
    books = []
    for progress in me.get("mediaProgress") or []:
        if not isinstance(progress, dict) or not progress.get("isFinished") or progress.get("episodeId"):
            continue
        if progress.get("mediaItemType") not in (None, "book") or not progress.get("libraryItemId"):
            continue
        books.append({
            "libraryItemId": str(progress["libraryItemId"]),
            "finishedAt": int(_number(progress.get("finishedAt"))) or None,
            "lastUpdate": int(_number(progress.get("lastUpdate"))),
        })
    return books


def _title_of(item: Any) -> Optional[str]:
    media = item.get("media") if isinstance(item, dict) and isinstance(item.get("media"), dict) else {}
    meta = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
    return str(meta["title"]) if meta.get("title") else None


class AudiobookshelfConnector(Connector):
    key = "audiobookshelf"
    label = "Audiobookshelf"
    description = "Listening sessions (audiobooks and podcasts) and finished books, from the API."
    icon = "bi-headphones"
    settings_class = AudiobookshelfSettings
    measurements = (READING_SESSION, BOOK_FINISHED)
    required_env = ("TRIBUTARY_ABS_URL", "TRIBUTARY_ABS_TOKEN")

    def api_configured(self) -> bool:
        return bool(self.settings.url and self.settings.token and self.settings.token.get_secret_value())

    def full_cursor(self, cursor: Any) -> Any:
        """A full resync reads everything again, but keeps the times chosen
        for undated finished books: chosen anew, they would count twice."""
        undated = cursor.get("undated_finished") if isinstance(cursor, dict) else None
        return {"undated_finished": undated} if undated else None

    # -- API -----------------------------------------------------------------

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        url = self.settings.url.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self.settings.token.get_secret_value()}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if urlparse(url).scheme not in ("http", "https"):
            raise ConnectorError(f"Invalid TRIBUTARY_ABS_URL ({self.settings.url}): "
                                 "it must start with http:// or https://.")
        request = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 401:
                raise ApiError("Audiobookshelf refused the API key (401): check TRIBUTARY_ABS_TOKEN.", 401) from None
            if exc.code == 403:
                raise ApiError(f"Audiobookshelf refused {path} (403): check the API key's user and its "
                               "permissions.", 403) from None
            raise ApiError(f"Audiobookshelf answered {exc.code} on {path}.", exc.code) from None
        except URLError as exc:
            raise ConnectorError(f"Audiobookshelf unreachable ({self.settings.url}): {exc.reason}") from None
        except (TimeoutError, ValueError) as exc:
            raise ConnectorError(f"Unreadable answer from Audiobookshelf on {path}: {exc}") from None
        except (http.client.HTTPException, OSError) as exc:
            raise ConnectorError(f"Connection to Audiobookshelf lost on {path}: "
                                 f"{type(exc).__name__} {exc}".rstrip()) from None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params)

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("POST", path, body=body)

    def _sessions(self, cursor: Dict) -> Tuple[List[Dict], Dict[str, Any]]:
        """(sessions new or changed since the cursor, slimmed; the cursor's
        new session part)."""
        since = int(_number(cursor.get("sessions_updated_at")))
        recent = cursor.get("recent_sessions") if isinstance(cursor.get("recent_sessions"), dict) else {}
        window = max(0, self.settings.lookback_days) * DAY_MS
        horizon = since - window if since else None
        changed: List[Dict] = []
        seen: Dict[str, int] = {}
        page = 0
        while True:
            payload = self._get("/api/me/listening-sessions", {"itemsPerPage": SESSIONS_PAGE_SIZE, "page": page})
            if not isinstance(payload, dict):
                raise ConnectorError("Unexpected answer from Audiobookshelf on /api/me/listening-sessions.")
            in_window = False
            for raw in payload.get("sessions") or []:
                if not isinstance(raw, dict):
                    continue
                session = _slim_session(raw)
                if horizon is not None and session["updatedAt"] <= horizon:
                    continue
                in_window = True
                seen[session["id"]] = session["updatedAt"]
                if recent.get(session["id"]) != session["updatedAt"]:
                    changed.append(session)
            page += 1
            if not in_window or page >= _number(payload.get("numPages")):
                break
        newest = max([since] + [s["updatedAt"] for s in changed])
        keep_after = newest - window
        recent = {sid: at for sid, at in {**recent, **seen}.items() if isinstance(at, (int, float)) and at > keep_after}
        return changed, {"sessions_updated_at": newest, "recent_sessions": recent}

    def _titles(self, wanted: Set[str]) -> Dict[str, str]:
        """Library item id -> its current title, for the ids wanted."""
        if not wanted:
            return {}
        try:
            return self._titles_by_id(sorted(wanted))
        except ApiError as exc:
            # One item the user may no longer open fails the whole batch, and
            # an older server may not have it: read the libraries instead.
            if exc.code == 401:
                raise
            return self._titles_by_library(wanted)

    def _titles_by_id(self, wanted: List[str]) -> Dict[str, str]:
        titles: Dict[str, str] = {}
        for start in range(0, len(wanted), BATCH_IDS):
            payload = self._post("/api/items/batch/get", {"libraryItemIds": wanted[start:start + BATCH_IDS]})
            for item in (payload.get("libraryItems") if isinstance(payload, dict) else None) or []:
                title = _title_of(item)
                if title and item.get("id"):
                    titles[str(item["id"])] = title
        return titles

    def _titles_by_library(self, wanted: Set[str]) -> Dict[str, str]:
        """Every library, page by page, until all the ids wanted are found."""
        titles: Dict[str, str] = {}
        libraries = self._get("/api/libraries")
        for library in (libraries.get("libraries") if isinstance(libraries, dict) else None) or []:
            if not isinstance(library, dict) or not library.get("id"):
                continue
            page = 0
            while wanted - set(titles):
                payload = self._get(f"/api/libraries/{quote(str(library['id']), safe='')}/items",
                                    {"limit": ITEMS_PAGE_SIZE, "page": page, "minified": 1})
                results = [i for i in (payload.get("results") if isinstance(payload, dict) else None) or []
                           if isinstance(i, dict)]
                for item in results:
                    title = _title_of(item)
                    if item.get("id") in wanted and title:
                        titles[item["id"]] = title
                page += 1
                if not results or page * ITEMS_PAGE_SIZE >= _number(payload.get("total")):
                    break
        return titles

    # -- Sync ----------------------------------------------------------------

    def sync(self, cursor: Any) -> SyncResult:
        cursor = cursor if isinstance(cursor, dict) else {}
        progress_since = int(_number(cursor.get("progress_updated_at")))
        undated = cursor.get("undated_finished") if isinstance(cursor.get("undated_finished"), dict) else {}
        undated = dict(undated)

        me = self._get("/api/me")
        if not isinstance(me, dict) or "id" not in me:
            raise ConnectorError("Unexpected answer from Audiobookshelf on /api/me.")
        finished = _finished_books(me)
        newest_progress = max([progress_since] + [int(_number(p.get("lastUpdate")))
                                                  for p in me.get("mediaProgress") or [] if isinstance(p, dict)])
        del me  # holds the user's token and email: gone before anything else runs

        sessions, session_cursor = self._sessions(cursor)
        listened = [s for s in sessions if s["seconds"] > 0 and _ms(s["startedAt"])]
        changed_books = [b for b in finished if b["lastUpdate"] > progress_since]
        # All finished books, or none: see book_finished in the module docstring.
        books = finished if changed_books else []

        wanted = {s["libraryItemId"] for s in listened if s["libraryItemId"]} | {b["libraryItemId"] for b in books}
        titles = self._titles(wanted)

        points: List[Point] = []
        for s in listened:
            points.append(Point(
                READING_SESSION, _ms(s["startedAt"]),
                tags={"kind": "podcast" if s["podcast"] else "audiobook", "device": s["device"]},
                fields={"minutes": round(s["seconds"] / 60, 3), "sessions": 1,
                        "title": titles.get(s["libraryItemId"]) or s["title"]},
            ))
        for book in books:
            item_id = book["libraryItemId"]
            at = undated.get(item_id) or book["finishedAt"]
            if not at:
                # No date: its last update now, and that for good.
                at = undated[item_id] = book["lastUpdate"]
            if not _ms(at):
                continue
            # A book no longer in any library still counts, untitled.
            points.append(Point(BOOK_FINISHED, _ms(at), tags={"kind": "audiobook"},
                                fields={"count": 1, "title": titles.get(item_id)}))

        summary = []
        if sessions:
            summary.append(f"{len(listened)} new or updated session(s)"
                           + (f" ({len(sessions) - len(listened)} empty, skipped)" if len(sessions) > len(listened) else ""))
        if changed_books:
            summary.append(f"{len(changed_books)} finished book(s) new or changed"
                           + (f" ({len(books)} finished books written)" if len(books) > len(changed_books) else ""))
        new_cursor = {**session_cursor, "progress_updated_at": newest_progress}
        if undated:
            new_cursor["undated_finished"] = undated
        return SyncResult(points=points, summary=summary, cursor=new_cursor)
