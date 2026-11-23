# WHOOP models

WHOOP's app turns a night and a day into a handful of numbers: how much sleep
you need tonight, the debt you carry, how well you slept against that need,
how regular your nights are, how recovered you are, how hard the day was, and
when to go to bed to wake up at a given time. This connector rebuilds those
numbers from the data your devices already put in InfluxDB, so you get them

- from a WHOOP strap, to check the models against the app;
- from a **Fitbit** (fitbit-grafana) or a **Garmin** (garmin-grafana), with or
  without a WHOOP subscription;
- side by side, for anyone wearing two devices and deciding which to keep.

The [Sleep planner & WHOOP models](../dashboards.md#sleep-planner--whoop-models)
dashboard draws them.

## A derived source: it reads other tools' values

Every other connector reads an API or a file. This one reads InfluxDB: the
nights, HRV, resting heart rate and day strain the Whoop connector writes
(`SleepStats..sleep_session`, `BodyStats..daily_activity`, source `whoop`), the
nights and minute heart rate fitbit-fetch writes (`FitbitHealthStats`), and
garmin-grafana's (`GarminStats`). Elsewhere Tributary reads another tool's
*timestamps* only ([compatible tools](../compatible-tools.md)); this connector
reads their **values**, because a model of them is the point. So:

- **It is off until you enable it** (`TRIBUTARY_WHOOP_MODEL_ENABLED=true`).
- It reads, never writes, the other tools' series. It writes its own two
  measurements only, `source=whoop_model` (one writer per source), with an
  `input` tag naming the device each value was rebuilt from.
- What it reads stays in memory for the run: nothing is cached or logged, and
  only the derived numbers below are written.

## Setup

| Variable | Default | |
|---|---|---|
| `TRIBUTARY_WHOOP_MODEL_ENABLED` | `false` | Switch it on. |
| `TRIBUTARY_WHOOP_MODEL_INPUTS` | `whoop,fitbit,garmin` | The devices to rebuild the scores from. One with no night in InfluxDB (or no database) is skipped. |
| `TRIBUTARY_WHOOP_MODEL_BASELINE_NEED_H` | WHOOP's, else `7.5` | Your baseline sleep need, in hours. Unset: the latest `need_baseline_s` the Whoop connector wrote, else 7.5 h. |
| `TRIBUTARY_WHOOP_MODEL_HR_MAX` | WHOOP's, else `190` | Max heart rate, for the strain from a heart rate. Unset: the 99.5th percentile of WHOOP's daily max HR, else 190. |
| `TRIBUTARY_WHOOP_MODEL_FITBIT_ASLEEP_BIAS_MIN`, `..._IN_BED_BIAS_MIN` | `0` | A Fitbit's median offset against WHOOP on time asleep and time in bed, in minutes (positive: it counts more), measured on nights both recorded. Taken off its nights, so its scores land on WHOOP's scale. |
| `TRIBUTARY_WHOOP_MODEL_GARMIN_ASLEEP_BIAS_MIN`, `..._IN_BED_BIAS_MIN` | `0` | The same for a Garmin. |
| `TRIBUTARY_WHOOP_MODEL_CONSTANTS` | `{}` | Refitted constants, a JSON object over the keys of `DEFAULTS` in `app/connectors/whoop_model.py` (e.g. `{"recovery": [6.6, 0.016, -0.5]}`). An unknown key fails the run. |
| `TRIBUTARY_WHOOP_MODEL_LOOKBACK_DAYS` | `7` | Nights recomputed before the last one written: late syncs, WHOOP's rescoring. |
| `TRIBUTARY_WHOOP_MODEL_START_DATE` | each input's first night | Where a first or full run starts. A first run reads the minute heart rate of every night since: years of a Garmin take minutes; set a date to start later. |
| `TRIBUTARY_WHOOP_MODEL_SCHEDULE` | `50 7,11,15,19 * * *` | Four runs a day: the night once the devices synced, then the strain so far. |

It uses `TRIBUTARY_INFLUXDB_URL`, `TRIBUTARY_INFLUXDB_USER` (the `user` of the
WHOOP series it reads, and of the points it writes) and `TRIBUTARY_TZ`.

## Refresh now

It runs on what is in InfluxDB at that moment. Before bed, that may be hours
old: the Whoop connector runs once a day, so the WHOOP strain the evening runs
see is the morning's, and fitbit-grafana reads workouts and sleep on its own
clock. **Refresh inputs and run**, on the connector's page, opens its refresh
page (`/connectors/whoop_model/refresh`), which the
[Sleep planner](../dashboards.md#sleep-planner--whoop-models) links to as
*Refresh now*. One button runs, in order:

1. the [refresh hook](../compatible-tools.md#refresh-hook-asking-a-tool-to-fetch-now)
   of each tool among the inputs that has one: fitbit-grafana for `fitbit`,
   garmin-grafana for `garmin`. Tributary asks them to fetch now, all at once,
   and waits for them to answer. A tool with no hook is read as it is;
2. the Whoop connector, when `whoop` is an input and it is configured, for
   WHOOP's strain so far (a run re-reads the open cycle);
3. the WHOOP models.

A failed step doesn't stop the next ones: the models still run on what is
there. The page then shows each tool's freshness line, and the planner's *Is
the data fresh?* row shows when each device last synced. Open the device's
phone app before you refresh: a refresh can't make it sync. The same chain is
`POST /api/connectors/whoop_model/refresh`; `GET` on that address reports it
step by step.

## The models

Reverse-engineered on one WHOOP member's history (600 nights, 2025-2026), from
the fields WHOOP's API and export give for each night. *Model vs app* is the
mean absolute difference between the model run on WHOOP's own inputs and the
app's value, over those nights.

| Number | Model | Model vs app |
|---|---|---|
| **Sleep need** | baseline + strain term + debt − nap credit. An exact identity in WHOOP's fields (`need_baseline_s + need_strain_s + need_debt_s − need_nap_s`). | 6 min (r 0.91) |
| strain term | `a·(S + b)^c` seconds, S = the day strain of the cycle before the night: about 3 min at strain 4, 12 at 10, 30 at 14, 75 at 20. | 1 min (r 0.99) |
| debt | `r × max(0, need − sleep)` of the night before, consecutive nights only. r = ½, or exactly 9/14 after a night scored above 87. The debt is part of the need it is taken from, so it halves night after night instead of piling up. | 5 min (r 0.92) |
| **Sleep performance** | `s1·min(100, sleep ÷ need %) + s2·efficiency % + s3·consistency % + s0`, within 0..100. | 3.3 (r 0.90) |
| **Sleep consistency** | `k1 ×` the mean overlap (intersection over union, %) of the night's sleep window with each of the 4 nights before, moved onto the same day, `+ k0`. | 3.2 (r 0.90) |
| **Recovery** | `100·σ(h·(ln HRV − the mean ln HRV of the 30 nights before) + p·performance + c)`, from the 8th night with an HRV. Resting heart rate and breathing rate add nothing once HRV is in. | 5.8 (r 0.90) |
| **Day strain** | WHOOP's own for a strap. From a heart rate: `a·ln(1 + L/b)`, L = a TRIMP over the cycle (sleep onset to the next one), Σ x·0.64·e^(k·x) per minute, x = (HR − resting HR) ÷ (max HR − resting HR), resting HR the device's 30-day mean; at most 21. a, b and k fitted on a Fitbit's minute heart rate against WHOOP's strain over a month, leaving out the days with logged strength work (see below). k comes out near 4, twice Banister's 1.92: WHOOP weighs the minutes near max heart rate much more, and with 1.92 the strain of a hard run read 3 to 5 low. A two-value `hr_strain` override (an older fit) keeps 1.92. | 0.6 on a Fitbit (r 0.98), 0.7 in leave-one-out, strength days aside; not re-measured on a Garmin since k |
| **Bedtime** | wake-up − target × need ÷ efficiency − time to fall asleep, target 100 % (*Peak*), 85 % (*Perform*) or 70 % (*Get by*), efficiency the 14-night median. Computed by the dashboard from `whoop_model_tonight`; the other way round, a bedtime gives the share of the need slept: (wake-up − bedtime − time to fall asleep) × efficiency ÷ need. | |

The constants (`DEFAULTS`) are WHOOP's algorithm as fitted, not a person's:
the baseline and the max heart rate, which are, come from your settings or
your own WHOOP data. Refit them on your own history with care: a refit on a
few weeks overfits.

What the models can't see: WHOOP adds a muscular load to weightlifting
sessions logged with its Strength Trainer, which a heart rate misses (the
strain from a heart rate is lower on those days); naps (a nap's credit is left
out); and WHOOP's own sleep staging: a device that counts less time awake
(a Fitbit counts about half of WHOOP's) gets more sleep, a higher efficiency
and a higher performance, unless its bias is set.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `whoop_model` | night and device | the night's end (the wake-up) | `input` (`whoop`, `fitbit`, `garmin`) | `need_s`, `need_base_s`, `need_strain_s`, `need_debt_s`, `asleep_s`, `in_bed_s`, `efficiency`, `latency_s`, `sleep_score`, `consistency`, `recovery`, `strain_prev`, `day_strain` (float), `day_complete` (int) |
| `whoop_model_tonight` | device | the end of its last night | `input` | `debt_s`, `base_s`, `efficiency`, `latency_s`, `need_s_strain8`, `need_s_strain12`, `need_s_strain16`, `strain_so_far`, `need_s_so_far`, `night_asleep_s`, `usual_asleep_s` (float), `partial`, `cut_short`, `run_s`, `night_start_s`, `night_end_s`, `synced_s`, `wake_day_s` (int) |

Fields are described in [the schema](../schema.md). A night is a device's
main sleep; it is keyed by the local date it ends on, and two nights are
consecutive when they end on consecutive days less than 36 hours apart.
`partial` is 1 when the device's heart rate stops before the end of its last
night: it had not synced yet, so that night is short and its debt too high
until the next run. `cut_short` is 1 when the last night has less than 60 %
of the usual sleep (the median of the 14 nights before) or ends 3 h or more
before the usual wake-up: the device synced, but its source had not finished
the night (Google Health has held a Fitbit night ending at 01:42 until noon,
while the heart rate kept syncing). Both clear at the first run after the
source rewrites the night. `run_s`, `night_start_s`, `night_end_s` and
`synced_s` (the device's last heart-rate sample) let the dashboard show how
old each of them is; `wake_day_s` is the local midnight of the coming
wake-up's day, from which it writes the bedtime as a clock time.

## Cursor and reruns

The cursor holds, per device, the end of the last night written. A run
recomputes from there minus the lookback, reading 40 more days before it (the
HRV baseline and the debt need the nights before), and rewrites those nights
in place (same keys). A first or full run starts at the start date, or at each
device's first night, and walks the history 120 days at a time, the minute
heart rate a week at a time.
