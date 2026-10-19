from collections import Counter
from datetime import datetime, timezone

import pytest

from app.connectors import github
from app.connectors.base import ConnectorError
from app.points import encode
from tests.fakes import GITHUB_SECRETS, GITHUB_TOKEN_2, parse

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
BILLING = "/organizations/acme/settings/billing/usage"


def at(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def lines_of(result, user=None):
    return encode(result.points, "github", user).get("DevStats", [])


def of(lines, measurement):
    return [parse(line) for line in lines if parse(line)[0] == measurement]


def graphql_since(fake):
    return [body["variables"]["since"] for method, _path, body, _scope in fake.calls
            if method == "POST" and "history" in body["query"]]


def months_read(fake, path=BILLING):
    return [(int(p["year"]), int(p["month"])) for _m, called, p, _s in fake.calls if called == path]


def cursor_at(moment=NOW):
    state = {"commits_until": int(moment.timestamp()), "runs_until": int(moment.timestamp()),
             "usage_until": int(moment.timestamp())}
    return {"accounts": {"acme": dict(state), "dana": dict(state)}}


# -- Commits ----------------------------------------------------------------------------

def test_commits_are_told_apart_by_author(github_api, github_connector):
    commits = of(lines_of(github_connector.sync(None)), "github_commit")
    by_time = {ts: tags["author"] for _m, tags, _f, ts in commits}
    assert by_time == {
        at("2026-10-13T21:30:00Z"): "agent",  # Claude, a co-author
        at("2026-10-13T08:00:00Z"): "self",
        at("2026-10-12T07:00:00Z"): "bot",  # Renovate's pull request, squash-merged by dana
        at("2026-10-12T06:00:00Z"): "bot",  # renovate-bot: a -bot account
        at("2026-10-12T05:00:00Z"): "bot",  # github-actions[bot]
        at("2026-10-11T11:00:00Z"): "other",  # an email no account claims
        at("2026-10-10T09:00:00Z"): "agent",  # Copilot, the author, by login
        at("2025-01-01T09:00:00Z"): "self",
        at("2026-10-09T18:00:00Z"): "self",
    }
    # The merge commit is left out, the fork never read, the empty repository has none.
    assert at("2026-10-11T12:00:00Z") not in by_time
    assert {(tags["owner"], tags["repository"]) for _m, tags, _f, _ts in commits} == {("acme", "api"),
                                                                                      ("acme", "site")}


def test_a_commit_is_written_at_its_author_time_with_the_local_hour(github_api, github_connector):
    lines = lines_of(github_connector.sync(None), user="dana")
    assert ("github_commit,author=agent,owner=acme,repository=api,source=github,time_precision=second,user=dana "
            f"additions=10i,count=1i,deletions=2i,local_hour=23i,local_weekday=2i {at('2026-10-13T21:30:00Z')}"
            in lines)


def test_agents_are_whoever_the_setting_names(github_api, github_connector, monkeypatch):
    monkeypatch.setattr(github_connector.settings, "agents", "")
    authors = Counter(tags["author"] for _m, tags, _f, _ts in of(lines_of(github_connector.sync(None)),
                                                                 "github_commit"))
    assert authors == {"self": 4, "bot": 3, "other": 2}


# -- Hosted minutes ---------------------------------------------------------------------

def test_each_line_of_minutes_in_the_billing_report_is_a_point(github_api, github_connector):
    lines = lines_of(github_connector.sync(None), user="dana")
    assert ("github_actions_usage,owner=acme,repository=api,sku=Actions\\ Linux,source=github,user=dana "
            f"discount_usd=0.072,gross_usd=0.072,minutes=12.0,net_usd=0.0,private=true {at('2026-10-13T00:00:00Z')}"
            in lines)
    usage = {(tags["owner"], tags["repository"], tags["sku"], ts): fields
             for _m, tags, fields, ts in of(lines, "github_actions_usage")}
    # Storage and other products are left out.
    assert {sku for _o, _r, sku, _ts in usage} == {"Actions Linux", "Actions Windows", "Actions Linux 4-core"}
    assert usage[("acme", "site", "Actions Linux", at("2026-10-13T00:00:00Z"))]["private"] == "false"
    # A repository no longer listed counts as private.
    assert usage[("acme", "gone", "Actions Linux", at("2026-10-02T00:00:00Z"))]["private"] == "true"
    assert usage[("dana", "dotfiles", "Actions Linux", at("2026-10-14T00:00:00Z"))]["minutes"] == "4.0"


def test_a_month_counts_what_the_included_minutes_count(github_api, github_connector):
    months = {(tags["owner"], ts): fields
              for _m, tags, fields, ts in of(lines_of(github_connector.sync(None)), "github_actions_month")}
    assert set(months) == {("acme", at("2026-10-01T00:00:00Z")), ("acme", at("2026-09-01T00:00:00Z")),
                           ("acme", at("2025-11-01T00:00:00Z")), ("dana", at("2026-10-01T00:00:00Z"))}
    # 52 minutes; the public site's 30 are free; of the private 22, Linux once,
    # Windows twice, the larger runner never.
    assert months[("acme", at("2026-10-01T00:00:00Z"))] == {
        "minutes": "52.0", "private_minutes": "22.0", "counted_minutes": "20.0", "gross_usd": "0.354",
        "net_usd": "0.0"}


# -- Workflow runs ----------------------------------------------------------------------

def runs_read(fake):
    return [params for _m, path, params, _s in fake.calls
            if path.endswith("/actions/runs") and params.get("page") == "1"]


def test_each_completed_run_is_a_point_with_its_duration(github_api, github_connector):
    runs = {(tags["owner"], tags["repository"], tags["workflow"], ts): (tags, fields)
            for _m, tags, fields, ts in of(lines_of(github_connector.sync(None)), "github_workflow_run")}
    # A re-run: the second attempt at the first run's creation.
    tags, fields = runs[("acme", "api", "Tests", at("2026-10-13T22:00:00Z"))]
    assert fields == {"count": "1i", "succeeded": "1i", "failed": "0i", "cancelled": "0i", "attempts": "2i",
                      "duration_s": "240i", "result": "1i"}
    assert tags["visibility"] == "private" and tags["event"] == "push"
    # timed_out counts as failed; run_started_at to updated_at.
    _tags, fields = runs[("acme", "api", "Deploy", at("2026-10-13T20:00:00Z"))]
    assert fields["failed"] == "1i" and fields["duration_s"] == "600i" and fields["result"] == "2i"
    # A workflow the list lacks: its file's name.
    assert ("acme", "api", "old.yml", at("2026-09-20T08:00:00Z")) in runs
    # A public repository's run, and the account's own.
    assert runs[("acme", "site", "Pages", at("2026-10-10T18:00:00Z"))][0]["visibility"] == "public"
    assert runs[("dana", "dotfiles", "Sync", at("2026-10-14T06:00:00Z"))][1]["duration_s"] == "40i"
    # In progress: not written, read again next sync. Outside the window: never read.
    assert at("2026-10-12T09:00:00Z") not in {ts for _o, _r, _w, ts in runs}
    assert at("2025-01-01T08:00:00Z") not in {ts for _o, _r, _w, ts in runs}


def test_a_first_run_reads_history_days_of_runs_and_a_later_one_the_lookback(github_api, github_connector):
    github_connector.sync(None)
    assert {p["created"] for p in runs_read(github_api)} == {"2026-09-14T12:00:00Z..2026-10-14T12:00:00Z"}
    github_api.calls.clear()
    github_connector.sync(cursor_at())
    assert {p["created"] for p in runs_read(github_api)} == {"2026-10-11T12:00:00Z..2026-10-14T12:00:00Z"}


def test_the_workflow_list_is_read_once_per_repository_that_has_a_run(github_api, github_connector):
    github_connector.sync(None)
    paths = [path for _m, path, _p, _s in github_api.calls if path.endswith("/actions/workflows")]
    # api's has three workflows, read two pages of.
    assert set(paths) == {"/repos/acme/api/actions/workflows", "/repos/acme/site/actions/workflows",
                          "/repos/dana/dotfiles/actions/workflows"}


# -- Cursor -----------------------------------------------------------------------------

def test_a_first_run_reads_every_commit_and_a_year_of_billing(github_api, github_connector):
    result = github_connector.sync(None)
    assert set(graphql_since(github_api)) == {None}
    assert months_read(github_api) == [(2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 11)]
    assert result.cursor == cursor_at()
    assert result.summary == ["acme: 9 commit(s) in 2 repositories (2 agent, 3 bot, 1 other, 3 self)",
                              "acme: 4 workflow run(s) since 2026-09-14",
                              "acme: 1 run(s) still in progress, read again next run",
                              "acme: 52 hosted minute(s) this month, 20 counted against the included minutes",
                              "dana: 0 commit(s) in 1 repositories",
                              "dana: 1 workflow run(s) since 2026-09-14",
                              "dana: 4 hosted minute(s) this month, 4 counted against the included minutes"]
    assert not result.errors


def test_a_later_run_reads_from_the_cursor_minus_the_lookback_and_last_month_on(github_api, github_connector):
    result = github_connector.sync(cursor_at())
    assert set(graphql_since(github_api)) == {"2026-10-11T12:00:00Z"}
    assert months_read(github_api) == [(2026, 9), (2026, 10)]
    assert len(of(lines_of(result), "github_commit")) == 5


# -- Accounts ---------------------------------------------------------------------------

def test_each_account_is_read_with_its_own_token(github_api, github_connector):
    github_connector.sync(None)
    scopes = {(path, scope) for _m, path, _p, scope in github_api.calls if path != "/graphql"}
    assert ("/orgs/acme/repos", "acme") in scopes and ("/user/repos", "dana") in scopes
    assert ("/users/dana/settings/billing/usage", "dana") in scopes
    assert "/users/dana/repos" not in {path for path, _s in scopes}
    assert github_api.user_agents == {f"Tributary/{github.__version__}"}


def test_a_part_that_fails_keeps_its_cursor_and_the_rest_is_written(github_api, github_connector):
    github_api.fail[BILLING] = [(403, {})]
    result = github_connector.sync(cursor_at())
    assert result.errors == ["acme: GitHub refused /organizations/acme/settings/billing/usage (403): the token "
                             "of acme needs the Administration organisation permission (read), and you, an "
                             "organisation owner or billing manager"]
    assert {tags["owner"] for _m, tags, _f, _ts in of(lines_of(result), "github_commit")} == {"acme"}
    assert {tags["owner"] for _m, tags, _f, _ts in of(lines_of(result), "github_actions_usage")} == {"dana"}
    assert result.cursor["accounts"]["acme"] == {"commits_until": int(NOW.timestamp()),
                                                 "runs_until": int(NOW.timestamp()),
                                                 "usage_until": int(NOW.timestamp())}
    # A first run's billing that fails leaves it unread: the next run reads the whole year.
    github_api.fail[BILLING] = [(404, {})]
    assert "usage_until" not in github_connector.sync(None).cursor["accounts"]["acme"]


def test_an_account_whose_token_is_refused_says_which_variable(github_api, github_connector):
    github_api.tokens.pop(GITHUB_TOKEN_2)
    result = github_connector.sync(cursor_at())
    assert result.errors == ["dana: GitHub refused the token of dana (401): check TRIBUTARY_GITHUB_TOKEN_2."]
    assert result.cursor["accounts"]["dana"] == cursor_at()["accounts"]["dana"]
    assert of(lines_of(result), "github_commit")


def test_a_run_fails_when_every_account_does(github_api, github_connector):
    github_api.tokens.clear()
    with pytest.raises(ConnectorError, match=r"check TRIBUTARY_GITHUB_TOKEN\. ") as error:
        github_connector.sync(None)
    assert "TRIBUTARY_GITHUB_TOKEN_2" in str(error.value)


@pytest.mark.parametrize("numbered, message", [
    ({"TRIBUTARY_GITHUB_OWNER_3": "x", "TRIBUTARY_GITHUB_TOKEN_3": "y"}, "numbered _2 to _9 without a gap"),
    ({"TRIBUTARY_GITHUB_OWNER_2": "dana"}, "TRIBUTARY_GITHUB_TOKEN_2 missing"),
    ({"TRIBUTARY_GITHUB_OWNER_2": "ACME", "TRIBUTARY_GITHUB_TOKEN_2": "t"}, "name each owner once"),
])
def test_misconfigured_accounts_are_explained(github_api, github_connector, monkeypatch, numbered, message):
    monkeypatch.setattr(github_connector.settings, "_numbered", numbered)
    with pytest.raises(ConnectorError, match=message):
        github_connector.sync(None)


def test_numbered_accounts_come_from_the_environment_then_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRIBUTARY_GITHUB_OWNER_2=from-file\nTRIBUTARY_GITHUB_TOKEN_2=file-token\n")
    monkeypatch.setenv("TRIBUTARY_GITHUB_OWNER_2", "from-env")
    assert github.numbered_env(str(env_file)) == {"TRIBUTARY_GITHUB_OWNER_2": "from-env",
                                                  "TRIBUTARY_GITHUB_TOKEN_2": "file-token"}


def test_it_needs_an_owner_and_a_token(github_api, github_connector, monkeypatch):
    assert github_connector.api_configured()
    monkeypatch.setattr(github_connector.settings, "owner", " ")
    assert not github_connector.api_configured()


# -- Errors and privacy -----------------------------------------------------------------

def test_server_errors_and_secondary_limits_are_retried(github_api, github_connector):
    github_api.fail["/users/acme"] = [(502, {}), (403, {"Retry-After": "30"})]
    assert not github_connector.sync(None).errors
    assert github_api.slept == [2, 30]


def test_a_spent_quota_fails_the_account(github_api, github_connector):
    github_api.fail["/users/acme"] = [(403, {"X-RateLimit-Remaining": "0"})]
    assert "quota" in github_connector.sync(None).errors[0]


def test_no_email_or_token_is_ever_written(github_api, github_connector):
    result = github_connector.sync(None)
    written = "\n".join(lines_of(result, user="dana") + result.summary + result.errors)
    for secret in GITHUB_SECRETS:
        assert secret not in written
