# The Grafana dashboards

Tributary draws no graphs: [grafana/dashboards/](../grafana/dashboards/) ships
dashboards that read InfluxDB, for the series Tributary writes and the ones
other tools write the same way ([the schema](schema.md)). The quick start
provisions them, with a datasource per database
([grafana/provisioning/](../grafana/provisioning/)). In a Grafana you already
run, import the JSON: each one finds its datasource by name (any InfluxDB
datasource whose name contains its database, `BodyStats` for example).

They ship with nothing saved in them. No user, source, project or medication
is preselected, and no panel is about one person's data: the lists fill from
your own series. The time zone the days are cut in is a variable (`tz`), set
to the same default as `TRIBUTARY_TZ`. The screenshots below show demo data.

## Reading & listening

`MediaStats`: `reading_session`, `book_finished`. Time per day and per week by
kind and source, sessions, active days, time by device, evening reading by
local hour, books finished. Its screenshot is in the
[README](../README.md#graphs-in-grafana).

## Body composition

`BodyStats`: `body_composition`, `body_levels`, `body_thresholds`. Per user,
the latest reading of every metric coloured by its level (the scale app's
own boundaries, from `body_thresholds`), a folded detail row per metric (gauge,
level, change over the range, history), and the history of every metric by
source. Variables: user, source.

![The Body composition dashboard with demo data: the latest reading of each metric, then the weight history and its trend](screenshots/grafana-body-composition-light.png)

## Medication

`BodyStats`: `medication`, from every source that writes it (MyTherapy,
Medisafe). Doses taken, the share logged as taken, missed or skipped, days
with a dose taken; a strip per medication of the share taken in each bucket;
a table per medication (first and last dose, scheduled, taken) and every dose
with its status, delay and note. Variables: user, source, medication.

![The Medication dashboard with demo data: doses taken, the share logged as taken, and a strip per medication](screenshots/grafana-medication-light.png)

## Nutrition & exercise

`BodyStats`: `nutrition`, `exercise`, and `body_composition` for the weight.
Food days logged, median intake and protein (on days with at least 3 meals
logged), calories per meal for each logged day, the macro split, a table per
day; workouts per day and per week by activity, workout time, the device
calorie adjustment, every workout; every scale's weigh-ins on the same time
axis. Variables: user, source (one at a time: the panels sum per day).

![The Nutrition and exercise dashboard with demo data: calories per meal per logged day and workout calories by activity](screenshots/grafana-nutrition-exercise-light.png)

## Todoist

`TodoistStats`: `todoist_completion`, `todoist_task_completed`,
`todoist_stats`. Completions, tasks per day, active days, the app's streak
and karma; completions per day with a 30-day average, per project, and by
priority; for one-off tasks, done by the due date or late, lead time and
postponements; every completion (task names only when
`TRIBUTARY_TODOIST_TASK_TEXT` is on). Variables: time zone, project.

![The Todoist dashboard with demo data: completions, streak and karma, completions per day and per project](screenshots/grafana-todoist-light.png)
