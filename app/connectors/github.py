"""GitHub: who wrote the commits (you, an agent, a bot), the workflow runs
and how they ended, and the minutes every job spent on a GitHub-hosted or a
self-hosted runner, from the GitHub API, into DevStats.

API (https://docs.github.com/en/rest, https://docs.github.com/en/graphql),
auth `Authorization: Bearer <token>`:
  POST /graphql  viewer { login }: whose commits are `self`.
                 repository.defaultBranchRef...history(since:): the default
                 branch's commits, 100 a call, with their authors (the
                 `Co-authored-by` trailers included), additions and
                 deletions. Never their message.
  GET /users/{owner}                    an organisation or a user.
  GET /orgs/{owner}/repos, /user/repos (the token's own), /users/{owner}/repos
                                        the repositories: name, visibility.
  GET /repos/{o}/{r}/actions/workflows  the workflows' names, by id.
  GET /repos/{o}/{r}/actions/runs?created=<a UTC day>
                                        the runs created that day.
  GET /repos/{o}/{r}/actions/runs/{id}/jobs?filter=all
                                        its jobs, every attempt: the only
                                        place that says which runner ran one.
Lists are paged (`page`, 100 at a time). 5xx and a secondary rate limit
(403/429 with Retry-After) are retried; a redirect is refused (the token would
follow it).

Mapping (times in UTC seconds):
  github_commit        one per commit of a repository's default branch (merge
                       commits left out), at its author's time, local time
                       added. Tags repository, author: `agent` when one of its
                       authors, the git author or a Co-authored-by trailer, is
                       an agent (TRIBUTARY_GITHUB_AGENTS: Claude, Copilot and
                       OpenHands by default), else, by its git author, `bot`
                       (a `[bot]` or `-bot` account), `self` (the token's own
                       account) or `other` (an email no account claims
                       included). Fields count=1, additions, deletions.
  github_workflow_run  one per run, at its creation (which a re-run keeps, so
                       a re-run overwrites it). Tags repository, workflow,
                       event, visibility. Fields count=1, succeeded, failed
                       (failure, timed out or never started), cancelled (each
                       1 or 0), attempts, duration_s (start of its last
                       attempt to its last update).
  github_job           one per job that ran (skipped ones left out), every
                       attempt, at its start. Tags repository, workflow, job,
                       runner (`github-hosted`, `self-hosted`), runner_label
                       (its labels, comma-joined), conclusion, visibility.
                       Fields count=1, duration_s, queued_s (created to
                       started), billable_min: a GitHub-hosted job of a
                       private repository, rounded up to the minute and
                       multiplied as GitHub counts it (Windows 2, macOS 10),
                       0 otherwise (public repositories and self-hosted
                       runners are free).
The workflow's name is read from the workflow list, never from the run (whose
`name` is its run-name, which may carry a pull request's title). No commit
message, title, branch or login is written.

Cursor: when the last run read up to, commits and Actions apart. Commits are
read from then, minus TRIBUTARY_GITHUB_LOOKBACK_DAYS, and the whole history on
a first or full run (100 a call: cheap). Actions cost a call per run for its
jobs, so a first or full run reads TRIBUTARY_GITHUB_HISTORY_DAYS, a UTC day at
a time, oldest first, and stops after TRIBUTARY_GITHUB_MAX_RUNS runs or when
the token's hourly quota runs low: the cursor then keeps the day it reached,
and the next run carries on from it. A run still in progress is read again by
the next run (the lookback). Append only: a deleted run stays in InfluxDB.
"""
import http.client
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import sleep
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app.__version__ import __version__
from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_GITHUB_"
API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
PER_PAGE = 100
# 5xx and secondary rate limits: retried after 2, 4, 8 s, or the Retry-After
# asked when longer, up to a minute: runs share one scheduler, a run that gives
# up is read again by the next.
RETRIES = 3
RETRY_AFTER_MAX = 60
_RETRYABLE = {500, 502, 503, 504}
# Calls of the hourly quota (5,000 for a token) left for the UI's other runs:
# below it, a run stops reading Actions and carries on next time.
QUOTA_RESERVE = 200
DAY = timedelta(days=1)
# Conclusions counted as failed: the run did not do its job.
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
# How GitHub counts a hosted runner's minute against a plan's included minutes.
MULTIPLIERS = (("windows", 2), ("macos", 10))
# Waits between retries, and the clock (tests replace them).
_sleep = sleep


def _now() -> datetime:
    return datetime.now(timezone.utc)


DATABASE = "DevStats"
COMMIT = Measurement(
    database=DATABASE, name="github_commit", tags=("repository", "author"),
    fields={"count": int, "additions": int, "deletions": int},
    local_time=True,
)
WORKFLOW_RUN = Measurement(
    database=DATABASE, name="github_workflow_run", tags=("repository", "workflow", "event", "visibility"),
    fields={"count": int, "succeeded": int, "failed": int, "cancelled": int, "attempts": int, "duration_s": int},
)
JOB = Measurement(
    database=DATABASE, name="github_job",
    tags=("repository", "workflow", "job", "runner", "runner_label", "conclusion", "visibility"),
    fields={"count": int, "duration_s": int, "queued_s": int, "billable_min": int},
)

COMMITS_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, since: $since, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              authoredDate additions deletions
              parents { totalCount }
              author { email user { login } }
              authors(first: 10) { nodes { email user { login } } }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # A token: fine-grained, read-only (Actions, Contents, Metadata) on the owner
    # it reads; a classic one reads several owners but needs the `repo` scope.
    token: Optional[SecretStr] = None
    # The organisations or users whose repositories it reads, comma-separated.
    owners: str = ""
    # Commit authors that are agents, as a co-author or the author: emails or
    # GitHub logins, comma-separated (Claude, GitHub Copilot, OpenHands).
    agents: str = "noreply@anthropic.com,Copilot,openhands-agent"
    # Days read again before the last run's end, for runs still in progress.
    lookback_days: int = 3
    # Days of Actions history a first or full run reads (a call per run).
    history_days: int = 30
    # Runs whose jobs one sync reads at most: a longer backfill goes on next run.
    max_runs: int = 3000


# ────────────────────────────────────────────────
# Mapping
# ────────────────────────────────────────────────

@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    private: bool

    @property
    def visibility(self) -> str:
        return "private" if self.private else "public"


def parse_datetime(raw: Any) -> Optional[datetime]:
    """GitHub times are ISO 8601 with a `Z`: aware UTC, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _dict(value: Any) -> Dict:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _names(raw: str) -> FrozenSet[str]:
    """A comma-separated list, lower-cased: emails and logins alike."""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _login(person: Any) -> str:
    return str(_dict(_dict(person).get("user")).get("login") or "")


def commit_author(commit: Mapping, me: str, agents: FrozenSet[str]) -> str:
    """`agent` when an agent is among its authors (the git author or a
    Co-authored-by trailer), by email or login; else `bot`, `self` or `other`
    by its git author."""
    author = _dict(commit.get("author"))
    people = [author] + [_dict(n) for n in _dict(commit.get("authors")).get("nodes") or []]
    if any({str(p.get("email") or "").lower(), _login(p).lower()} & agents for p in people):
        return "agent"
    login, email = _login(author).lower(), str(author.get("email") or "").lower()
    if login.endswith(("[bot]", "-bot")) or email.endswith("[bot]@users.noreply.github.com"):
        return "bot"
    return "self" if me and login.lower() == me.lower() else "other"


def commit_points(commits: List[Dict], repo: Repo, me: str, agents: FrozenSet[str]) -> List[Point]:
    points = []
    for commit in commits:
        authored = parse_datetime(commit.get("authoredDate"))
        if authored is None or _int(_dict(commit.get("parents")).get("totalCount")) > 1:
            continue
        points.append(Point(COMMIT, authored, {"repository": repo.name, "author": commit_author(commit, me, agents)},
                            {"count": 1, "additions": _int(commit.get("additions")),
                             "deletions": _int(commit.get("deletions"))}))
    return points


def workflow_name(run: Mapping, workflows: Mapping[int, str]) -> str:
    """The workflow's own name; its file's when the list lacks it."""
    name = workflows.get(_int(run.get("workflow_id")))
    if name:
        return name
    path = str(run.get("path") or "")
    return path.rsplit("/", 1)[-1] or "(unknown)"


def run_point(run: Mapping, repo: Repo, workflow: str) -> Optional[Point]:
    created = parse_datetime(run.get("created_at"))
    if created is None:
        return None
    conclusion = run.get("conclusion")
    fields: Dict[str, Any] = {"count": 1, "succeeded": int(conclusion == "success"),
                              "failed": int(conclusion in FAILED), "cancelled": int(conclusion == "cancelled"),
                              "attempts": max(1, _int(run.get("run_attempt")))}
    started, updated = parse_datetime(run.get("run_started_at")), parse_datetime(run.get("updated_at"))
    if started is not None and updated is not None and updated >= started:
        fields["duration_s"] = int((updated - started).total_seconds())
    return Point(WORKFLOW_RUN, created, {"repository": repo.name, "workflow": workflow,
                                         "event": str(run.get("event") or ""), "visibility": repo.visibility},
                 fields)


def billable_minutes(seconds: int, labels: List[str], hosted: bool, private: bool) -> int:
    """GitHub rounds each hosted job up to the minute and counts Windows twice,
    macOS ten times; public repositories and self-hosted runners are free."""
    if not (hosted and private):
        return 0
    multiplier = next((m for prefix, m in MULTIPLIERS if any(label.lower().startswith(prefix) for label in labels)), 1)
    return math.ceil(seconds / 60) * multiplier


def job_points(jobs: List[Dict], repo: Repo, workflow: str) -> List[Point]:
    points = []
    for job in jobs:
        if job.get("status") != "completed" or job.get("conclusion") in (None, "skipped"):
            continue
        started, completed = parse_datetime(job.get("started_at")), parse_datetime(job.get("completed_at"))
        if started is None or completed is None:
            continue
        created = parse_datetime(job.get("created_at"))
        labels = [str(label) for label in job.get("labels") or []]
        ran = bool(job.get("runner_name"))
        hosted = ran and (job.get("runner_group_id") == 0 or job.get("runner_group_name") == "GitHub Actions"
                          or str(job.get("runner_name")).startswith("GitHub Actions "))
        duration = max(0, int((completed - started).total_seconds()))
        fields = {"count": 1, "duration_s": duration,
                  "billable_min": billable_minutes(duration, labels, hosted, repo.private)}
        if created is not None and started >= created:
            fields["queued_s"] = int((started - created).total_seconds())
        points.append(Point(JOB, started, {
            "repository": repo.name, "workflow": workflow, "job": str(job.get("name") or ""),
            "runner": ("github-hosted" if hosted else "self-hosted") if ran else "",
            "runner_label": ",".join(labels), "conclusion": str(job.get("conclusion")),
            "visibility": repo.visibility,
        }, fields))
    return points


def utc_days(start: datetime, end: datetime) -> Iterator[datetime]:
    """The UTC midnights of the days from start's to end's, both included."""
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        yield day
        day += DAY


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    return None


class _NoRedirect(HTTPRedirectHandler):
    """A redirect is an error: urllib would send the token along, to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect)


class _Stop(Exception):
    """The run's budget for Actions is spent: carry on next run."""


# ────────────────────────────────────────────────
# Connector
# ────────────────────────────────────────────────

class GitHubConnector(Connector):
    key = "github"
    label = "GitHub"
    description = ("Who wrote the commits (you, an agent, a bot), the workflow runs and their failures, "
                   "and the minutes each job spent on GitHub-hosted or self-hosted runners.")
    icon = "bi-github"
    category = "Productivity"
    # Commits and runs carry their own time: once a day, at night, when few
    # runs are still in progress.
    default_schedule = "15 4 * * *"
    settings_class = GitHubSettings
    measurements = (COMMIT, WORKFLOW_RUN, JOB)
    required_env = (f"{PREFIX}TOKEN", f"{PREFIX}OWNERS")

    def __init__(self) -> None:
        super().__init__()
        self._quota: Optional[int] = None

    def api_configured(self) -> bool:
        return bool(self.settings.token and self.settings.token.get_secret_value().strip() and self._owners())

    def _owners(self) -> List[str]:
        return [owner.strip() for owner in self.settings.owners.split(",") if owner.strip()]

    # -- API -----------------------------------------------------------------

    def _call(self, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict] = None) -> Any:
        url = API_URL.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self.settings.token.get_secret_value().strip()}",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": f"Tributary/{__version__}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            retry_after: Optional[str] = None
            try:
                with _opener.open(Request(url, data=data, headers=headers), timeout=TIMEOUT_SECONDS) as response:
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    if remaining and remaining.isdigit() and path != "/graphql":
                        self._quota = int(remaining)
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 401:
                    raise ConnectorError(f"GitHub refused the token (401): check {PREFIX}TOKEN.") from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                limited = exc.code == 429 or (exc.code == 403 and (retry_after or (
                    exc.headers and exc.headers.get("X-RateLimit-Remaining") == "0")))
                if limited and not retry_after:
                    raise ConnectorError("GitHub's hourly quota for this token is spent: the next run carries on.") \
                        from None
                if exc.code == 403 and not limited:
                    raise ConnectorError(f"GitHub refused {path} (403): the token cannot read it "
                                         "(fine-grained: Actions, Contents and Metadata, read).") from None
                if exc.code == 404:
                    raise ConnectorError(f"GitHub has no {path} (404), or the token cannot see it.") from None
                if not (limited or exc.code in _RETRYABLE) or attempt >= RETRIES:
                    raise ConnectorError(f"GitHub answered {exc.code} on {path}.") from None
            except URLError as exc:
                if attempt >= RETRIES:
                    raise ConnectorError(f"GitHub unreachable: {exc.reason}") from None
            except ValueError as exc:
                raise ConnectorError(f"Unreadable answer from GitHub on {path}: {type(exc).__name__}.") from None
            except (http.client.HTTPException, OSError) as exc:
                if attempt >= RETRIES:
                    raise ConnectorError(f"Connection to GitHub lost on {path}: {type(exc).__name__}.") from None
            attempt += 1
            delay = 2 ** attempt
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(int(retry_after), RETRY_AFTER_MAX))
            _sleep(delay)

    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict:
        answer = _dict(self._call("/graphql", body={"query": query, "variables": variables}))
        errors = answer.get("errors")
        if errors:
            message = str(_dict(errors[0]).get("message") or "an error") if isinstance(errors, list) else "an error"
            raise ConnectorError(f"GitHub's GraphQL API answered: {message[:200]}")
        return _dict(answer.get("data"))

    def _pages(self, path: str, key: Optional[str], params: Optional[Dict[str, Any]] = None) -> Iterator[List[Dict]]:
        """Each page's items (`key` of an object, or the list itself), as it comes."""
        page = 1
        while True:
            data = self._call(path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            items = _dict(data).get(key) if key else data
            if not isinstance(items, list):
                raise ConnectorError(f"Unexpected answer from GitHub on {path}.")
            yield [item for item in items if isinstance(item, dict)]
            if len(items) < PER_PAGE:
                return
            page += 1

    def _repos(self, me: str) -> List[Repo]:
        repos: Dict[Tuple[str, str], Repo] = {}
        for owner in self._owners():
            kind = _dict(self._call(f"/users/{quote(owner)}")).get("type")
            if kind == "Organization":
                path, params = f"/orgs/{quote(owner)}/repos", {"type": "all"}
            elif me and owner.lower() == me.lower():
                path, params = "/user/repos", {"affiliation": "owner", "visibility": "all"}
            else:
                path, params = f"/users/{quote(owner)}/repos", {"type": "owner"}
            for page in self._pages(path, None, params):
                for item in page:
                    name = str(item.get("name") or "")
                    # A fork's history is mostly its upstream's.
                    if name and not item.get("fork"):
                        visibility = item.get("visibility")
                        private = visibility != "public" if visibility else bool(item.get("private"))
                        repos[(owner.lower(), name)] = Repo(owner, name, private)
        return sorted(repos.values(), key=lambda r: (r.owner.lower(), r.name.lower()))

    def _commits(self, repo: Repo, since: Optional[datetime]) -> Iterator[List[Dict]]:
        after = None
        while True:
            data = self._graphql(COMMITS_QUERY, {"owner": repo.owner, "name": repo.name,
                                                 "since": _iso(since) if since else None, "after": after})
            ref = _dict(_dict(data.get("repository")).get("defaultBranchRef"))
            history = _dict(_dict(ref.get("target")).get("history"))
            yield [_dict(node) for node in history.get("nodes") or []]
            info = _dict(history.get("pageInfo"))
            if not info.get("hasNextPage") or not info.get("endCursor"):
                return
            after = info["endCursor"]

    def _workflows(self, repo: Repo) -> Dict[int, str]:
        names: Dict[int, str] = {}
        for page in self._pages(f"/repos/{quote(repo.owner)}/{quote(repo.name)}/actions/workflows", "workflows"):
            names.update({_int(w.get("id")): str(w.get("name") or "") for w in page if _int(w.get("id"))})
        return names

    # -- Sync ----------------------------------------------------------------

    def _actions(self, repos: List[Repo], since: datetime, now: datetime) -> Tuple[List[Point], datetime, Dict]:
        """Runs and jobs, a UTC day at a time from since's day: the points, how
        far it got (now, or the day it stopped at), and what it read."""
        points: List[Point] = []
        read = {"runs": 0, "jobs": 0, "in_progress": 0}
        workflows: Dict[Repo, Dict[int, str]] = {}
        budget = max(0, self.settings.max_runs)
        try:
            for day in utc_days(since, now):
                for repo in repos:
                    if self._quota is not None and self._quota < QUOTA_RESERVE:
                        raise _Stop(day)
                    base = f"/repos/{quote(repo.owner)}/{quote(repo.name)}/actions/runs"
                    created = f"{_iso(day)}..{_iso(day + DAY - timedelta(seconds=1))}"
                    for page in self._pages(base, "workflow_runs", {"created": created}):
                        for run in page:
                            if run.get("status") != "completed":
                                read["in_progress"] += 1
                                continue
                            if read["runs"] >= budget:
                                raise _Stop(day)
                            if repo not in workflows:
                                workflows[repo] = self._workflows(repo)
                            name = workflow_name(run, workflows[repo])
                            point = run_point(run, repo, name)
                            if point is not None:
                                points.append(point)
                            read["runs"] += 1
                            for jobs in self._pages(f"{base}/{_int(run.get('id'))}/jobs", "jobs", {"filter": "all"}):
                                found = job_points(jobs, repo, name)
                                points += found
                                read["jobs"] += len(found)
        except _Stop as stop:
            return points, stop.args[0], read
        return points, now, read

    def sync(self, cursor: Any) -> SyncResult:
        cursor = _dict(cursor)
        self._quota = None
        tz = ZoneInfo(app_settings.tz)
        now = _now()
        lookback = timedelta(days=max(0, self.settings.lookback_days))
        me = str(_dict(self._graphql("query { viewer { login } }", {}).get("viewer")).get("login") or "")
        agents = _names(self.settings.agents)
        repos = self._repos(me)

        commits_until = _epoch(cursor.get("commits_until"))
        commits_since = min(commits_until, now) - lookback if commits_until else None
        commits: List[Point] = []
        for repo in repos:
            for page in self._commits(repo, commits_since):
                commits += commit_points(page, repo, me, agents)

        runs_until = _epoch(cursor.get("runs_until"))
        if runs_until is None:
            runs_since = now - timedelta(days=max(0, self.settings.history_days))
        elif cursor.get("runs_partial"):
            runs_since = min(runs_until, now)
        else:
            runs_since = min(runs_until, now) - lookback
        actions, reached, read = self._actions(repos, runs_since, now)
        partial = reached < now

        authors: Dict[str, int] = {}
        for point in commits:
            authors[point.tags["author"]] = authors.get(point.tags["author"], 0) + 1
        by_author = ", ".join(f"{count} {name}" for name, count in sorted(authors.items()))
        summary = [f"{len(commits)} commit(s) in {len(repos)} repositories"
                   + (f" ({by_author})" if by_author else "")
                   + (f" since {commits_since.astimezone(tz).date().isoformat()}" if commits_since else ""),
                   f"{read['runs']} workflow run(s), {read['jobs']} job(s) "
                   f"since {runs_since.astimezone(tz).date().isoformat()}"]
        if read["in_progress"]:
            summary.append(f"{read['in_progress']} run(s) still in progress, read again next run")
        if partial:
            summary.append(f"Actions read up to {reached.date().isoformat()} (UTC): the next run carries on from there")
        return SyncResult(points=commits + actions, summary=summary, cursor={
            "commits_until": int(now.timestamp()), "runs_until": int(reached.timestamp()), "runs_partial": partial})
