# Sleep as Android

[Sleep as Android](https://sleep.urbandroid.org/) tracks sleep on a phone or a
wearable. It has no API, so Tributary takes the backup you import in its UI and
writes it into `SleepStats..sleep_session`, `..sleep_stage` and `..sleep_vitals`
(see [the schema](../schema.md)).

The backup (*Settings → Backup → Export data*) is the app's **whole history**, so
an import **replaces** the connector's three series: a session the export no
longer holds is one the app no longer knows. The run says so ("after dropping
its own series"). Upload only: Tributary never watches a folder for this
connector, since a backup is the whole history a watched folder's rebuild would
assume.

## Setup

1. In Sleep as Android, *Settings → Backup → Export data*; keep the
   `sleep-export.zip` (it holds `sleep-export.csv`). The CSV on its own works
   too.
2. In Tributary, *Dry run* next to the file shows the line protocol of what the
   import would write; *Import file* writes it.

There is nothing else to configure but two optional settings: the `user` tag
comes from `TRIBUTARY_INFLUXDB_USER`, and the sessions' free-text comments are
written only with `TRIBUTARY_SLEEP_AS_ANDROID_NOTES=true` (below).

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `sleep_session` | sleep session | its end (the wake) | `kind` (`night`, `nap`) | the contract's, plus the app's own (below) |
| `sleep_stage` | hypnogram segment | the segment's start | -- | `level` (int), `stage` (str), `duration_s` (int) |
| `sleep_vitals` | recorded sample | the sample's time | -- | `hr_bpm`, `spo2_pct`, `sdnn_ms` (float, whichever it has) |

- **A session's fields** are the contract's (`start`, `in_bed_s`, `asleep_s` as
  the app reports it — its `Hours` shortened by `LenAdjust` — the exclusive
  `awake_s`/`light_s`/`deep_s`/`rem_s`, `cycles`, `tz_offset_min`,
  `bedtime_min`, `wake_min`) and the app's own: `avg_hr`, `min_hr`, `max_hr`
  from its heart-rate samples, `deep_pct`, `rating`, `sdnn_ms` (an **SDNN** HRV,
  a different statistic from WHOOP's `hrv_rmssd_ms`), `snore_s`, `noise_level`,
  `tz` (the zone's IANA name), and `manual` (true for a session you entered by
  hand: its cycles are `-1`).
- **A session is a nap** when it starts in the daytime (10:00-19:00) and is
  short (under 4 h). The app has no nap flag; the short 07:00-10:00 records are
  post-alarm re-tracks, i.e. the tail of the night, and stay `kind=night`.
- **The hypnogram is flattened to exclusive stages.** Sleep as Android draws REM
  on top of light (and sometimes deep) and awake on top of either, where Garmin
  and WHOOP report stages that add up to the time in bed. With a precedence
  (awake beats rem beats deep beats light) each moment counts once, so
  `sleep_stage` and the `*_s` fields line up across sources.
- **Comment hashtags are `tags`, always.** They are a vocabulary you chose
  (`#alcohol`, `#sport`, `#late meal`), and the Sleep dashboard filters on them
  (`"tags" =~ /#alcohol/`). The comment's free text is `note`, written only with
  `TRIBUTARY_SLEEP_AS_ANDROID_NOTES`.
- **Times are floored to the second.** The export stamps milliseconds; Tributary
  writes whole seconds. A session's wake, a segment's start and a sample's time
  land on the second they fall in (a sub-second loss no dashboard sees), and two
  samples in the same second keep the later one, as InfluxDB's own write did.

## Privacy

The export holds your comments, tags, the session ids and the zone. Only the
records' numbers, times, the zone's name, the hypnogram's fixed words
(deep/light/rem/awake) and the comment's hashtags reach InfluxDB, all under
`source=sleep_as_android`. A comment's free text is written only when you ask
for it (`TRIBUTARY_SLEEP_AS_ANDROID_NOTES`); nothing identifies the account.

## Ported from

A one-off import script (`sleep_as_android_import.py`), run by hand under a
port-forward over one 2023 backup, writing the same three series. The parsing
(records, events, the nap rule, the stage precedence, the `LenAdjust` rule) and
the mapping are the same, and this connector writes exactly its series (tags,
fields, types and times), so importing the export again rewrites the history in
place and never duplicates it ([docs/porting.md](../porting.md)). Left out, and
why:

- the `--replace` flag: an import always rebuilds the connector's own series
  from the backup, which is the whole history;
- anything the script did not write: the actigraphy columns (named with each
  night's clock times) and the non-vital events (movement, `BROKEN`, ...).
