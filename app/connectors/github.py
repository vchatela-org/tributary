"""GitHub: who wrote the commits (you, an agent, a bot), the workflow runs and
how long they took, and the minutes GitHub's hosted runners spent on them,
against the plan's included minutes, from the GitHub API, into DevStats.

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
  GET /repos/{owner}/{repo}/actions/workflows
                                        a repository's workflows: their names.
  GET /repos/{owner}/{repo}/actions/runs?created=
                                        the runs created in a range, 100 a
                                        call, newest first: event, conclusion,
                                        attempt and how long it took. Not a
                                        call per run.
  GET /repos/{owner}/{repo}/actions/runs/{id}/jobs
                                        a run's jobs, 100 a call: their
                                        runner's name and group say whether
                                        GitHub hosted it (its group is
                                        `GitHub Actions`) or it was
                                        self-hosted (ARC). A call a run, so a
                                        first sync walks them with a budget.
  GET /organizations/{org}/settings/billing/usage?year=&month=
  GET /users/{user}/settings/billing/usage?year=&month=
                                        the month's billed usage, a line per
                                        day, repository and SKU: GitHub's own
                                        count of its hosted runners' minutes
                                        (self-hosted runners are not billed,
                                        so not in it).
Lists are paged (`page`, 100 at a time). 5xx and a secondary rate limit
(403/429 with Retry-After) are retried; a redirect is refused (the token would
follow it).

Accounts: one token per owner, as a fine-grained token reads one:
TRIBUTARY_GITHUB_OWNER / _TOKEN, then TRIBUTARY_GITHUB_OWNER_2 / _TOKEN_2,
_3... (up to _9). An account that fails, or a part of one (its commits, its
runs, its billing), doesn't stop the others.

Mapping (times in UTC seconds):
  github_commit         one per commit of a repository's default branch (merge
                        commits left out, forks not read), at its author's
                        time, local time added. Tags owner, repository,
                        author: `agent` when one of its authors, the git
                        author or a Co-authored-by trailer, is an agent
                        (TRIBUTARY_GITHUB_AGENTS: Claude, Copilot and OpenHands
                        by default); else `bot` when one is a bot (a `[bot]` or
                        `-bot` account: Renovate opening its pull requests as
                        you co-authors their squash commits); else `self` (the
                        token's account) or `other` (an email no account
                        claims included). Fields count=1, additions, deletions.
  github_workflow_run   one per completed run, at its creation (a re-run
                        keeps that time, so its point overwrites the first
                        attempt's). Tags owner, repository, workflow (its own
                        name, from the workflow list; the run's name, which
                        may carry a pull request's title, is never read),
                        event, visibility. Fields count=1, succeeded, failed,
                        cancelled, attempts, duration_s (run_started_at to
                        updated_at), result (1 success, 2 failed, 3
                        cancelled, 0 other, for a table's mappings), and
                        hosted (1 when a job ran on a GitHub-hosted runner,
                        0 when they all ran self-hosted, -1 until its jobs
                        are read: a field, so a later read overwrites it).
                        Every run is written, self-hosted (ARC)
                        ones included: the billing report says which
                        repositories hosted minutes were billed to.
  github_actions_usage  one per line of the billing report with minutes (its
                        storage left out): a day (UTC midnight), owner,
                        repository and SKU (`Actions Linux`...). Fields
                        minutes, gross_usd, discount_usd, net_usd, and private:
                        the repository's visibility when read (one no longer
                        listed counts as private), a field so that a change
                        overwrites the day instead of doubling it.
  github_actions_month  one per owner and month, at its first day (UTC): the
                        month's minutes, private_minutes, counted_minutes (the
                        private repositories' standard-runner minutes as a
                        plan's included minutes count them: Windows twice,
                        macOS ten times), gross_usd, net_usd. InfluxQL has no
                        calendar month: this is the monthly graph's.
No commit message, title, branch, email or login is written.

Cursor, per owner: when its commits, its runs and its billing were last read.
Commits are read from then, minus TRIBUTARY_GITHUB_LOOKBACK_DAYS, and the
whole history on a first or full run (100 a call: cheap). Runs are read the
same way, TRIBUTARY_GITHUB_HISTORY_DAYS on a first or full run; the lookback
reads again the runs still in progress. A run's jobs cost a call each, so
TRIBUTARY_GITHUB_MAX_JOBS_RUNS of them are read per sync (and the token keeps
a reserve of its hourly quota): what is left is read by the next sync, from
where this one stopped. Billing is read a month at a time:
TRIBUTARY_GITHUB_HISTORY_MONTHS on a first or full run, then from the month
before the last run's (GitHub completes a day's usage later). Append only:
nothing is dropped.
"""
import http.client
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import PrivateAttr, SecretStr
from pydantic_settings import SettingsConfigDict

from app.__version__ import __version__
from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_GITHUB_"
API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
PER_PAGE = 100
MAX_ACCOUNTS = 9
# 5xx and secondary rate limits: retried after 2, 4, 8 s, or the Retry-After
# asked when longer, up to a minute: runs share one scheduler, a run that gives
# up is read again by the next.
RETRIES = 3
RETRY_AFTER_MAX = 60
_RETRYABLE = {500, 502, 503, 504}
# Calls of the hourly quota (5,000 for a token) left for everything else: below
# it, a run stops reading jobs and carries on next time.
QUOTA_RESERVE = 200
# Conclusions counted as failed: the run did not do its job.
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
# A run's outcome as one number, for a table's value mappings.
RESULT_OTHER, RESULT_SUCCESS, RESULT_FAILED, RESULT_CANCELLED = 0, 1, 2, 3
# How a plan's included minutes count a standard hosted runner's minute; other
# SKUs (larger runners) are never covered by them.
INCLUDED_MULTIPLIERS = {"actions linux": 1, "actions windows": 2, "actions macos": 10}
# Waits between retries, and the clock (tests replace them).
_sleep = sleep


def _now() -> datetime:
    return datetime.now(timezone.utc)


DATABASE = "DevStats"
COMMIT = Measurement(
    database=DATABASE, name="github_commit", tags=("owner", "repository", "author"),
    fields={"count": int, "additions": int, "deletions": int},
    local_time=True,
)
WORKFLOW_RUN = Measurement(
    database=DATABASE, name="github_workflow_run",
    tags=("owner", "repository", "workflow", "event", "visibility"),
    fields={"count": int, "succeeded": int, "failed": int, "cancelled": int, "attempts": int, "duration_s": int,
            "result": int, "hosted": int},
)
ACTIONS_USAGE = Measurement(
    database=DATABASE, name="github_actions_usage", tags=("owner", "repository", "sku"),
    fields={"minutes": float, "gross_usd": float, "discount_usd": float, "net_usd": float, "private": bool},
)
ACTIONS_MONTH = Measurement(
    database=DATABASE, name="github_actions_month", tags=("owner",),
    fields={"minutes": float, "private_minutes": float, "counted_minutes": float, "gross_usd": float,
            "net_usd": float},
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

_NUMBERED = re.compile(rf"^{PREFIX}(OWNER|TOKEN)_(\d+)$", re.IGNORECASE)


def numbered_env(env_file: str = ".env") -> Dict[str, str]:
    """TRIBUTARY_GITHUB_{OWNER,TOKEN}_<n>, from the environment, else from the
    .env file (like every other setting), names upper-cased. The environment
    wins, even empty; an empty value counts as unset."""
    found: Dict[str, str] = {}
    sources = []
    if Path(env_file).is_file():
        sources.append(dotenv_values(env_file))
    sources.append(os.environ)  # last: the environment wins
    for source in sources:
        for name, value in source.items():
            if _NUMBERED.match(name):
                found[name.upper()] = value or ""
    return {name: value for name, value in found.items() if value}


class GitHubSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # The first account: an organisation or a user, and a fine-grained token
    # on it, read-only: Contents and Metadata, plus Administration (an
    # organisation) or Plan (a user) for the billing report.
    owner: str = ""
    token: Optional[SecretStr] = None
    # Commit authors that are agents, as a co-author or the author: emails or
    # GitHub logins, comma-separated (Claude, GitHub Copilot, OpenHands).
    agents: str = "noreply@anthropic.com,Copilot,openhands-agent"
    # Days of commits and runs read again before the last run's end.
    lookback_days: int = 3
    # Days of workflow runs a first or full run reads.
    history_days: int = 30
    # Runs whose jobs one sync reads at most (a call each): a longer backfill
    # goes on next run, from where this one stopped.
    max_jobs_runs: int = 3000
    # Months of billing a first or full run reads, the current one included.
    history_months: int = 12

    # Further accounts (TRIBUTARY_GITHUB_OWNER_2...): undeclared names, read
    # by numbered_env() at the start like the rest.
    _numbered: Dict[str, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        self._numbered = numbered_env()

    @property
    def numbered(self) -> Dict[str, str]:
        return self._numbered


@dataclass(frozen=True)
class Account:
    number: int
    owner: str
    token: str = field(repr=False)

    @property
    def token_variable(self) -> str:
        return f"{PREFIX}TOKEN" + (f"_{self.number}" if self.number > 1 else "")


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    private: bool
    fork: bool = False


# ────────────────────────────────────────────────
# Mapping
# ────────────────────────────────────────────────

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


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _names(raw: str) -> FrozenSet[str]:
    """A comma-separated list, lower-cased: emails and logins alike."""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _login(person: Any) -> str:
    return str(_dict(_dict(person).get("user")).get("login") or "").lower()


def _is_bot(person: Mapping) -> bool:
    return (_login(person).endswith(("[bot]", "-bot"))
            or str(person.get("email") or "").lower().endswith("[bot]@users.noreply.github.com"))


def commit_author(commit: Mapping, me: str, agents: FrozenSet[str]) -> str:
    """`agent` when an agent is among its authors (the git author or a
    Co-authored-by trailer), by email or login; else `bot` when a bot is;
    else `self` or `other` by its git author."""
    author = _dict(commit.get("author"))
    people = [author] + [_dict(n) for n in _dict(commit.get("authors")).get("nodes") or []]
    if any({str(p.get("email") or "").lower(), _login(p)} & agents for p in people):
        return "agent"
    if any(_is_bot(p) for p in people):
        return "bot"
    return "self" if me and _login(author) == me.lower() else "other"


def commit_points(commits: List[Dict], repo: Repo, me: str, agents: FrozenSet[str]) -> List[Point]:
    points = []
    for commit in commits:
        authored = parse_datetime(commit.get("authoredDate"))
        if authored is None or _int(_dict(commit.get("parents")).get("totalCount")) > 1:
            continue
        points.append(Point(COMMIT, authored, {"owner": repo.owner, "repository": repo.name,
                                               "author": commit_author(commit, me, agents)},
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


def run_hosted(jobs: List[Dict]) -> int:
    """1 when a job of the run ran on a GitHub-hosted runner, 0 when they all
    ran self-hosted (ARC), -1 when none ran (all skipped) or they are not read
    yet: GitHub's own runners are in its `GitHub Actions` group, and their
    runner's name says so too."""
    ran = [job for job in jobs if job.get("runner_name")]
    if not ran:
        return -1
    return int(any(str(job.get("runner_group_name") or "") == "GitHub Actions"
                   or ("runner_group_id" in job and job["runner_group_id"] == 0)
                   or str(job.get("runner_name")).startswith("GitHub Actions ")
                   for job in ran))


def run_point(run: Mapping, repo: Repo, workflow: str, hosted: int = -1) -> Optional[Point]:
    """A completed run, at its creation; a re-run overwrites it."""
    created = parse_datetime(run.get("created_at"))
    if created is None or run.get("status") != "completed":
        return None
    conclusion = run.get("conclusion")
    result = (RESULT_SUCCESS if conclusion == "success" else RESULT_FAILED if conclusion in FAILED
              else RESULT_CANCELLED if conclusion == "cancelled" else RESULT_OTHER)
    fields: Dict[str, Any] = {"count": 1, "succeeded": int(conclusion == "success"),
                              "failed": int(conclusion in FAILED), "cancelled": int(conclusion == "cancelled"),
                              "attempts": max(1, _int(run.get("run_attempt"))), "duration_s": 0,
                              "result": result, "hosted": hosted}
    started, updated = parse_datetime(run.get("run_started_at")), parse_datetime(run.get("updated_at"))
    if started is not None and updated is not None and updated >= started:
        fields["duration_s"] = int((updated - started).total_seconds())
    return Point(WORKFLOW_RUN, created, {"owner": repo.owner, "repository": repo.name, "workflow": workflow,
                                         "event": str(run.get("event") or ""),
                                         "visibility": "private" if repo.private else "public"}, fields)


def _sku(raw: Any) -> str:
    """`Actions Linux` (the report) and `actions_linux` (its summary) alike."""
    return str(raw or "").replace("_", " ").strip()


def minute_items(items: List[Dict]) -> List[Dict]:
    """The report's lines of Actions minutes (its storage left out)."""
    return [item for item in items if str(item.get("product") or "").lower() == "actions"
            and str(item.get("unitType") or "").lower() == "minutes" and parse_datetime(item.get("date"))]


def usage_points(items: List[Dict], owner: str, private: Mapping[str, bool]) -> List[Point]:
    return [Point(ACTIONS_USAGE, parse_datetime(item["date"]), {
        "owner": owner, "repository": str(item.get("repositoryName") or ""), "sku": _sku(item.get("sku")),
    }, {"minutes": _float(item.get("quantity")), "gross_usd": _float(item.get("grossAmount")),
        "discount_usd": _float(item.get("discountAmount")), "net_usd": _float(item.get("netAmount")),
        "private": private.get(str(item.get("repositoryName") or ""), True)}) for item in items]


def month_point(items: List[Dict], owner: str, private: Mapping[str, bool], month: datetime) -> Point:
    fields = {"minutes": 0.0, "private_minutes": 0.0, "counted_minutes": 0.0, "gross_usd": 0.0, "net_usd": 0.0}
    for item in items:
        minutes = _float(item.get("quantity"))
        fields["minutes"] += minutes
        fields["gross_usd"] += _float(item.get("grossAmount"))
        fields["net_usd"] += _float(item.get("netAmount"))
        if private.get(str(item.get("repositoryName") or ""), True):
            fields["private_minutes"] += minutes
            fields["counted_minutes"] += minutes * INCLUDED_MULTIPLIERS.get(_sku(item.get("sku")).lower(), 0)
    return Point(ACTIONS_MONTH, month, {"owner": owner}, {k: round(v, 6) for k, v in fields.items()})


def month_start(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def months(first: datetime, last: datetime) -> List[datetime]:
    """The first days (UTC) of the months from first's to last's, both included."""
    found, month = [], month_start(first)
    while month <= last:
        found.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return found


def months_back(now: datetime, count: int) -> datetime:
    month = month_start(now)
    for _ in range(max(1, count) - 1):
        month = month_start(month - timedelta(days=1))
    return month


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


# ────────────────────────────────────────────────
# Connector
# ────────────────────────────────────────────────

class GitHubConnector(Connector):
    key = "github"
    label = "GitHub"
    description = ("Who wrote the commits (you, an agent, a bot), the workflow runs and how long they took, "
                   "and the minutes GitHub's hosted runners spent on them, against the plan's included minutes.")
    icon = "bi-github"
    category = "Productivity"
    # Commits carry their own time and the billing report is daily: once a
    # day, at night.
    default_schedule = "15 4 * * *"
    settings_class = GitHubSettings
    measurements = (COMMIT, WORKFLOW_RUN, ACTIONS_USAGE, ACTIONS_MONTH)
    required_env = (f"{PREFIX}OWNER", f"{PREFIX}TOKEN")

    def __init__(self) -> None:
        super().__init__()
        # Each token's calls left this hour, from GitHub's answer; None until
        # a call says it.
        self._remaining: Dict[str, int] = {}

    def api_configured(self) -> bool:
        return bool(self.settings.owner.strip() and self.settings.token
                    and self.settings.token.get_secret_value().strip())

    def accounts(self) -> List[Account]:
        """The first account, then the numbered ones; a configuration mistake
        raises, naming the variables (never their values)."""
        s = self.settings
        accounts = [Account(1, s.owner.strip(), s.token.get_secret_value().strip() if s.token else "")]
        env = s.numbered
        problems: List[str] = []
        numbers = sorted({int(m.group(2)) for name in env if (m := _NUMBERED.match(name))})
        if numbers != list(range(2, len(numbers) + 2)) or (numbers and numbers[-1] > MAX_ACCOUNTS):
            problems.append(f"further accounts are numbered _2 to _{MAX_ACCOUNTS} without a gap "
                            f"(found {', '.join(f'_{n}' for n in numbers)})")
        for n in numbers:
            names = [f"{PREFIX}OWNER_{n}", f"{PREFIX}TOKEN_{n}"]
            missing = [name for name in names if not env.get(name, "").strip()]
            if missing:
                problems.append(f"{', '.join(missing)} missing")
            else:
                accounts.append(Account(n, env[names[0]].strip(), env[names[1]].strip()))
        owners = [a.owner.lower() for a in accounts]
        if len(set(owners)) != len(owners):
            problems.append(f"{PREFIX}OWNER* must name each owner once")
        if problems:
            raise ConnectorError("GitHub accounts misconfigured: " + "; ".join(problems) + ".")
        return accounts

    # -- API -----------------------------------------------------------------

    def _call(self, account: Account, path: str, params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict] = None, refused: Optional[str] = None) -> Any:
        """One request with the account's token. `refused`: what a 403 or a 404
        means on this path, for the run's message."""
        url = API_URL.rstrip("/") + path + ("?" + urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {account.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": f"Tributary/{__version__}"}
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
                    if remaining and remaining.isdigit():
                        self._remaining[account.token] = int(remaining)
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 401:
                    raise ConnectorError(f"GitHub refused the token of {account.owner} (401): check "
                                         f"{account.token_variable}.") from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                limited = exc.code == 429 or (exc.code == 403 and (retry_after or (
                    exc.headers and exc.headers.get("X-RateLimit-Remaining") == "0")))
                if limited and not retry_after:
                    raise ConnectorError(f"GitHub's hourly quota for the token of {account.owner} is spent: "
                                         "the next run reads it again.") from None
                if exc.code in (403, 404) and not limited:
                    raise ConnectorError(f"GitHub refused {path} ({exc.code}): "
                                         + (refused or f"the token of {account.owner} cannot read it.")) from None
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

    def _graphql(self, account: Account, query: str, variables: Dict[str, Any]) -> Dict:
        answer = _dict(self._call(account, "/graphql", body={"query": query, "variables": variables}))
        errors = answer.get("errors")
        if errors:
            message = str(_dict(errors[0]).get("message") or "an error") if isinstance(errors, list) else "an error"
            raise ConnectorError(f"GitHub's GraphQL API answered: {message[:200]}")
        return _dict(answer.get("data"))

    def _pages(self, account: Account, path: str, params: Optional[Dict[str, Any]] = None,
               key: Optional[str] = None) -> Iterator[List[Dict]]:
        """Each page's items: the answer's list, or `key` of its object."""
        page = 1
        while True:
            answer = self._call(account, path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            items = _dict(answer).get(key) if key else answer
            if not isinstance(items, list):
                raise ConnectorError(f"Unexpected answer from GitHub on {path}.")
            yield [item for item in items if isinstance(item, dict)]
            if len(items) < PER_PAGE:
                return
            page += 1

    def _repos(self, account: Account, organisation: bool, me: str) -> List[Repo]:
        owner = quote(account.owner)
        if organisation:
            path, params = f"/orgs/{owner}/repos", {"type": "all"}
        elif me and account.owner.lower() == me.lower():
            path, params = "/user/repos", {"affiliation": "owner", "visibility": "all"}
        else:
            path, params = f"/users/{owner}/repos", {"type": "owner"}
        repos: Dict[str, Repo] = {}
        for page in self._pages(account, path, params):
            for item in page:
                name = str(item.get("name") or "")
                visibility = item.get("visibility")
                if name:
                    repos[name] = Repo(account.owner, name,
                                       visibility != "public" if visibility else bool(item.get("private")),
                                       bool(item.get("fork")))
        return sorted(repos.values(), key=lambda r: r.name.lower())

    def _commits(self, account: Account, repo: Repo, since: Optional[datetime]) -> Iterator[List[Dict]]:
        after = None
        while True:
            data = self._graphql(account, COMMITS_QUERY, {"owner": repo.owner, "name": repo.name,
                                                          "since": _iso(since) if since else None, "after": after})
            ref = _dict(_dict(data.get("repository")).get("defaultBranchRef"))
            history = _dict(_dict(ref.get("target")).get("history"))
            yield [_dict(node) for node in history.get("nodes") or []]
            info = _dict(history.get("pageInfo"))
            if not info.get("hasNextPage") or not info.get("endCursor"):
                return
            after = info["endCursor"]

    def _workflows(self, account: Account, repo: Repo) -> Dict[int, str]:
        """A repository's workflows: their id to their own name."""
        names: Dict[int, str] = {}
        for page in self._pages(account, f"/repos/{quote(repo.owner)}/{quote(repo.name)}/actions/workflows",
                                key="workflows"):
            names.update({_int(w.get("id")): str(w.get("name") or "") for w in page if _int(w.get("id"))})
        return names

    def _runs(self, account: Account, repo: Repo, since: datetime, until: datetime) -> Iterator[List[Dict]]:
        """The runs created in [since, until], newest first."""
        yield from self._pages(account, f"/repos/{quote(repo.owner)}/{quote(repo.name)}/actions/runs",
                               {"created": f"{_iso(since)}..{_iso(until)}", "sort": "created",
                                "direction": "desc"}, key="workflow_runs")

    def _jobs(self, account: Account, repo: Repo, run: Mapping) -> List[Dict]:
        """Every job of a run, the skipped ones included."""
        jobs: List[Dict] = []
        for page in self._pages(account, f"/repos/{quote(repo.owner)}/{quote(repo.name)}/actions/runs/"
                                         f"{_int(run.get('id'))}/jobs", {"filter": "all"}, key="jobs"):
            jobs += page
        return jobs

    def _usage(self, account: Account, organisation: bool, month: datetime) -> List[Dict]:
        owner = quote(account.owner)
        if organisation:
            path = f"/organizations/{owner}/settings/billing/usage"
            refused = (f"the token of {account.owner} needs the Administration organisation permission (read), "
                       "and you, an organisation owner or billing manager")
        else:
            path = f"/users/{owner}/settings/billing/usage"
            refused = f"the token of {account.owner} needs the Plan user permission (read)"
        answer = _dict(self._call(account, path, {"year": month.year, "month": month.month}, refused=refused))
        items = answer.get("usageItems")
        if not isinstance(items, list):
            raise ConnectorError(f"Unexpected answer from GitHub on {path}.")
        return [item for item in items if isinstance(item, dict)]

    # -- Sync ----------------------------------------------------------------

    def _run_points(self, account: Account, repo: Repo, since: datetime, until: datetime,
                    read: Dict[str, Any], jobs: bool = True) -> Tuple[List[Point], int, Optional[datetime], bool]:
        """The completed runs created in [since, until), newest first, their
        jobs read while the sync's budget (`read`) allows (`jobs`): the
        points, the runs still in progress, the creation time of the oldest
        run whose jobs were read, and whether the whole window was read (the
        budget didn't stop it)."""
        points: List[Point] = []
        in_progress = 0
        oldest: Optional[datetime] = None
        stopped_before = read["stopped"]
        workflows: Optional[Dict[int, str]] = None
        for page in self._runs(account, repo, since, until):
            for run in page:
                created = parse_datetime(run.get("created_at"))
                if created is None or created < since or created >= until:
                    continue
                if run.get("status") != "completed":
                    in_progress += 1
                    continue
                if workflows is None:
                    workflows = self._workflows(account, repo)
                hosted = -1
                if jobs and not read["stopped"]:
                    if (read["jobs"] >= self.settings.max_jobs_runs
                            or self._remaining.get(account.token, QUOTA_RESERVE) < QUOTA_RESERVE):
                        read["stopped"] = True
                    else:
                        read["jobs"] += 1
                        hosted = run_hosted(self._jobs(account, repo, run))
                        read["hosted"] += hosted == 1
                        oldest = created
                point = run_point(run, repo, workflow_name(run, workflows), hosted)
                if point is not None:
                    points.append(point)
        return points, in_progress, oldest, jobs and not stopped_before and not read["stopped"]

    def _sync_account(self, account: Account, known: Dict, now: datetime, tz: ZoneInfo
                      ) -> Tuple[List[Point], List[str], List[str], Dict]:
        """One owner's points, summary, errors and cursor; a part that fails
        keeps its cursor, the other is still written."""
        me = str(_dict(self._graphql(account, "query { viewer { login } }", {}).get("viewer")).get("login") or "")
        organisation = _dict(self._call(account, f"/users/{quote(account.owner)}")).get("type") == "Organization"
        repos = self._repos(account, organisation, me)
        private = {repo.name: repo.private for repo in repos}
        points: List[Point] = []
        summary: List[str] = []
        errors: List[str] = []
        cursor = dict(known)

        commits_until = _epoch(known.get("commits_until"))
        since = min(commits_until, now) - timedelta(days=max(0, self.settings.lookback_days)) \
            if commits_until else None
        try:
            agents = _names(self.settings.agents)
            commits: List[Point] = []
            read = [repo for repo in repos if not repo.fork]
            for repo in read:
                for page in self._commits(account, repo, since):
                    commits += commit_points(page, repo, me, agents)
            authors: Dict[str, int] = {}
            for point in commits:
                authors[point.tags["author"]] = authors.get(point.tags["author"], 0) + 1
            by_author = ", ".join(f"{count} {name}" for name, count in sorted(authors.items()))
            summary.append(f"{account.owner}: {len(commits)} commit(s) in {len(read)} repositories"
                           + (f" ({by_author})" if by_author else "")
                           + (f" since {since.astimezone(tz).date().isoformat()}" if since else ""))
            points += commits
            cursor["commits_until"] = int(now.timestamp())
        except ConnectorError as exc:
            errors.append(f"{account.owner}: {exc}")

        runs_until = _epoch(known.get("runs_until"))
        runs_since = min(runs_until, now) - timedelta(days=max(0, self.settings.lookback_days)) \
            if runs_until else now - timedelta(days=max(0, self.settings.history_days))
        history_start = now - timedelta(days=max(0, self.settings.history_days))
        jobs_done = bool(known.get("jobs_done"))
        floors = {name: int(floor.timestamp()) for name, floor in
                  ((name, _epoch(value)) for name, value in _dict(known.get("jobs_floors")).items())
                  if floor is not None}
        names = {repo.name for repo in repos}
        floors = {name: floor for name, floor in floors.items() if name in names}
        read: Dict[str, Any] = {"jobs": 0, "hosted": 0, "stopped": False}
        try:
            # Until the older runs' jobs are read, the main pass only reads
            # new runs (since the last sync, or the whole history on a first
            # one, written with hosted=-1) and the backfill below carries on
            # from its floor, so neither spends the budget on the other's.
            first_sync = runs_until is None
            recent = runs_until if runs_until is not None and not jobs_done else runs_since
            runs: List[Point] = []
            in_progress = 0
            for repo in repos:
                finished = False
                oldest: Optional[datetime] = None
                if not first_sync:
                    found, waiting, oldest, finished = self._run_points(
                        account, repo, recent, now, read, jobs=True)
                    runs += found
                    in_progress += waiting
                if jobs_done:
                    continue
                if finished:
                    target = int(recent.timestamp())
                    floors[repo.name] = min(floors.get(repo.name, target), target)
                elif oldest is not None:
                    reached = int(oldest.timestamp())
                    floors[repo.name] = min(floors.get(repo.name, reached), reached)
            if not jobs_done:
                for repo in repos:
                    # A repository the main pass didn't reach has no floor:
                    # read all of its history window. One it did keeps its
                    # floor, or this sync's window start when the floor is
                    # newer (that part was just read).
                    found_floor = floors.get(repo.name)
                    upper = int(now.timestamp()) if found_floor is None \
                        else min(found_floor, int(recent.timestamp()))
                    if upper <= int(history_start.timestamp()):
                        continue
                    found, waiting, oldest, finished = self._run_points(
                        account, repo, history_start, datetime.fromtimestamp(upper, timezone.utc), read)
                    runs += found
                    in_progress += waiting
                    if finished:
                        floors[repo.name] = int(history_start.timestamp())
                    elif oldest is not None:
                        floors[repo.name] = min(floors.get(repo.name, int(oldest.timestamp())),
                                                int(oldest.timestamp()))
                jobs_done = all(floor <= int(history_start.timestamp()) for floor in floors.values()) \
                    and len(floors) >= len(repos)
            label = history_start if not first_sync and not jobs_done else recent
            summary.append(f"{account.owner}: {len(runs)} workflow run(s) since "
                           f"{label.astimezone(tz).date().isoformat()}")
            if in_progress:
                summary.append(f"{account.owner}: {in_progress} run(s) still in progress, read again next run")
            if read["jobs"]:
                summary.append(f"{account.owner}: {read['jobs']} run(s)' jobs read, "
                               f"{read['hosted']} on GitHub-hosted runners")
            if not jobs_done:
                summary.append(f"{account.owner}: the older runs' jobs are still being read, "
                               "the next run carries on")
            points += runs
            cursor["runs_until"] = int(now.timestamp())
            cursor["jobs_done"] = jobs_done
            if jobs_done:
                cursor.pop("jobs_floors", None)
            else:
                cursor["jobs_floors"] = {name: int(floor) for name, floor in floors.items()}
        except ConnectorError as exc:
            errors.append(f"{account.owner}: {exc}")

        usage_until = _epoch(known.get("usage_until"))
        first = month_start(month_start(min(usage_until, now)) - timedelta(days=1)) if usage_until \
            else months_back(now, self.settings.history_months)
        try:
            usage: List[Point] = []
            this_month: Optional[Point] = None
            for month in months(first, now):
                items = minute_items(self._usage(account, organisation, month))
                if items:
                    usage += usage_points(items, account.owner, private)
                    usage.append(month_point(items, account.owner, private, month))
                    if month == month_start(now):
                        this_month = usage[-1]
            if this_month is not None:
                summary.append(f"{account.owner}: {this_month.fields['minutes']:.0f} hosted minute(s) this month, "
                               f"{this_month.fields['counted_minutes']:.0f} counted against the included minutes")
            points += usage
            cursor["usage_until"] = int(now.timestamp())
        except ConnectorError as exc:
            errors.append(f"{account.owner}: {exc}")
        return points, summary, errors, cursor

    def sync(self, cursor: Any) -> SyncResult:
        known = _dict(_dict(cursor).get("accounts"))
        accounts = self.accounts()
        tz = ZoneInfo(app_settings.tz)
        now = _now()
        self._remaining = {}
        points: List[Point] = []
        summary: List[str] = []
        errors: List[str] = []
        new_cursor: Dict[str, Dict] = {}
        failed = 0
        for account in accounts:
            key = account.owner.lower()
            try:
                found, said, wrong, state = self._sync_account(account, _dict(known.get(key)), now, tz)
            except ConnectorError as exc:
                errors.append(f"{account.owner}: {exc}")
                if key in known:
                    new_cursor[key] = known[key]
                failed += 1
                continue
            points += found
            summary += said
            errors += wrong
            new_cursor[key] = state
            failed += len(wrong) == 3
        if failed == len(accounts):
            raise ConnectorError(" ".join(errors))
        return SyncResult(points=points, summary=summary, errors=errors, cursor={"accounts": new_cursor})
