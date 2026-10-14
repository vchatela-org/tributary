# GitHub

[GitHub](https://github.com/) holds the commits of your repositories and the
runs of their workflows. Tributary syncs them from the
[REST](https://docs.github.com/en/rest) and
[GraphQL](https://docs.github.com/en/graphql) APIs into `DevStats` (see
[the schema](../schema.md)), to answer three questions:

- **Who wrote the commits:** you, an agent (a commit you made with Claude,
  Copilot or OpenHands as a co-author, or one it made), a bot (Renovate,
  Dependabot...), or someone else. Each with its local hour, so it can be
  read against a night's sleep.
- **How the workflow runs end:** succeeded, failed or cancelled, per
  repository and workflow.
- **Where the minutes go:** every job's duration on a GitHub-hosted runner
  or on your own (self-hosted, e.g. actions-runner-controller), and the
  minutes GitHub bills a private repository's hosted jobs.

Why not Grafana's GitHub data source: it queries one repository (and one
workflow) at a time, reads no job or runner, and keeps nothing. Prometheus
metrics from actions-runner-controller see only its own runners, for as long
as Prometheus keeps them.

## Setup

1. Create a token: GitHub, *Settings > Developer settings > Personal access
   tokens > Fine-grained tokens*. Resource owner: the organisation (or your
   account) to read; repository access: all repositories; permissions, all
   **read-only**: *Actions*, *Contents* and *Metadata*. A fine-grained token
   reads one owner; to read several, use a classic token with the `repo`
   scope, which can also write: prefer one owner.
2. Set:

   | Variable | |
   |---|---|
   | `TRIBUTARY_GITHUB_TOKEN` | The token. |
   | `TRIBUTARY_GITHUB_OWNERS` | The organisations or users whose repositories it reads, comma-separated (`my-org`). Your own account's private repositories are read when the token is yours. |
   | `TRIBUTARY_GITHUB_AGENTS` | Commit authors that are agents: emails or GitHub logins, comma-separated (default `noreply@anthropic.com,Copilot,openhands-agent`: Claude, GitHub Copilot, OpenHands). |
   | `TRIBUTARY_GITHUB_HISTORY_DAYS` | Days of workflow runs a first or full sync reads (default 30). Commits are read whole. |
   | `TRIBUTARY_GITHUB_MAX_RUNS` | Runs whose jobs one sync reads at most (default 3000), see [How it syncs](#how-it-syncs). |
   | `TRIBUTARY_GITHUB_LOOKBACK_DAYS` | Days read again before the last sync, for runs still in progress then (default 3). |
   | `TRIBUTARY_GITHUB_SCHEDULE` | A 5-field cron expression, in `TRIBUTARY_TZ` (default: daily at 04:15). Set it on the source's page instead, when you can: see [Scheduling](../scheduling.md). |
   | `TRIBUTARY_GITHUB_INTERVAL_MINUTES` | Deprecated: minutes between two syncs, counted from the start. Superseded by the schedule; still honoured so an existing deployment keeps working. |

3. In Tributary, *Dry run* shows the line protocol of what the first sync
   would write; *Run now* writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `github_commit` | commit of a repository's default branch | its author's time | `repository`, `author` (`self`, `agent`, `bot`, `other`) | `count` (1), `additions`, `deletions`; `local_hour`, `local_weekday` |
| `github_workflow_run` | workflow run | its creation | `repository`, `workflow`, `event`, `visibility` | `count` (1), `succeeded`, `failed`, `cancelled` (1 or 0), `attempts`, `duration_s` |
| `github_job` | job that ran, every attempt | its start | `repository`, `workflow`, `job`, `runner` (`github-hosted`, `self-hosted`), `runner_label`, `conclusion`, `visibility` | `count` (1), `duration_s`, `queued_s`, `billable_min` |

- `author` is `agent` when one of the commit's authors, its git author or a
  `Co-authored-by` trailer, is in `TRIBUTARY_GITHUB_AGENTS`; else it goes by
  the git author: `bot` (a `[bot]` or `-bot` account), `self` (the token's
  account), `other` (anyone else, and an email no GitHub account claims:
  add it to your account for its commits to count as yours). A commit you
  made with an agent is the agent's: it is the share an agent wrote.
- Merge commits are left out (the commits they merge are counted), and so
  are forks (their history is mostly their upstream's).
- `failed` covers a run that failed, timed out or never started. A re-run
  keeps its run's creation time, so it overwrites the run's point:
  `attempts` says how many it took, the other fields how the last ended.
- `duration_s` of a run is from its last attempt's start to its last update;
  of a job, from its start to its end. `queued_s` is a job's wait for a
  runner.
- `runner` is `github-hosted` for GitHub's runners and `self-hosted` for
  yours (an actions-runner-controller scale set's `runner_label` is its
  name); a job cancelled before a runner took it has neither. Larger
  GitHub-hosted runners are counted as self-hosted.
- `billable_min` is what GitHub counts against a plan's included minutes:
  a GitHub-hosted job of a private repository, rounded up to the minute,
  Windows twice and macOS ten times. Public repositories and self-hosted
  runners are free: 0.
- Skipped jobs never ran: they are left out.

## How it syncs

- Commits: the first sync reads every repository's whole default branch,
  100 commits a request: a few hundred requests for years of history. Each
  later sync reads from the previous one, minus the lookback.
- Workflow runs cost a request each, for their jobs (the only place that
  says which runner ran one): an organisation running 200 a day costs 200
  requests a day, and a token gets 5,000 an hour. So the first sync reads
  `TRIBUTARY_GITHUB_HISTORY_DAYS`, a UTC day at a time, oldest first, and
  stops after `TRIBUTARY_GITHUB_MAX_RUNS` runs, or when fewer than 200
  requests of the hour are left: its message says how far it got, and the
  next sync carries on from that day. *Run now* an hour later goes on
  sooner.
- A run still in progress is read by a later sync (the lookback), as is a
  re-run within it.
- *Full resync* reads everything again, the history days included.
- Nothing is ever dropped: a run deleted on GitHub stays in InfluxDB.
- Server errors and GitHub's secondary rate limits are retried three times,
  waiting up to a minute. A spent hourly quota fails the sync, which the
  next one repeats. A redirect is refused: the token would follow it.

## Privacy

Repository, workflow and job names are tags, as a dashboard filters by them.
Nothing else of a commit or a run is written: no message, title, branch,
email or login. A workflow's name is read from the repository's workflow
list, never from the run, whose name may carry a pull request's title.

The token lives in the environment only, never in a log, the database or an
API answer; an error from GitHub is shown without it. Tests check all of
this.
