# Todoist

[Todoist](https://todoist.com/) keeps every task you complete. Tributary
syncs them from the [Todoist API](https://developer.todoist.com/api/v1/)
into `TodoistStats` (see [the schema](../schema.md)): each completed task
with its details, each completion (recurring tasks included), the daily
karma, and your goals and streaks.

Why the API and not Todoist's export: backups and the per-project CSV
export only hold active tasks, not completed ones or archived projects.

## Setup

1. Copy your API token: Todoist, *Settings > Integrations > Developer*.
2. Set:

   | Variable | |
   |---|---|
   | `TRIBUTARY_TODOIST_TOKEN` | The API token. |
   | `TRIBUTARY_TODOIST_TASK_TEXT` | `true` to also write each task's name and labels (default `false`), see [Privacy](#privacy). |
   | `TRIBUTARY_TODOIST_LOOKBACK_DAYS` | Days read again before the last sync, for a task ticked off on a phone that synced later (default 3). |
   | `TRIBUTARY_TODOIST_INTERVAL_MINUTES` | Minutes between two syncs (default 1440, once a day). |

   Local days (`late_days`, the daily karma and stats points) are in
   `TRIBUTARY_TZ`.
3. In Tributary, *Dry run* shows the line protocol of what the first sync
   would write; *Run now* writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `todoist_task_completed` | completed task | its completion | `task_id`, `project`, `section`, `priority`, `recurring`, `subtask` | `count` (1), `lead_time_hours`, `postponed_count`, `duration_minutes`, `late_days`; `content`, `labels` with task text |
| `todoist_completion` | completion | the completion | `task_id`, `project` | `count` (1); `content` with task text |
| `todoist_karma` | day | local midnight | none | `karma` (the day's average) |
| `todoist_stats` | day | local midnight | none | `karma`, `completed_count` (lifetime), `daily_goal`, `weekly_goal`, `current_daily_streak`, `max_daily_streak`, `current_weekly_streak`, `max_weekly_streak` |

- **Count completions from `todoist_completion`.** Completing a recurring
  task only moves its due date, so the completed-tasks list (and
  `todoist_task_completed`) never has it; the activity log has one event
  per completion. `todoist_task_completed` carries the details (priority,
  due date, lead time...) of one-off tasks. The activity log is complete
  on a Pro plan; on a free one it goes back 7 days.
- `priority` is the app's: `p1` (urgent) to `p4`. `late_days` is the
  completion's local date minus the due date (negative: early), for one-off
  tasks only (a recurring task's due date has already moved on).
  `lead_time_hours` runs from the task's creation to its completion.
- `project` is the project's current name (archived projects included); a
  project deleted since keeps the name the activity log remembers, else
  `(unknown)`. A renamed project or section starts new series under its
  new name for the completions a sync reads again.
- Todoist returns only the last 7 days of karma: `todoist_karma` builds up
  from the first sync on. `todoist_stats` is today's state, rewritten by
  each sync of the day: a day's point holds what its last sync saw.
- No `source` and no `user` tag: this is the layout of the sync job this
  connector replaced, kept so its history and dashboard carry on. Todoist
  is the only writer of `TodoistStats`.

## How it syncs

- The first sync reads the whole history, from the account's creation, in
  3-month windows (the API's limit per request): a few hundred requests
  for years of tasks, a minute or two.
- Each later sync reads from the previous one, minus the lookback, to now,
  and rewrites it: harmless, the same points overwrite themselves. A sync
  missed for days is caught up by the next.
- *Full resync* reads the whole history again.
- Nothing is ever dropped: a completion undone or deleted in Todoist stays
  in InfluxDB.
- Rate limits (429) and server errors are retried three times, waiting up
  to a minute. A redirect is refused: the token would follow it.

## Privacy

Task names are free text, and InfluxDB often runs without authentication:
Tributary writes them only with `TRIBUTARY_TODOIST_TASK_TEXT=true` (then
`content` and `labels`; a dashboard listing completions needs them). Task
descriptions and comments are never read. Project and section names are
tags, as a dashboard filters by them.

The API's user profile (email, name, its own token) is never kept: only the
account's creation date is read, on a first or full sync. The token lives
in the environment only, never in a log, the database or an API answer;
an error from Todoist is shown without it. Tests check all of this.
