# Scheduling

Each connector runs on a **5-field cron expression**, in `TRIBUTARY_TZ`:

    ┌──────── minute (0-59)
    │ ┌────── hour (0-23)
    │ │ ┌──── day of month (1-31)
    │ │ │ ┌── month (1-12)
    │ │ │ │ ┌ day of week (0-6, Sunday is 0)
    * * * * *

Set it on the connector's page: pick a preset (*Manual only*, *Every hour*,
*Every 6 hours*, *Daily at a time*, *Weekly on a day and time*) or type an
expression under *Custom (cron)*. The page shows the five next runs, so a typo
is visible before it matters. `PUT /api/connectors/{key}/schedule` does the same
from a script; `{"preset": "daily", "at": "03:10"}` is expanded server-side, so
nothing has to build an expression in JavaScript.

## Where a value comes from

The page always says which of these is in force:

1. **What you saved in the UI** — stored in SQLite (`connector_schedules`), next
   to the run history. It survives a restart and a container rebuild, and it
   needs no redeploy.
2. **`TRIBUTARY_<KEY>_SCHEDULE`** — the same expression, from the environment, for
   a deployment that would rather keep it in its manifests.
3. **`TRIBUTARY_<KEY>_INTERVAL_MINUTES`** — deprecated, still honoured so an
   existing deployment keeps working on upgrade. An interval counts from the
   start, which cron cannot always say: a value that divides an hour maps
   exactly (`15` → `*/15 * * * *`), one that is a whole number of hours maps to
   the hour (`120` → `0 */2 * * *`), and "a day or longer" uses the connector's
   default daily time. Anything else (90 minutes, say) has no cron equivalent and
   falls back to the default with a word about it on the page.
4. **The connector's own `default_schedule`** — daily for the sources that read an
   API, at staggered night-time minutes so two runs never overlap, and `None`
   for an upload-only source (it is imported by hand).

*Reset to default* on the page deletes the saved value (1), so (2)–(4) apply
again. Choosing *Manual only* is different: it is **saved** as "no schedule" and
outranks the environment too — asking for no schedule by name should not be
undone by a leftover variable.

## What a schedule does and does not do

- **A missed run happens once.** If the app was down at the scheduled time, the
  run happens shortly after the next start, never once per missed occurrence.
  The first run of a connector that has never run also comes shortly after the
  start, so a fresh install sees data without waiting for the next night.
- **Times are absolute.** A restart no longer shifts a schedule the way the old
  "every N minutes from the start" did.
- **Nothing runs at startup itself.** The first scheduled run waits
  `TRIBUTARY_FIRST_RUN_DELAY` seconds: a slow sync must not hold up the liveness
  probe.
- **A schedule change never interrupts a run.** The thread finishes what it is
  doing, then re-plans; a run already queued keeps its place.
- **A dry run, a check or an import does not move the schedule.** They are not
  the scheduled run: the next run stays where the expression says.
- **One run at a time.** Connectors run one after another in a single process
  (`app/scheduler.py`), which is why the defaults are staggered.
- **A hidden connector keeps running.** Hiding one in Settings is display only.
