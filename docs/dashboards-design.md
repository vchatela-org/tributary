# Dashboards: design

How the shipped dashboards treat a value that several sources measure, which
dashboards exist and which are only proposed. The dashboards themselves are
described in [dashboards.md](dashboards.md); Tributary's own series, in
[the schema](schema.md).

## One value, several sources

The same number often comes from more than one tracker. A smart scale and a
watch both weigh you; a watch and a strap both measure the night's heart rate
variability; a watch, a strap and a food diary all log a run.

| Value | Sources | Where |
|---|---|---|
| Weight, body fat, BMI, water, bone | a scale (`renpho`), a food diary (`myfitnesspal`, weight only), `garmin` | `BodyStats..body_composition`; `GarminStats..BodyComposition` |
| Resting heart rate, HRV | `garmin`, a strap (`whoop`) | `GarminStats..SleepSummary`; `SleepStats..sleep_session` |
| Sleep: time asleep, stages, score, SpO₂, breathing rate | `garmin`, `whoop` | `GarminStats..SleepSummary`; `SleepStats..sleep_session` |
| Steps, calories burned | `garmin`, `whoop` | `GarminStats..DailyStats`; `BodyStats..daily_activity` |
| Workouts: count, time, calories, heart-rate zones | `garmin`, `myfitnesspal` (which also receives the watch's workouts), `whoop` | `GarminStats..ActivitySummary`; `BodyStats..exercise` |
| Recovery, skin temperature, strain | `whoop` today, any source writing the same fields | `SleepStats..sleep_session`; `BodyStats..daily_activity` |
| Intensity minutes, VO₂ max | `garmin` only | `GarminStats..DailyStats`, `..VO2_Max` |

Rules every panel follows:

1. **A graph draws one series per source, never a merge.** Tagged
   measurements are grouped by `source`; a `GarminStats` query, which has no
   `source` tag, is named `garmin`. Two scales are two lines, two watches two
   lines.
2. **A source keeps one colour on every dashboard.** The known sources have
   fixed overrides, from a validated categorical palette. A source nobody
   listed gets a colour derived from its name (`palette-classic-by-name`), so
   it does not change when another source appears. Tests check that a source
   never gets two colours, that two sources never share one in a panel, and
   that every per-source graph is coloured by name.

   | Source | Colour | | Source | Colour |
   |---|---|---|---|---|
   | `renpho` | blue `#3987e5` | | `kindle` | blue `#3987e5` |
   | `garmin` | orange `#d95926` | | `audiobookshelf` | orange `#d95926` |
   | `myfitnesspal` | aqua `#199e70` | | `audible` | aqua `#199e70` |
   | `whoop` | violet `#9085e9` | | | |
   | `sleep_as_android` | aqua `#199e70` | | | |

   Some sources share a graph: a scale, a diary and a watch; a watch, a diary
   and a strap; a watch, a strap and a phone (sleep); three reading apps. Each of those sets passes the palette
   validator, in light and dark, for every pair. A colour is reused only
   across domains that never share a graph, and a legend always names the
   series.
3. **A single number reads one source, chosen by the reader, per value.**
   Otherwise a tile, a stage breakdown or a correlation would average two
   devices that disagree. Each value has its own picker, under the same name
   on every dashboard that reads it:

   | Picker | Label | Reads |
   |---|---|---|
   | `sleep_from` | *Sleep from* | time asleep, stages, time awake, bedtime and wake-up |
   | `score_from` | *Sleep score from* | sleep score |
   | `hrv_from` | *HRV from* | overnight HRV |
   | `rhr_from` | *Resting HR from* | resting heart rate |
   | `activity_from` | *Activity from* | steps and daily totals |
   | `workouts_from` | *Workouts from* | workouts, their time and heart-rate zones |
   | `weight_from` | *Weight from*, *Readings from* | weight and body composition |

   One picker per value, not per family, because a device can be the better
   source of one value and not of another: a strap's HRV, a watch's sleep
   stages. A picker lists the sources found in the tagged measurements plus
   `garmin`. The shipped dashboards preselect nothing, so the first one is
   used. The order puts the richest first: a scale that rates its readings
   (`body_levels`), a device writing daily totals (steps), then `garmin`, then
   a diary. A deployment that has a preferred source pins it as the picker's
   default in its own copy of the dashboard (Grafana's *Save dashboard* with
   *Update default variable values*, or the `current` value of the variable
   in the JSON it provisions). A dashboard provisioned from a file is
   overwritten by the next provisioning, so the copy is where that default
   stays. A test checks that every single-source query reads the picker of
   its own value. A `GarminStats` query honours the picker with
   `'garmin' = '${hrv_from}'`, a constant InfluxQL folds to true or false.

   **Sources are never stitched into one series.** Someone who wore a watch
   for years and then a strap could want one long series, the watch's
   values up to the switch and the strap's after. Two devices score sleep
   with different algorithms, and measure HRV over different parts of the
   night. Joined at the switch date, the series steps there, and a
   correlation over the years reads that step as an effect. A correlation
   reads the picked source alone, over the time it covers. For the years
   before, switch the picker to the device worn then.
4. **Counts of distinct events may be added across sources.** A dose, a
   reading session or a task has exactly one writer. Measured values never
   are: weight, heart rate, sleep, or a workout that two apps both received.

## Series written by other tools

Tributary's own series are in [the schema](schema.md). Beside them, the
dashboards read the following.
- `GarminStats` as garmin-grafana writes it: its measurement and field
  names, grams for weights, and an `END` marker point after each workout,
  skipped by counting a duration. It is assumed to hold one person: it has no
  `user` tag, and garmin-grafana's optional `User_ID` is not filtered.
  Checked against garmin-grafana's `main` branch, October 2026.
- A `SleepSummary` night counts when it has time asleep and REM
  (`"sleepTimeSeconds" > 0 AND "remSleepSeconds" > 0`, on every query, a test
  checks). Without the filter a second copy of each night, written under
  another `Device` tag with its stages but no time asleep, adds itself to
  every stage sum. The nights a watch scored before it had sleep staging (a
  movement-only algorithm: no REM, and 5 to 12 hours of "deep" sleep) are
  left out with it.
- `SleepStats` and two `BodyStats` series that Tributary's Whoop connector
  writes with `source=whoop` and a `user` tag ([the schema](schema.md)), listed
  here for the fields the dashboards read: any other writer of the same names
  (the Sleep as Android connector in `sleep_session`, its `#hashtags` in
  `tags`) is drawn the same way.

| Database | Measurement | Fields the dashboards read | Written by |
|---|---|---|---|
| `GarminStats` | `SleepSummary` | `sleepTimeSeconds`, `deepSleepSeconds`, `lightSleepSeconds`, `remSleepSeconds`, `awakeSleepSeconds`, `sleepScore`, `restingHeartRate`, `avgOvernightHrv`, `averageSpO2Value`, `averageRespirationValue` (one point per night, at its end) | garmin-grafana |
| `GarminStats` | `SleepIntraday` | `SleepStageLevel` (0 deep, 1 light, 2 REM, 3 awake), `heartRate`: the samples of a night | garmin-grafana |
| `GarminStats` | `DailyStats` | `totalSteps`, `activeKilocalories`, `bmrKilocalories`, `moderateIntensityMinutes`, `vigorousIntensityMinutes` | garmin-grafana |
| `GarminStats` | `ActivitySummary` | `elapsedDuration`, `calories`, `hrTimeInZone_1`...`_5` | garmin-grafana |
| `GarminStats` | `VO2_Max` | `VO2_max_value`, `VO2_max_value_cycling` | garmin-grafana |
| `GarminStats` | `BodyComposition` | `weight`, `bodyFat`, `bodyWater`, `boneMass`, `bmi`, `visceralFat` | garmin-grafana |
| `SleepStats` | `sleep_session` | tag `kind` (`night`, `nap`: naps are left out); `asleep_s`, `deep_s`, `light_s`, `rem_s`, `awake_s`, `bedtime_min`, `wake_min` (int); `score`, `resting_hr`, `hrv_rmssd_ms`, `avg_spo2_pct`, `respiration_rate`, `recovery_score`, `skin_temp_c` (float). One point per sleep, at its end | Tributary: `whoop`, `sleep_as_android` |
| `BodyStats` | `daily_activity` | `steps` (int); `strain`, `energy_kcal` (float). One point per physiological day, at its start | Tributary: `whoop` |
| `BodyStats` | `exercise` | beyond MyFitnessPal's fields: `zone1_s`...`zone5_s` (int, seconds per heart-rate zone) | Tributary: `whoop`, next to `myfitnesspal` |

If a dashboard's datasource does not exist, those series are left out. If the
database does not exist yet, the panels that read it show a warning and draw
the other sources.

### Whoop, without Postgres

No panel reads Whoops' PostgreSQL. The Whoop connector writes the series
above, so these panels draw a `whoop` series and list it in their pickers with
no dashboard change; without it, they show the other sources.
- **Sleep:**
  - every graph: time asleep, stages, score, deep and REM, resting heart
    rate, HRV, SpO₂, breathing rate, recovery score, skin temperature;
  - the regularity row: bedtime and wake-up, their spread;
  - the tiles, when picked.
- **Healthspan:**
  - the resting heart rate, HRV, time asleep, workout time and steps
    trends;
  - the matching tiles, when picked.
- **Activity & fitness:**
  - steps, strain and calories burned per day;
  - workout time, count and calories per week;
  - heart-rate zones and the tiles, when picked.
- **Correlations:** time asleep, HRV and workouts, when picked.
- **Nutrition & exercise:** the workout panels, when `whoop` is the source
  picked.

The Whoop journal (`SleepStats..journal`) is not drawn yet: see *Habits and
recovery* below.

## Audit of the dashboards that existed

| Dashboard | Finding | Fix |
|---|---|---|
| Body composition | The tiles, gauges and per-metric history read *every* selected source as one series: two scales zig-zag, and "last" and "change" mix devices. The history row ignored the watch's weigh-ins. Renpho was Grafana's named `blue`, not the palette's. | The tiles and metric rows read one source (*Readings from*, the rated scale first). The history row draws every source in fixed colours, `garmin` included (weight, fat mass, body fat, water, bone, BMI, visceral fat), with one trend per source. |
| Nutrition & exercise | *Weight, every scale* left out `garmin`, and its colours differed from Body composition's. | `garmin` added, in the same colours. The diary panels keep their single *source*, because they add up meals and workouts per day. |
| Reading & listening | Audible's orange was off-palette and close to Audiobookshelf's. | Audible takes the palette's aqua. |
| Medication | It counts doses: distinct events, one app each, added across apps (rule 4). | None. |
| Todoist | One writer, no `source` tag. | None. |

## Catalogue

Ranked by what each tells you that no single app does. The built ones are
described in [dashboards.md](dashboards.md).

| Rank | Dashboard | Question | Reads | Status |
|---|---|---|---|---|
| 1 | **Healthspan** | Are the long-run markers moving the right way? Resting heart rate, HRV, VO₂ max, sleep and its regularity, activity against the 150-minute guideline, weight and body fat, medication taken. | `GarminStats`: `SleepSummary`, `DailyStats`, `VO2_Max`, `ActivitySummary`, `BodyComposition`; `SleepStats..sleep_session`; `BodyStats`: `body_composition`, `body_levels`, `exercise`, `daily_activity`, `medication` | Built |
| 2 | **Sleep** | How long, how deep and how regular each night is, and what the night's heart said, per device. | `GarminStats..SleepSummary`; `SleepStats..sleep_session` | Built |
| 3 | **Correlations** | Does a short night cost the next day's tasks? Does evening reading change the night? Does training load show in HRV? | `SleepStats`, `GarminStats`, `TodoistStats..todoist_completion`, `MediaStats..reading_session`, `BodyStats`: `exercise`, `daily_activity` | Built |
| 4 | **Activity & fitness** | How much you move and train, per device: steps, strain, calories burned, intensity minutes, workouts, heart-rate zones, VO₂ max. | `GarminStats`: `DailyStats`, `ActivitySummary`, `VO2_Max`; `BodyStats`: `daily_activity`, `exercise` | Built |
| 5 | Weekly review | This week against the four before: sleep, workouts, reading, tasks, doses, weight. | all of the above, `time(1w)` | Proposed |
| 6 | Energy balance | Intake against the calories burned, and the weight trend that follows. | `BodyStats`: `nutrition`, `daily_activity`, weights; `GarminStats..DailyStats` | Proposed |
| 7 | Habits and recovery | Which journal answers (an evening habit, a late meal) go with a higher or lower recovery the next morning? | `SleepStats`: `journal` (Whoop export), `sleep_session` | Proposed: the Whoop connector writes `journal` |
| 8 | Medication and vitals | Resting heart rate, HRV and blood pressure across the periods a medication was taken. | `BodyStats..medication`, `GarminStats`: `DailyStats`, `BloodPressure` | Proposed |
| 9 | Stress and body battery | The watch's daytime stress and recovery. | `GarminStats`: `StressIntraday`, `BodyBatteryIntraday` (garmin-grafana's own dashboard covers it) | Proposed |
| 10 | Shopping and digital life | Takeback's purchases and activity counts. | `ShoppingStats`, `DigitalStats` (not yet in the schema) | Proposed |

Every new dashboard has these variables, with nothing saved in them:
- a datasource per database (`/BodyStats/i`, `/GarminStats/i`,
  `/SleepStats/i`...);
- the time zone (`tz`, at `TRIBUTARY_TZ`'s default);
- the user;
- the source pickers above.

## Limits

- **No clock time from a timestamp.** InfluxQL cannot read one, so bedtime
  and wake-up regularity come from the sources that write `bedtime_min` and
  `wake_min`. Garmin's summary has neither, so its regularity falls back to
  the spread of time asleep.
- **A correlation pairs per window.** It pairs per local day ("a night and
  the day it ends"), per noon-to-noon window ("an evening and the night
  after") or per week. InfluxQL cannot shift one series by a day.
- **A physiological day starts at sleep onset.** `daily_activity` is stamped
  at the start of the day it covers, usually the evening before. The steps,
  strain and calories graphs draw its points as written: one per day,
  without regrouping by calendar day.
