"""Audiobookshelf (self-hosted audiobook and podcast server): the API key
user's listening sessions and finished books, into MediaStats.

API (checked against Audiobookshelf 2.37), auth `Authorization: Bearer <API key>`:
  GET /api/me/listening-sessions?itemsPerPage=&page=
      paged, most recently updated first: paging stops at the first page
      that brings nothing newer than the cursor, so an hourly sync reads
      one page, not the whole history.
  GET /api/me                       mediaProgress: finished books.
  GET /api/libraries, GET /api/libraries/{id}/items?minified=1
      the books' current titles (read only when there is something to write).

Privacy: /api/me also returns the user's old non-expiring token and email,
and a session's deviceInfo carries an IP address. None of it is kept: each
response is reduced to the few values a point needs as soon as it is read,
and nothing of the payloads is stored -- the cursor holds two timestamps
(and the library ids of the few books finished without a date).

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
                   for an episode), at finishedAt, else lastUpdate -- then
                   once only, as lastUpdate moves with any later progress.
Times are epoch milliseconds, UTC.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Point
from app.schema import BOOK_FINISHED, READING_SESSION

SESSIONS_PAGE_SIZE = 100
ITEMS_PAGE_SIZE = 200
TIMEOUT_SECONDS = 30

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
    """The values a point needs, nothing else (no IP, no chapters, no ids
    beyond the library item's)."""
    meta = session.get("mediaMetadata") if isinstance(session.get("mediaMetadata"), dict) else {}
    return {
        "libraryItemId": session.get("libraryItemId"),
        "podcast": session.get("mediaType") == "podcast" or bool(session.get("episodeId")),
        "seconds": _number(session.get("timeListening")),
        "startedAt": session.get("startedAt"),
        "updatedAt": _number(session.get("updatedAt")),
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
        finished_at = _ms(progress.get("finishedAt"))
        books.append({
            "libraryItemId": progress["libraryItemId"],
            "time": finished_at or _ms(progress.get("lastUpdate")),
            # Without finishedAt the time is the last update, which moves:
            # such a book is written once only (see sync's cursor).
            "undated": finished_at is None,
            "lastUpdate": _number(progress.get("lastUpdate")),
        })
    return books


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

    # -- API -----------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.settings.url.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={"Authorization": f"Bearer {self.settings.token.get_secret_value()}",
                                        "Accept": "application/json"})
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ConnectorError(
                    f"Audiobookshelf refused the API key ({exc.code}): check TRIBUTARY_ABS_TOKEN.") from None
            raise ConnectorError(f"Audiobookshelf answered {exc.code} on {path}.") from None
        except URLError as exc:
            raise ConnectorError(f"Audiobookshelf unreachable ({self.settings.url}): {exc.reason}") from None
        except (TimeoutError, ValueError) as exc:
            raise ConnectorError(f"Unreadable answer from Audiobookshelf on {path}: {exc}") from None

    def _sessions_since(self, since: float) -> List[Dict]:
        """Sessions updated after `since` (epoch ms), slimmed."""
        sessions: List[Dict] = []
        page = 0
        while True:
            payload = self._get("/api/me/listening-sessions", {"itemsPerPage": SESSIONS_PAGE_SIZE, "page": page})
            if not isinstance(payload, dict):
                raise ConnectorError("Unexpected answer from Audiobookshelf on /api/me/listening-sessions.")
            batch = [_slim_session(s) for s in payload.get("sessions") or [] if isinstance(s, dict)]
            new = [s for s in batch if s["updatedAt"] > since]
            sessions.extend(new)
            page += 1
            if not new or page >= _number(payload.get("numPages")):
                return sessions

    def _titles(self, wanted: Set[str]) -> Dict[str, str]:
        """Library item id -> its current title, for the ids wanted (every
        library is read, page by page, until all are found)."""
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
                    media = item.get("media") if isinstance(item.get("media"), dict) else {}
                    meta = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
                    if item.get("id") in wanted and meta.get("title"):
                        titles[item["id"]] = str(meta["title"])
                page += 1
                if not results or page * ITEMS_PAGE_SIZE >= _number(payload.get("total")):
                    break
        return titles

    # -- Sync ----------------------------------------------------------------

    def sync(self, cursor: Any) -> SyncResult:
        cursor = cursor if isinstance(cursor, dict) else {}
        sessions_since = _number(cursor.get("sessions_updated_at"))
        progress_since = _number(cursor.get("progress_updated_at"))

        me = self._get("/api/me")
        if not isinstance(me, dict) or "id" not in me:
            raise ConnectorError("Unexpected answer from Audiobookshelf on /api/me.")
        progress = _finished_books(me)
        newest_progress = max([_number(p.get("lastUpdate")) for p in me.get("mediaProgress") or []
                               if isinstance(p, dict)] + [progress_since])
        del me  # holds the user's token and email: gone before anything else runs

        sessions = self._sessions_since(sessions_since)
        sessions_seen = max([s["updatedAt"] for s in sessions] + [sessions_since])
        listened = [s for s in sessions if s["seconds"] > 0 and _ms(s["startedAt"])]
        # Books finished without a finishedAt, already written at their
        # then last update: written again, they would count twice.
        counted = set(cursor.get("undated_finished") or [])
        finished = [p for p in progress if p["lastUpdate"] > progress_since and p["time"]
                    and not (p["undated"] and p["libraryItemId"] in counted)]

        wanted = {s["libraryItemId"] for s in listened if s["libraryItemId"]} | {p["libraryItemId"] for p in finished}
        titles = self._titles(wanted) if wanted else {}

        points: List[Point] = []
        for s in listened:
            points.append(Point(
                READING_SESSION, _ms(s["startedAt"]),
                tags={"kind": "podcast" if s["podcast"] else "audiobook", "device": s["device"]},
                fields={"minutes": round(s["seconds"] / 60, 3), "sessions": 1,
                        "title": titles.get(s["libraryItemId"]) or s["title"]},
            ))
        for p in finished:
            # A book no longer in any library still counts, untitled.
            points.append(Point(BOOK_FINISHED, p["time"], tags={"kind": "audiobook"},
                                fields={"count": 1, "title": titles.get(p["libraryItemId"])}))

        summary = []
        if sessions:
            summary.append(f"{len(listened)} new or updated session(s)"
                           + (f" ({len(sessions) - len(listened)} empty, skipped)" if len(sessions) > len(listened) else ""))
        if finished:
            summary.append(f"{len(finished)} finished book(s)")
        cursor = {"sessions_updated_at": sessions_seen, "progress_updated_at": newest_progress}
        undated = sorted(counted | {p["libraryItemId"] for p in finished if p["undated"]})
        if undated:
            cursor["undated_finished"] = undated
        return SyncResult(points=points, summary=summary, cursor=cursor)
