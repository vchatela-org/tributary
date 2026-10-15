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
every dashboard. A single number, such as a tile or a correlation, reads the
one source you pick in a variable, because two devices that disagree are never
averaged. There is one such picker per value (*Sleep from*, *Sleep score
from*, *HRV from*, *Resting HR from*, *Activity from*, *Workouts from*,
*Weight from*), with the same name on every dashboard. To open them on your
own source, save that source as the variable's default in your copy of the
dashboard. The rules, the series other tools write and the dashboards proposed
next are in [dashboards-design.md](dashboards-design.md).

## Healthspan

The long-run markers on one page. Its 12 tiles are:
- resting heart rate and HRV (median of the nights);
- VO₂ max;
- time asleep per night, and its spread from night to night;
- intensity minutes per week (the guideline is 150), steps per day, workout
  hours per week;
- weight, its change over the range, body fat;
- the share of medication doses taken.

A tile turns yellow when its value falls short of a published guideline:
time asleep below 7 hours a night, intensity minutes below 150 a week, steps
below 8,000 a day. The other markers have no colour: what is healthy for them
depends on age, sex or the person.

Below the tiles, each marker's trend draws every source in its own colour,
each night or day as a point and its 7-day average as a line. The same
guidelines are dashed lines.

Each tile reads the source picked for its value: *Resting HR from*, *HRV
from*, *Sleep from*, *Activity from*, *Workouts from*, *Weight from*. Each
lists the sources found plus `garmin`, the scale that rates its readings
first. Reads:
- `GarminStats` (garmin-grafana): `SleepSummary`, `DailyStats`, `VO2_Max`,
  `ActivitySummary`, `BodyComposition`;
- `SleepStats`: `sleep_session`;
- `BodyStats`: `body_composition`, `exercise`, `daily_activity`,
  `medication`.

![The Healthspan dashboard with demo data: twelve tiles, each labelled with the source it reads, then resting heart rate, HRV and time asleep from a watch and a strap](screenshots/grafana-healthspan-light.png)

## Sleep

Every night from every sleep tracker: `GarminStats..SleepSummary` and
`SleepStats..sleep_session`, naps left out.
- **Tiles**, from the source picked in *Sleep from*: time asleep, nights
  tracked, deep and REM share, time awake; the sleep score from *Sleep score
  from*.
- **Every night:** time asleep per night, one colour per source, against a
  7-hour line; the picked source's stages per night (deep, light, REM,
  awake); sleep score; deep and REM averages; time awake per source; the
  share of deep, light and REM per source.
- **Overnight:** resting heart rate (Sleep as Android's lowest heart rate of
  the night, as the app records no resting value), HRV, blood oxygen,
  breathing rate, recovery score and skin temperature.
- **Regularity:** usual bedtime and wake-up and their spread, from the
  sources that record a bedtime; the weekly spread of time asleep, and its
  nights, mean, p10 and p90 per source.
- **Coverage and naps:** the share of the last 30 days with a tracked night,
  per source (which device covers which years), and naps per week.
- **One night in detail** (folded): the hypnogram and the heart rate through
  the night, from the sources that store them (`SleepStats`: `sleep_stage`,
  `sleep_vitals`; `GarminStats..SleepIntraday`). Zoom the range to a night.

Variables: user, sources, *Sleep from*, *Sleep score from*.

![The Sleep dashboard with demo data: tiles from one source, time asleep per night from a watch and a strap, and the stages of each night](screenshots/grafana-sleep-light.png)

## Activity & fitness

How much you move and train, every source side by side. Reads `GarminStats`
(`DailyStats`, `ActivitySummary`, `VO2_Max`) and `BodyStats` (`daily_activity`,
`exercise`).
- **Tiles**, devices listed first: steps per day from *Activity from*;
  intensity minutes per week and VO₂ max (`garmin`); workouts, training hours
  per week and days with a workout from *Workouts from*.
- **Per day:** steps, strain, calories burned, each source as points and a
  7-day average.
- **Per week:** intensity minutes against the 150-minute guideline; VO₂ max,
  running and cycling.
- **Workouts per week, one line per source:** time, count and calories; a
  workout that two apps received is in both lines.
- **Heart-rate zones:** time per zone, from the source picked in *Workouts
  from*.
- **Workouts by sport,** from the source picked in *Workouts from*, by the
  sport it names: workouts per week, a table per sport (duration, strain,
  calories, heart rate), the share of time in each heart-rate zone, the
  strain of each workout.

Variables: user, sources, *Activity from*, *Workouts from*.

![The Activity and fitness dashboard with demo data: tiles, steps per day from a watch and a strap, strain and calories burned per day](screenshots/grafana-activity-light.png)

## Correlations

One domain against another. Each point is one pairing window, and each panel
reads the sources picked in *Sleep from*, *Sleep score from*, *HRV from* and
*Workouts from*.
- **A night and the day it ends:** time asleep, the morning's HRV, or the
  sleep score, against the Todoist tasks completed that day. Tiles give the
  average tasks after a night under 7 hours and after one of 7 hours or more,
  and after a score under 70 and of 70 or more (each device scores on its own
  scale).
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
local hour with the sleep score of the night after (from *Sleep score from*,
on the same noon-to-noon windows), books finished. Its screenshot is in the
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
with its status, delay and note.

**A medication and the nights:** set the time range to start on the day a
medication began, stopped or changed. Tiles compare the nights in the range
with the same length of time just before it: nights, time asleep, time
awake, awakenings, deep and REM sleep, sleep score and resting heart rate,
each from its picker. 30-night averages below draw every source. No date is
saved in the dashboard: the range is the date.

Variables: time zone, user, source, medication, *Sleep from*, *Sleep score
from*, *Resting HR from*.

![The Medication dashboard with demo data: doses taken, the share logged as taken, and a strip per medication](screenshots/grafana-medication-light.png)

## Nutrition & exercise

`BodyStats`: `nutrition`, `exercise`, and `body_composition` for the weight.
Food days logged, median intake and protein (on days with at least 3 meals
logged), calories per meal for each logged day, the macro split, a table per
day; workouts per day and per week by activity, workout time, the device
calorie adjustment, every workout; every scale's weigh-ins on the same time
axis, garmin-grafana's included.

**Eaten and burned:** on fully logged food days (lunch and dinner, or at least
3 meals), the calories eaten against every tracker's burn (a watch's resting
and active calories, on days it was worn; a strap's day). The daily balance
subtracts the burn of the tracker picked in *Activity from*; a day either side
lacks stays empty.

Variables: user, source (one at a time: the panels sum per day), *Activity
from*.

![The Nutrition and exercise dashboard with demo data: calories per meal per logged day and workout calories by activity](screenshots/grafana-nutrition-exercise-light.png)

## Todoist

`TodoistStats`: `todoist_completion`, `todoist_task_completed`,
`todoist_stats`. Completions, tasks per day, active days, the app's streak
and karma; completions per day with a 30-day average, per project, and by
priority; for one-off tasks, done by the due date or late, lead time and
postponements; every completion (task names only when
`TRIBUTARY_TODOIST_TASK_TEXT` is on). **Productivity and recovery:** the
tasks per day above the sleep score and the overnight HRV of every source,
on one time axis. Variables: time zone, project, user (whose nights).

![The Todoist dashboard with demo data: completions, streak and karma, completions per day and per project](screenshots/grafana-todoist-light.png)

## Habits & recovery

`SleepStats..journal`: a daily journal of yes or no questions (the Whoop
journal, from the Whoop connector's export import), each answer written with
the day's recovery, HRV, sleep and strain.
- **Every question:** a calendar of the answers, the yes rate per question,
  and the recovery and HRV on days answered yes against days answered no.
- **One question** (picked in *Question*): the share of yes days when the
  night was short (under 6.5 h), the recovery low (under 50) or the strain
  high (12 or more), against the days without, with the days each counts;
  the share of yes days over the last 30.

It is correlation, not cause: a factor and an answer can share a third
reason, and few days make the difference noise. Variables: time zone, user,
source, question.

## Strength training

garmin-grafana's `GarminStats`: `StrengthExerciseSet`, `StrengthHRZones` (a
Garmin watch's weight training, one point per set). Total volume, sets, reps
and workouts; the estimated one-rep max per exercise (Epley) and personal
records; volume per workout; volume and sets by category; the latest sets and
a summary per session; time per heart-rate zone and time under tension per
exercise; workouts per week, a calendar, and the sets Garmin could not
classify. Variables: time zone, exercise category, exercise.

## GitHub: commits and hosted minutes

`DevStats`, from the [GitHub connector](connectors/github.md): `github_commit`,
`github_actions_month`, `github_actions_usage`.
- **Hosted minutes** (GitHub's billing report): this month's minutes counted
  against the 2,000 a free plan includes, per account (yellow from 80%, red at
  2,000); each account's months against the same line; the minutes per
  repository over the range (public repositories' are free). Counted minutes
  keep only the private repositories' standard runners, with GitHub's
  multipliers (Windows ×2, macOS ×10).
- **Commits** on the default branches, merges and forks left out: totals by
  author (you alone, you with an agent, a bot, someone else), the share of your
  commits made with an agent, commits per week by author, and per repository
  for each account.

The connector writes one point per account and month, on its first day: the
monthly panels draw those, as InfluxQL has no calendar month. Variables:
time zone, account.
