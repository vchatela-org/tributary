"""Todoist (task manager): completed tasks, every completion (recurring
tasks included), karma and the productivity stats, from the Todoist API v1,
into TodoistStats.

Ported from a daily sync job, writing exactly its series (same
measurements, tags, fields, types and times), so a run rewrites that
history in place and never duplicates it (docs/porting.md). That job wrote
no `source` and no `user` tag: neither does this connector (Todoist is the
only writer of TodoistStats).

API (https://developer.todoist.com/api/v1/), auth `Authorization: Bearer <token>`:
  GET /user                      joined_at only: where a first or full run
                                 starts (the rest -- email, name, token -- is
                                 never read).
  GET /projects, /projects/archived, /sections   names, by id.
  GET /tasks/completed/by_completion_date?since=&until=
      completed tasks, at most 3 months per request: walked in 90-day
      windows. Recurring tasks never show up there (completing one only
      moves its due date).
  GET /activities?object_event_types=["item:completed"]&annotate_parents=true&date_from=&date_to=
      the activity log: one event per completion, recurring ones included,
      annotated with the project's name (which fills in deleted projects).
      Complete on a Pro plan; 7 days on a free one.
  GET /tasks/completed/stats     karma (last 7 days, daily), lifetime count,
                                 goals and streaks.
Lists are paged (`next_cursor`), each page turned into points as it comes
(the payloads, task descriptions included, are not kept). 429 and 5xx
answers are retried; a redirect is refused (the token would follow it).

Mapping (times in UTC seconds; "local" = TRIBUTARY_TZ):
  todoist_task_completed  one per completed task, at completed_at. Tags
                          task_id, project, section, priority (p1..p4 as in
                          the app: the API's 4 is p1), recurring, subtask
                          ("true"/"false"). Fields count=1, lead_time_hours
                          (creation -> completion), postponed_count,
                          duration_minutes, late_days (completion's local
                          date - due date; one-off tasks only).
  todoist_completion      one per activity event, at event_date. Tags
                          task_id, project. Field count=1.
  todoist_karma           karma (daily average), at each day's local midnight.
  todoist_stats           karma, completed_count, daily/weekly goal and
                          streaks, at today's local midnight: one point per
                          day, the last run of the day wins.
  With TRIBUTARY_TODOIST_TASK_TEXT, the task's text too, whole as the job
  wrote it: `content` (and `labels`, comma-joined) on
  todoist_task_completed, `content` on todoist_completion. Off by default:
  Tributary writes no free text unless asked (docs/schema.md).
Records sharing a key (same tags, same second: an activity on a window's
boundary comes with both windows) are merged, the later one's values
winning, as consecutive writes would (`last_wins`: the core would sum them).

Cursor: when the last run read up to. A run reads from then, minus
TRIBUTARY_TODOIST_LOOKBACK_DAYS (a task ticked off on a phone that synced
the next morning lands back in time), to now, and rewrites it: harmless,
the same points overwrite themselves. A first or full run reads the whole
history, from the account's creation. Append only: a completion deleted in
Todoist stays in InfluxDB.

Of the job's options: its "last N days" became the cursor plus the
lookback, its time zone TRIBUTARY_TZ (it fell back on the account's, which
this doesn't), the activity log is always read (it was on), the database
is always TodoistStats.
"""
import http.client
import json
from datetime import date, datetime, time, timedelta, timezone
from time import sleep
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.__version__ import __version__
from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Measurement, Point, epoch_seconds

PREFIX = "TRIBUTARY_TODOIST_"
API_URL = "https://api.todoist.com/api/v1"
TIMEOUT_SECONDS = 30
PAGE_LIMIT = 200
# /tasks/completed/by_completion_date takes at most 3 months per request
# (90-day windows, as the job's backfill walked every year since 2021).
WINDOW = timedelta(days=90)
# 429 and 5xx: retried after 2, 4, 8 s, or the Retry-After asked when
# longer, up to a minute: runs share one scheduler, a run that gives up is
# read again by the next.
RETRIES = 3
RETRY_AFTER_MAX = 60
_RETRYABLE = {429, 500, 502, 503, 504}
# Floor of a full run when the account's creation date is unknown.
TODOIST_EPOCH = datetime(2007, 1, 1, tzinfo=timezone.utc)
UNKNOWN_PROJECT = "(unknown)"
# Waits between retries (tests replace it).
_sleep = sleep

# The ported job's measurements, as it wrote them: no `source` nor `user`
# tag, the later of two records sharing a key winning, text whole.
_AS_THE_JOB = {"tagged_source": False, "tagged_user": False, "last_wins": True, "text_max": None}
TASK_COMPLETED = Measurement(
    database="TodoistStats", name="todoist_task_completed",
    tags=("task_id", "project", "section", "priority", "recurring", "subtask"),
    fields={"count": int, "content": str, "labels": str, "lead_time_hours": float, "postponed_count": int,
            "duration_minutes": int, "late_days": int},
    **_AS_THE_JOB,
)
COMPLETION = Measurement(
    database="TodoistStats", name="todoist_completion", tags=("task_id", "project"),
    fields={"count": int, "content": str}, **_AS_THE_JOB,
)
KARMA = Measurement(database="TodoistStats", name="todoist_karma", tags=(), fields={"karma": float}, **_AS_THE_JOB)
STATS = Measurement(
    database="TodoistStats", name="todoist_stats", tags=(),
    fields={"karma": float, "completed_count": int, "daily_goal": int, "weekly_goal": int,
            "current_daily_streak": int, "max_daily_streak": int, "current_weekly_streak": int,
            "max_weekly_streak": int},
    **_AS_THE_JOB,
)

GOALS = ("daily_goal", "weekly_goal")
STREAKS = ("current_daily_streak", "max_daily_streak", "current_weekly_streak", "max_weekly_streak")


class TodoistSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # An API token (Todoist: Settings > Integrations > Developer).
    token: Optional[SecretStr] = None
    # Days read again before the last run's end, for completions synced late.
    lookback_days: int = 3
    # Also write the tasks' text (content, labels): free text, off by default.
    task_text: bool = False


# ────────────────────────────────────────────────
# Mapping
# ────────────────────────────────────────────────

def parse_datetime(raw: Any) -> Optional[datetime]:
    """Todoist times are RFC 3339, usually with a `Z`: aware UTC, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: Any) -> Dict:
    return value if isinstance(value, dict) else {}


def _priority(raw: Any) -> str:
    """The API's priority 4 is the app's p1 (urgent), 1 is p4 (none)."""
    try:
        priority = int(raw)
    except (TypeError, ValueError):
        return ""
    return f"p{5 - priority}" if 1 <= priority <= 4 else ""


def _due_date(due: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(_dict(due).get("date") or "")[:10])
    except ValueError:
        return None


def _duration_minutes(duration: Any) -> Optional[int]:
    duration = _dict(duration)
    try:
        amount = int(duration.get("amount"))
    except (TypeError, ValueError):
        return None
    return amount * 1440 if duration.get("unit") == "day" else amount


def name_map(objects: List[Dict]) -> Dict[str, str]:
    return {str(o["id"]): o.get("name") or "" for o in objects if o.get("id")}


def project_names_from_events(events: List[Dict]) -> Dict[str, str]:
    """Project names carried by annotated activity events: they fill in the
    projects deleted since, which /projects no longer returns."""
    names: Dict[str, str] = {}
    for event in events:
        project_id = event.get("parent_project_id")
        name = _dict(event.get("extra_data")).get("parent_project_name")
        if project_id and name:
            names.setdefault(str(project_id), name)
    return names


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc)


def task_points(tasks: List[Dict], projects: Mapping[str, str], sections: Mapping[str, str], tz: ZoneInfo,
                text: bool) -> List[Point]:
    points = []
    for task in tasks:
        completed = parse_datetime(task.get("completed_at"))
        if completed is None or not task.get("id"):
            continue
        due = _dict(task.get("due"))
        recurring = bool(due.get("is_recurring"))
        fields: Dict[str, Any] = {"count": 1}
        if text:
            fields["content"] = task.get("content") or ""
            labels = task.get("labels")
            if labels and isinstance(labels, list):
                fields["labels"] = ",".join(sorted(str(label) for label in labels))
        added = parse_datetime(task.get("added_at"))
        if added is not None and added <= completed:
            fields["lead_time_hours"] = round((completed - added).total_seconds() / 3600, 2)
        if _int(task.get("postponed_count")) is not None:
            fields["postponed_count"] = task["postponed_count"]
        minutes = _duration_minutes(task.get("duration"))
        if minutes is not None:
            fields["duration_minutes"] = minutes
        due_date = _due_date(due)
        # A recurring task's due date has already moved on to the next occurrence.
        if due_date is not None and not recurring:
            fields["late_days"] = (completed.astimezone(tz).date() - due_date).days
        points.append(Point(TASK_COMPLETED, completed, {
            "task_id": str(task["id"]),
            "project": projects.get(str(task.get("project_id") or ""), UNKNOWN_PROJECT),
            "section": sections.get(str(task.get("section_id") or ""), ""),
            "priority": _priority(task.get("priority")),
            "recurring": "true" if recurring else "false",
            "subtask": "true" if task.get("parent_id") else "false",
        }, fields))
    return points


def slim_event(event: Dict, text: bool) -> Dict:
    """What an activity event's point needs, nothing else of it."""
    extra = _dict(event.get("extra_data"))
    kept = {"parent_project_name": extra.get("parent_project_name")}
    if text:
        kept["content"] = extra.get("content")
    return {"object_id": event.get("object_id"), "event_date": event.get("event_date"),
            "parent_project_id": event.get("parent_project_id"), "extra_data": kept}


def event_points(events: List[Dict], projects: Mapping[str, str], text: bool) -> List[Point]:
    points = []
    for event in events:
        moment = parse_datetime(event.get("event_date"))
        if moment is None or not event.get("object_id"):
            continue
        extra = _dict(event.get("extra_data"))
        fields: Dict[str, Any] = {"count": 1}
        if text:
            fields["content"] = extra.get("content") or ""
        points.append(Point(COMPLETION, moment, {
            "task_id": str(event["object_id"]),
            "project": projects.get(str(event.get("parent_project_id") or ""))
            or extra.get("parent_project_name") or UNKNOWN_PROJECT,
        }, fields))
    return points


def karma_points(stats: Dict, tz: ZoneInfo) -> List[Point]:
    """The daily karma average of the last days Todoist returns (7)."""
    points = []
    for entry in stats.get("karma_graph_data") or []:
        try:
            day = date.fromisoformat(str(entry.get("date"))[:10])
            karma = float(entry["karma_avg"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        points.append(Point(KARMA, local_midnight(day, tz), {}, {"karma": karma}))
    return points


def stats_fields(stats: Dict) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    karma = stats.get("karma")
    if isinstance(karma, (int, float)) and not isinstance(karma, bool):
        fields["karma"] = float(karma)
    if _int(stats.get("completed_count")) is not None:
        fields["completed_count"] = stats["completed_count"]
    goals = _dict(stats.get("goals"))
    for key in GOALS:
        if _int(goals.get(key)) is not None:
            fields[key] = goals[key]
    for key in STREAKS:
        count = _dict(goals.get(key)).get("count")
        if _int(count) is not None:
            fields[key] = count
    return fields


def windows(start: datetime, end: datetime, size: timedelta = WINDOW) -> Iterator[Tuple[datetime, datetime]]:
    """[start, end) in consecutive windows of at most `size`, oldest first."""
    cursor = start
    while cursor < end:
        upper = min(cursor + size, end)
        yield cursor, upper
        cursor = upper


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _NoRedirect(HTTPRedirectHandler):
    """A redirect is an error: urllib would send the token along, to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect)


# ────────────────────────────────────────────────
# Connector
# ────────────────────────────────────────────────

class TodoistConnector(Connector):
    key = "todoist"
    label = "Todoist"
    description = ("Completed tasks and every completion (recurring tasks included), karma and streaks, "
                   "from the Todoist API.")
    icon = "bi-check2-square"
    category = "Productivity"
    # Completions carry their own time: once a day is plenty. 00:30, the time
    # the CronJob this replaced ran at.
    default_schedule = "30 0 * * *"
    settings_class = TodoistSettings
    measurements = (TASK_COMPLETED, COMPLETION, KARMA, STATS)
    required_env = (f"{PREFIX}TOKEN",)

    def api_configured(self) -> bool:
        return bool(self.settings.token and self.settings.token.get_secret_value().strip())

    # -- API -----------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = API_URL.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self.settings.token.get_secret_value().strip()}",
                   "Accept": "application/json", "User-Agent": f"Tributary/{__version__}"}
        attempt = 0
        while True:
            retry_after: Optional[str] = None
            try:
                with _opener.open(Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 401:
                    raise ConnectorError(f"Todoist refused the token (401): check {PREFIX}TOKEN.") from None
                if exc.code == 403:
                    raise ConnectorError(f"Todoist refused {path} (403): check the token, and the plan "
                                         "(the activity log is a paid feature beyond 7 days).") from None
                if exc.code not in _RETRYABLE or attempt >= RETRIES:
                    raise ConnectorError(f"Todoist answered {exc.code} on {path}.") from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except URLError as exc:
                if attempt >= RETRIES:
                    raise ConnectorError(f"Todoist unreachable: {exc.reason}") from None
            except ValueError as exc:
                raise ConnectorError(f"Unreadable answer from Todoist on {path}: {type(exc).__name__}.") from None
            except (http.client.HTTPException, OSError) as exc:
                if attempt >= RETRIES:
                    raise ConnectorError(f"Connection to Todoist lost on {path}: {type(exc).__name__}.") from None
            attempt += 1
            delay = 2 ** attempt
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(int(retry_after), RETRY_AFTER_MAX))
            _sleep(delay)

    def _pages(self, path: str, key: str, params: Optional[Dict[str, Any]] = None) -> Iterator[List[Dict]]:
        """Each page's items, as it comes."""
        params = {**(params or {}), "limit": PAGE_LIMIT}
        seen = set()
        while True:
            data = self._get(path, params)
            if not isinstance(data, dict):
                raise ConnectorError(f"Unexpected answer from Todoist on {path}.")
            yield [item for item in data.get(key) or [] if isinstance(item, dict)]
            cursor = data.get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise ConnectorError(f"Todoist paged {path} in a loop.")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    def _all(self, path: str, key: str) -> List[Dict]:
        return [item for page in self._pages(path, key) for item in page]

    def _windows(self, path: str, key: str, since: datetime, until: datetime, params: Dict[str, Any],
                 bounds: Tuple[str, str]) -> Iterator[List[Dict]]:
        for lower, upper in windows(since, until):
            yield from self._pages(path, key, {**params, bounds[0]: _iso(lower), bounds[1]: _iso(upper)})

    def _joined_at(self) -> Optional[datetime]:
        return parse_datetime(_dict(self._get("/user")).get("joined_at"))

    # -- Sync ----------------------------------------------------------------

    def sync(self, cursor: Any) -> SyncResult:
        until = _dict(cursor).get("until")
        tz = ZoneInfo(app_settings.tz)
        now = datetime.now(timezone.utc)
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            last = min(datetime.fromtimestamp(until, timezone.utc), now)
            since = last - timedelta(days=max(0, self.settings.lookback_days))
        else:
            since = self._joined_at() or TODOIST_EPOCH
        text = self.settings.task_text

        projects = name_map(self._all("/projects", "results") + self._all("/projects/archived", "results"))
        sections = name_map(self._all("/sections", "results"))
        events = [slim_event(event, text) for page in self._windows(
            "/activities", "results", since, now,
            {"object_event_types": '["item:completed"]', "annotate_parents": "true"}, ("date_from", "date_to"))
            for event in page]
        # Live names win; the events' annotations only fill in deleted projects.
        projects = {**project_names_from_events(events), **projects}
        completions = event_points(events, projects, text)
        tasks: List[Point] = []
        for page in self._windows("/tasks/completed/by_completion_date", "items", since, now, {},
                                  ("since", "until")):
            tasks += task_points(page, projects, sections, tz, text)
        stats = self._get("/tasks/completed/stats")
        if not isinstance(stats, dict):
            raise ConnectorError("Unexpected answer from Todoist on /tasks/completed/stats.")

        points = tasks + completions + karma_points(stats, tz)
        today = stats_fields(stats)
        if today:
            points.append(Point(STATS, local_midnight(now.astimezone(tz).date(), tz), {}, today))

        def distinct(found: List[Point]) -> int:
            return len({(p.measurement.name, epoch_seconds(p.time), tuple(sorted(p.tags.items()))) for p in found})

        summary = [f"{distinct(tasks)} completed task(s) and {distinct(completions)} completion(s) "
                   f"since {since.astimezone(tz).date().isoformat()}"]
        return SyncResult(points=points, cursor={"until": int(now.timestamp())}, summary=summary)
