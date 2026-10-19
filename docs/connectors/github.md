# GitHub

[GitHub](https://github.com/) holds your repositories' commits, runs your
workflows and bills the minutes its hosted runners spend on them. Tributary
syncs all of it from the [GraphQL](https://docs.github.com/en/graphql) and
[REST](https://docs.github.com/en/rest) APIs into `DevStats` (see
[the schema](../schema.md)), for every organisation and account you give it a
token for:

- **Who wrote the commits:** you, an agent (a commit made with Claude, Copilot
  or OpenHands as a co-author, or one it made), a bot (Renovate,
  Dependabot...), or someone else; per repository, with the local hour.
  These are the commits GitHub's *Insights > Contributors* counts: the
  default branch's, without merge commits.
- **The workflow runs:** each completed run's workflow, event, conclusion,
  attempts and duration, so a dashboard can show the latest and the longest
  ones.
- **Hosted minutes against the included ones:** GitHub's own billing report,
  per day, repository and runner type, and a monthly total of what counts
  against the plan's included minutes (2,000 a month on the Free plan).

Self-hosted runners (actions-runner-controller...) are free, so not in the
billing report: their own metrics cover them, and the runs they executed are
still written. Grafana's GitHub data source queries one repository at a time
and keeps nothing.

## Setup

1. Create a token for each organisation or user to read: GitHub,
   *Settings > Developer settings > Personal access tokens > Fine-grained
   tokens*. Resource owner: the organisation (or your account); repository
   access: all repositories; permissions, all **read-only**:
   - repository: *Contents* and *Metadata*, for the commits, and *Actions*,
     for the workflow runs;
   - for an organisation, organisation permission *Administration*, for its
     billing report (you must be an owner or a billing manager of it);
   - for your account, account permission *Plan*, for yours.

   A fine-grained token reads one owner: hence a token per owner.
2. Set, for the first owner, then `_2`, `_3`... (up to `_9`) for the others:

   | Variable | |
   |---|---|
   | `TRIBUTARY_GITHUB_OWNER` | The organisation or user (`my-org`). |
   | `TRIBUTARY_GITHUB_TOKEN` | Its token. |
   | `TRIBUTARY_GITHUB_OWNER_2`, `TRIBUTARY_GITHUB_TOKEN_2`... | The next ones, numbered without a gap. |
   | `TRIBUTARY_GITHUB_AGENTS` | Commit authors that are agents: emails or GitHub logins, comma-separated (default `noreply@anthropic.com,Copilot,openhands-agent`: Claude, GitHub Copilot, OpenHands). |
   | `TRIBUTARY_GITHUB_HISTORY_DAYS` | Days of workflow runs a first or full sync reads (default 30). |
   | `TRIBUTARY_GITHUB_HISTORY_MONTHS` | Months of billing a first or full sync reads, the current one included (default 12). Commits are read whole. |
   | `TRIBUTARY_GITHUB_LOOKBACK_DAYS` | Days of commits and runs read again before the last sync (default 3). |
   | `TRIBUTARY_GITHUB_SCHEDULE` | A 5-field cron expression, in `TRIBUTARY_TZ` (default: daily at 04:15). Set it on the source's page instead, when you can: see [Scheduling](../scheduling.md). |
   | `TRIBUTARY_GITHUB_INTERVAL_MINUTES` | Deprecated: minutes between two syncs, counted from the start. Superseded by the schedule; still honoured so an existing deployment keeps working. |

3. In Tributary, *Dry run* shows the line protocol of what the first sync
   would write; *Run now* writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `github_commit` | commit of a repository's default branch | its author's time | `owner`, `repository`, `author` (`self`, `agent`, `bot`, `other`) | `count` (1), `additions`, `deletions`; `local_hour`, `local_weekday` |
| `github_workflow_run` | completed workflow run | its creation | `owner`, `repository`, `workflow`, `event`, `visibility` | `count` (1), `succeeded`, `failed`, `cancelled`, `attempts`, `duration_s`, `result` (1 success, 2 failed, 3 cancelled, 0 other) |
| `github_actions_usage` | day, repository and SKU of the billing report | the day (UTC) | `owner`, `repository`, `sku` | `minutes`, `gross_usd`, `discount_usd`, `net_usd`, `private` |
| `github_actions_month` | owner and month | its first day (UTC) | `owner` | `minutes`, `private_minutes`, `counted_minutes`, `gross_usd`, `net_usd` |

- `author` is `agent` when one of the commit's authors, its git author or a
  `Co-authored-by` trailer, is in `TRIBUTARY_GITHUB_AGENTS`; else `bot` when
  one is a bot (a `[bot]` or `-bot` account: Renovate opening its pull
  requests as you co-authors their squash commits); else it goes by the git
  author: `self` (the token's account) or `other` (anyone else, and an email
  no GitHub account claims: add it to your account for its commits to count
  as yours).
- Merge commits are left out (the commits they merge are counted), and forks
  are not read (their history is mostly their upstream's).
- A run is written when it completes, at its creation: a re-run keeps that
  time, so its point overwrites the first attempt's (`attempts` counts
  them). Runs still in progress are read again by the next sync.
  `duration_s` is `run_started_at` to `updated_at`; `workflow` is the
  workflow's own name, never the run's, which may carry a pull request's
  title. Every run is written, self-hosted (ARC) ones included: the billing
  report says which repositories hosted minutes were billed to.
- `sku` is GitHub's: `Actions Linux`, `Actions Windows`, `Actions macOS`,
  and a larger runner's own. `gross_usd` is the minutes' list price,
  `discount_usd` what the included minutes and public repositories' free
  minutes take off, `net_usd` what is billed.
- `private` is the repository's visibility when the day was read: a
  repository made public since counts as public for the days read after.
  One no longer listed (deleted) counts as private.
- `counted_minutes` is what the plan's included minutes count: the private
  repositories' standard runners, Windows twice and macOS ten times (larger
  runners are always billed). The monthly point exists because InfluxQL has
  no calendar month: a monthly graph reads it as is.

## How it syncs

- The first sync reads every repository's whole default branch, 100 commits a
  request (a few hundred requests for years of history), the last
  `TRIBUTARY_GITHUB_HISTORY_DAYS` of runs, 100 runs a request (a workflow
  list a repository), and the last `TRIBUTARY_GITHUB_HISTORY_MONTHS` of
  billing, a request a month.
- Each later sync reads the commits and runs from the previous one, minus the
  lookback, and the billing from the month before the previous sync's (GitHub
  completes a day's usage later): the same points overwrite themselves.
- Each owner is read with its own token; one that fails, or one of its parts
  (its commits, its runs, its billing), doesn't stop the others, and is read
  again by the next sync.
- *Full resync* reads everything again.
- Nothing is ever dropped.
- Server errors and GitHub's secondary rate limits are retried three times,
  waiting up to a minute. A redirect is refused: the token would follow it.

## Privacy

Owner, repository, workflow, event and SKU names are tags, as a dashboard
filters by them. Nothing else of a commit or a run is written: no message,
branch, run name, email or login, and of the billing report only its
numbers.

The tokens live in the environment only, never in a log, the database or an
API answer; an error from GitHub is shown without them. Tests check all of
this.
