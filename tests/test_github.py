from collections import Counter
from datetime import datetime, timezone

import pytest

from app.connectors import github
from app.connectors.base import ConnectorError
from app.points import encode
from tests.fakes import GITHUB_SECRETS, parse

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def at(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def lines_of(result, user=None):
    return encode(result.points, "github", user).get("DevStats", [])


def of(lines, measurement):
    return [parse(line) for line in lines if parse(line)[0] == measurement]


def graphql_since(fake):
    return [body["variables"]["since"] for method, path, body in fake.calls
            if method == "POST" and "history" in body["query"]]


def runs_listed(fake):
    return [(path.split("/")[3], params["created"]) for method, path, params in fake.calls
            if path.endswith("/actions/runs") and params.get("page") == "1"]


# -- Commits ----------------------------------------------------------------------------

def test_commits_are_told_apart_by_author(github_api, github_connector):
    commits = of(lines_of(github_connector.sync(None)), "github_commit")
    by_time = {ts: tags["author"] for _m, tags, _f, ts in commits}
    assert by_time == {
        at("2026-10-13T21:30:00Z"): "agent",  # Claude, a co-author
        at("2026-10-13T08:00:00Z"): "self",
        at("2026-10-12T06:00:00Z"): "bot",  # renovate-bot: a -bot account
        at("2026-10-12T05:00:00Z"): "bot",  # github-actions[bot]
        at("2026-10-11T11:00:00Z"): "other",  # an email no account claims
        at("2026-10-10T09:00:00Z"): "agent",  # Copilot, the author, by login
        at("2025-01-01T09:00:00Z"): "self",
        at("2026-10-09T18:00:00Z"): "self",
    }
    # The merge commit is left out, the empty repository has none.
    assert at("2026-10-11T12:00:00Z") not in by_time
    assert {tags["repository"] for _m, tags, _f, _ts in commits} == {"api", "site"}


def test_a_commit_is_written_at_its_author_time_with_the_local_hour(github_api, github_connector):
    lines = lines_of(github_connector.sync(None), user="dana")
    assert ("github_commit,author=agent,repository=api,source=github,time_precision=second,user=dana "
            f"additions=10i,count=1i,deletions=2i,local_hour=23i,local_weekday=2i {at('2026-10-13T21:30:00Z')}"
            in lines)


def test_agents_are_whoever_the_setting_names(github_api, github_connector, monkeypatch):
    monkeypatch.setattr(github_connector.settings, "agents", "")
    authors = Counter(tags["author"] for _m, tags, _f, _ts in of(lines_of(github_connector.sync(None)),
                                                                 "github_commit"))
    assert authors == {"self": 4, "bot": 2, "other": 2}


# -- Runs and jobs ----------------------------------------------------------------------

def test_a_run_is_counted_by_how_it_ended(github_api, github_connector):
    runs = {tags["workflow"]: (tags, fields, ts)
            for _m, tags, fields, ts in of(lines_of(github_connector.sync(None)), "github_workflow_run")}
    assert set(runs) == {"Tests", "Deploy", "old.yml", "Pages"}
    tags, fields, ts = runs["Tests"]
    assert (tags["event"], tags["visibility"], ts) == ("pull_request", "private", at("2026-10-13T10:00:00Z"))
    assert fields == {"count": "1i", "succeeded": "1i", "failed": "0i", "cancelled": "0i", "attempts": "1i",
                      "duration_s": "300i"}
    _t, fields, _ts = runs["Deploy"]
    assert (fields["failed"], fields["attempts"], fields["duration_s"]) == ("1i", "2i", "3600i")
    tags, fields, _ts = runs["Pages"]
    assert (tags["visibility"], tags["event"], fields["cancelled"]) == ("public", "schedule", "1i")


def test_jobs_say_which_runner_ran_them_and_what_it_bills(github_api, github_connector):
    lines = lines_of(github_connector.sync(None))
    assert ("github_job,conclusion=success,job=pytest,repository=api,runner=github-hosted,runner_label=ubuntu-latest,"
            "source=github,visibility=private,workflow=Tests billable_min=3i,count=1i,duration_s=121i,queued_s=5i "
            f"{at('2026-10-13T10:00:10Z')}") in lines
    jobs = {(tags["job"], ts): (tags, fields) for _m, tags, fields, ts in of(lines, "github_job")}
    # Skipped: never ran, left out.
    assert not [key for key in jobs if key[0] == "lint"]
    # Windows counts twice: 61 s is 2 minutes, 4 billed.
    assert jobs[("windows", at("2026-10-13T10:00:20Z"))][1]["billable_min"] == "4i"
    # Both attempts of the re-run, on ARC: free.
    apply = [v for (name, _ts), v in jobs.items() if name == "apply"]
    assert [(t["runner"], t["runner_label"], t["conclusion"], f["billable_min"]) for t, f in apply] == [
        ("self-hosted", "arc-runner-set", "failure", "0i")] * 2
    # A public repository's hosted minutes are free.
    tags, fields = jobs[("build", at("2026-10-13T03:00:05Z"))]
    assert (tags["runner"], tags["visibility"], fields["billable_min"]) == ("github-hosted", "public", "0i")
    # Cancelled before a runner took it: no runner tag.
    assert "runner" not in jobs[("deploy", at("2026-10-13T03:00:40Z"))][0]


def test_no_title_branch_email_or_token_is_ever_written(github_api, github_connector):
    result = github_connector.sync(None)
    written = "\n".join(lines_of(result, user="dana") + result.summary)
    for secret in GITHUB_SECRETS:
        assert secret not in written


# -- Cursor and budget ------------------------------------------------------------------

def test_a_first_run_reads_every_commit_and_the_last_days_of_actions(github_api, github_connector):
    result = github_connector.sync(None)
    assert set(graphql_since(github_api)) == {None}
    days = sorted({created for _repo, created in runs_listed(github_api)})
    assert days[0] == "2026-09-14T00:00:00Z..2026-09-14T23:59:59Z"
    assert days[-1] == "2026-10-14T00:00:00Z..2026-10-14T23:59:59Z"
    # Forks are never read.
    assert not [path for _m, path, _p in github_api.calls if "/upstream/" in path]
    assert result.cursor == {"commits_until": int(NOW.timestamp()), "runs_until": int(NOW.timestamp()),
                             "runs_partial": False}
    assert result.summary == ["8 commit(s) in 3 repositories (2 agent, 2 bot, 1 other, 3 self)",
                              "4 workflow run(s), 7 job(s) since 2026-09-14",
                              "1 run(s) still in progress, read again next run"]


def test_a_later_run_reads_from_the_cursor_minus_the_lookback(github_api, github_connector):
    result = github_connector.sync({"commits_until": int(NOW.timestamp()), "runs_until": int(NOW.timestamp()),
                                    "runs_partial": False})
    assert set(graphql_since(github_api)) == {"2026-10-11T12:00:00Z"}
    assert min(created for _r, created in runs_listed(github_api)).startswith("2026-10-11T00:00:00Z")
    assert len(of(lines_of(result), "github_commit")) == 4
    assert len(of(lines_of(result), "github_workflow_run")) == 4


def test_a_backfill_stops_at_the_budget_and_carries_on_from_the_day_it_reached(github_api, github_connector,
                                                                               monkeypatch):
    monkeypatch.setattr(github_connector.settings, "max_runs", 2)
    first = github_connector.sync(None)
    assert {tags["workflow"] for _m, tags, _f, _ts in of(lines_of(first), "github_workflow_run")} == {
        "old.yml", "Deploy"}
    assert first.cursor["runs_until"] == at("2026-10-13T00:00:00Z") and first.cursor["runs_partial"]
    assert first.summary[-1] == "Actions read up to 2026-10-13 (UTC): the next run carries on from there"

    github_api.calls.clear()
    second = github_connector.sync(first.cursor)
    # From the day it reached, not a lookback before it.
    assert min(created for _r, created in runs_listed(github_api)).startswith("2026-10-13T00:00:00Z")
    assert {tags["workflow"] for _m, tags, _f, _ts in of(lines_of(second), "github_workflow_run")} == {
        "Tests", "Pages"}
    assert second.cursor["runs_until"] == int(NOW.timestamp()) and not second.cursor["runs_partial"]


def test_a_run_stops_reading_actions_before_the_quota_runs_out(github_api, github_connector):
    # Five calls find the repositories; the first day of one leaves less than the reserve.
    github_api.quota = github.QUOTA_RESERVE + 5
    result = github_connector.sync(None)
    assert result.cursor["runs_partial"] and result.cursor["runs_until"] == at("2026-09-14T00:00:00Z")
    assert len(of(lines_of(result), "github_commit")) == 8


# -- Configuration and errors -----------------------------------------------------------

def test_it_needs_a_token_and_an_owner(github_api, github_connector, monkeypatch):
    assert github_connector.api_configured()
    monkeypatch.setattr(github_connector.settings, "owners", " , ")
    assert not github_connector.api_configured()


def test_the_token_own_account_is_read_with_its_private_repositories(github_api, github_connector):
    github_connector.sync(None)
    paths = [path for _m, path, _p in github_api.calls]
    assert "/user/repos" in paths and "/users/dana/repos" not in paths
    assert github_api.user_agents == {f"Tributary/{github.__version__}"}


def test_a_refused_token_says_which_variable(github_api, github_connector):
    github_api.token = "another"
    with pytest.raises(ConnectorError, match="TRIBUTARY_GITHUB_TOKEN") as error:
        github_connector.sync(None)
    assert "github-test-token" not in str(error.value)


def test_server_errors_and_secondary_limits_are_retried(github_api, github_connector):
    github_api.fail["/users/acme"] = [(502, {}), (403, {"Retry-After": "30"})]
    github_connector.sync(None)
    assert github_api.slept == [2, 30]


def test_a_spent_quota_or_a_refusal_fails_the_run(github_api, github_connector):
    github_api.fail["/users/acme"] = [(403, {"X-RateLimit-Remaining": "0"})]
    with pytest.raises(ConnectorError, match="quota"):
        github_connector.sync(None)
    github_api.fail["/users/acme"] = [(403, {})]
    with pytest.raises(ConnectorError, match="cannot read it"):
        github_connector.sync(None)
