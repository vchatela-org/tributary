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

A value that several sources measure (weight, heart rate, sleep, steps,
workouts) is drawn as one series per source, each source in the same colour on
every dashboard. A single number, such as a tile, reads the one source you pick
in a variable, because two devices that disagree are never averaged. The rules,
the series other tools write and the dashboards proposed next are in
[dashboards-design.md](dashboards-design.md).

## Healthspan

The long-run markers on one page. Its 12 tiles are:
- resting heart rate and HRV (median of the nights);
- VO₂ max;
- time asleep per night, and its spread from night to night;
- intensity minutes per week (the guideline is 150), steps per day, workout
  hours per week;
- weight, its change over the range, body fat;
- the share of medication doses taken.

Below the tiles, each marker's trend draws every source in its own colour,
each night or day as a point and its 7-day average as a line.

Each family of tiles reads the source picked in a variable: *Heart from*,
*Sleep from*, *Activity from*, *Weight from*. Each lists the sources found
plus `garmin`, the scale that rates its readings first. Reads:
- `GarminStats` (garmin-grafana): `SleepSummary`, `DailyStats`, `VO2_Max`,
  `ActivitySummary`, `BodyComposition`;
- `SleepStats`: `sleep_session`;
- `BodyStats`: `body_composition`, `exercise`, `daily_activity`,
  `medication`.

![The Healthspan dashboard with demo data: twelve tiles, each labelled with the source it reads, then resting heart rate, HRV and time asleep from a watch and a strap](screenshots/grafana-healthspan-light.png)

## Sleep

Every night from every sleep tracker: `GarminStats..SleepSummary` and
`SleepStats..sleep_session`, naps left out.
- **Tiles**, from the source picked in *Tiles from*: time asleep, nights
  tracked, deep and REM share, time awake, sleep score.
- **Every night:** time asleep per night, one colour per source, against a
  7-hour line; the picked source's stages per night (deep, light, REM,
  awake); sleep score; deep and REM averages.
- **Overnight:** resting heart rate, HRV, blood oxygen, breathing rate,
  recovery score and skin temperature.
- **Regularity:** usual bedtime and wake-up and their spread, from the
  sources that record a bedtime; the weekly spread of time asleep, every
  source.

Variables: user, sources, *Tiles from*.

![The Sleep dashboard with demo data: tiles from one source, time asleep per night from a watch and a strap, and the stages of each night](screenshots/grafana-sleep-light.png)

## Activity & fitness

How much you move and train, every source side by side. Reads `GarminStats`
(`DailyStats`, `ActivitySummary`, `VO2_Max`) and `BodyStats` (`daily_activity`,
`exercise`).
- **Tiles**, from the source picked in *Tiles from*, devices listed first:
  steps per day, intensity minutes per week, VO₂ max, workouts, training
  hours per week, days with a workout.
- **Per day:** steps, strain, calories burned, each source as points and a
  7-day average.
- **Per week:** intensity minutes against the 150-minute guideline; VO₂ max,
  running and cycling.
- **Workouts per week, one line per source:** time, count and calories; a
  workout that two apps received is in both lines.
- **Heart-rate zones:** time per zone, from the picked source.

Variables: user, sources, *Tiles from*.

![The Activity and fitness dashboard with demo data: tiles, steps per day from a watch and a strap, strain and calories burned per day](screenshots/grafana-activity-light.png)

## Correlations

One domain against another. Each point is one pairing window, and each panel
reads the sources picked in *Sleep from*, *Heart from* and *Workouts from*.
- **A night and the day it ends:** time asleep, or the morning's HRV,
  against the Todoist tasks completed that day. Two tiles give the average
  tasks after a night under 7 hours and after one of 7 hours or more.
- **An evening and the night after (noon to noon):** evening reading (18:00
  to 03:00, every reading source) against the time asleep, with the same
  split; workout minutes against the next night's HRV.
- **Weeks:** training hours against the week's HRV; sleep against tasks.

Reads `SleepStats`, `GarminStats`, `BodyStats`, `TodoistStats` and
`MediaStats`. Its range defaults to a year, so the clouds have enough points.

![The Correlations dashboard with demo data: time asleep and HRV against the tasks done that day, evening reading against the night after](screenshots/grafana-correlations-light.png)

## Reading & listening

`MediaStats`: `reading_session`, `book_finished`. Time per day and per week by
kind and source, sessions, active days, time by device, evening reading by
local hour, books finished. Its screenshot is in the
[README](../README.md#graphs-in-grafana).

## Body composition

`BodyStats`: `body_composition`, `body_levels`, `body_thresholds`, and
garmin-grafana's `GarminStats..BodyComposition`. Per user:
- the latest reading of every metric, coloured by its level (the scale
  app's own boundaries, from `body_thresholds`);
- a folded detail row per metric: gauge, level, change over the range,
  history.

The tiles and detail rows read one source, the one picked in *Readings from*
(a scale that rates its readings is listed first). The history of every
metric draws each source in its own colour, with a trend per source.
Variables: user, sources, *Readings from*.

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
axis, garmin-grafana's included. Variables: user, source (one at a time: the
panels sum per day).

![The Nutrition and exercise dashboard with demo data: calories per meal per logged day and workout calories by activity](screenshots/grafana-nutrition-exercise-light.png)

## Todoist

`TodoistStats`: `todoist_completion`, `todoist_task_completed`,
`todoist_stats`. Completions, tasks per day, active days, the app's streak
and karma; completions per day with a 30-day average, per project, and by
priority; for one-off tasks, done by the due date or late, lead time and
postponements; every completion (task names only when
`TRIBUTARY_TODOIST_TASK_TEXT` is on). Variables: time zone, project.

![The Todoist dashboard with demo data: completions, streak and karma, completions per day and per project](screenshots/grafana-todoist-light.png)
