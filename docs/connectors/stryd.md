# Stryd

A [Stryd](https://www.stryd.com/) Duo (a pod on each shoe) records the path of
each foot through the stride, which Stryd draws as its *Footpath*. Its power,
form power, leg spring stiffness and the other running figures already reach
the watch's FIT file through Stryd's Connect IQ data field (garmin-grafana's
`stryd` option reads them); the Footpath does not. Stryd sends it as an export,
on request to its support, and Tributary writes it into `BodyStats..footpath_step`
`..footpath_loop` and `..footpath_segment` (see [the schema](../schema.md)). The
[Running footpath](../dashboards.md#running-footpath) dashboard draws it.

Upload only: the export arrives once, on request.

## Setup

1. Ask Stryd's support for an export of your Footpath data. It comes as a zip
   of `<id>_footdata_<n>.json` files, one per recording.
2. In Tributary, *Dry run* next to the file shows the line protocol of what the
   import would write; *Import file* writes it.

A whole export is large (1.2 GB zipped for three years of runs) and its import
takes a few minutes. Above `TRIBUTARY_MAX_UPLOAD_MB` (200 MB by default), raise
it (the upload is kept on the data volume until it is imported, so it needs
the room), or upload the export in parts: a zip of some of its files, or a
single `.json`. An import only adds and rewrites (below), so the parts may come
in any order, and again. Keep the files of a run in the same part: each part
draws a run's typical loop, and its loops km by km, from the copies it holds.

There is nothing to configure: the `user` tag comes from
`TRIBUTARY_INFLUXDB_USER`.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `footpath_step` | sampled step | the step's second | `side` (`left`, `right`), `pace` (`4:30`...) | `lift_cm`, `back_cm`, `reach_cm`, `width_cm`, `touchdown_cm`, `speed_mps`, `power_w`, `incline` (float) |
| `footpath_loop` | point of a run's typical loop, per foot | the run's start, plus `phase` seconds | `run` (`2026-03-01 08:15`), `side` | `phase`, `steps` (int), `x_cm`, `y_cm`, `z_cm`, `speed_mps` (float) |
| `footpath_segment` | point of the loop of each 500 m of a run, per foot | the run's start, plus 64 s per segment before it, plus `phase` seconds | `run`, `side` | `phase`, `steps`, `elapsed_s` (int), `km`, `km_end`, `x_cm`, `y_cm`, `z_cm`, `speed_mps` (float); on `phase` 0, `lift_cm`, `back_cm`, `reach_cm`, `width_cm`, `touchdown_cm` (float) |

- **The path.** For a sample of the steps (one every 2 to 7 seconds per foot),
  the export holds the foot's path over one gait cycle, from mid-stance to the
  next, at 160 Hz, in metres, with the forward motion taken out: x forward, y
  sideways (the left foot's negative), z up. It starts and ends at 0.
- **A step's figures**, in cm from its path: `lift_cm` the highest point,
  `back_cm` how far behind mid-stance the foot travels after push-off,
  `reach_cm` how far ahead it swings, `width_cm` its sideways range,
  `touchdown_cm` where it meets the ground again (the first point under 1 cm
  after the highest one), ahead of mid-stance. With the step's `speed_mps`,
  `power_w` and `incline` (a ratio), as Stryd measured them. `pace` is the
  15 s/km band of its speed, by its lower bound (`4:30` is 4:30 to 4:44 per
  km; `2:30` and `8:00` take everything faster or slower), so a dashboard
  compares the form at the same pace: the loop grows with speed.
- **Only running steps**: a step slower than 2 m/s (8:20 per km: walking, a
  stop) is left out.
- **A run's typical loop**, per foot: the median, point by point, of its steps
  within 5% of its median speed (all its steps when fewer than 10 are), each
  resampled to 64 points of its cycle. `steps` and `speed_mps` say how many
  steps and at what speed. A run is a stretch of steps with no gap over 10
  minutes; `run` is its local start, in `TRIBUTARY_TZ`, to the minute.
- **The run km by km**, per foot: the same median loop for each 500 m of the
  run, from all its steps there (at least 3; whatever their pace: a hill or a
  rep is what the segment shows), so a dashboard draws the stride at km 3, or
  from km 5 to 8, and how lift, back swing, reach and touchdown change along
  the run. `km` and `km_end` bound the segment (the last one ends where the
  run does), `elapsed_s` is its first step's time after the run's start. The
  export holds no distance: it is the steps' speeds summed over time, from the
  run's first running step (walking steps count; two samples more than 30 s
  apart count as a stop). On three years of runs it was within 2 to 3% of the
  watch's distance for 80% of them (median +0.4%).
- **The export repeats itself.** A run synced from the phone and uploaded again
  later is there two to five times, whole or in part, under different names.
  A step is the same in every copy (same foot, same timestamp): the steps are
  merged on their foot and second before anything is written, and the run's
  message counts the repeats. A point written again lands on its own key, so
  importing the export again, or in parts, rewrites the same points. Nothing
  is ever dropped (no `rebuild_own_source`).
- **Left out:** the steps' velocities, their angles (in the newer files only)
  and the `scale_*` factors: the path is already in metres, and draws the
  Footpath on its own.

## Privacy

The export names the account (`user_id`), the pods' Bluetooth addresses
(`pod_id`) and the phone's file paths. None of them is read into a point: only
the steps' numbers, their times, the foot and the pace band, and the run's
local start reach InfluxDB, under `source=stryd`.
