# The shared schema

Tributary writes into InfluxDB 1.x (InfluxQL) following a contract that
other tools can follow too, so that one Grafana sees every source of a
domain side by side and can relate one domain to another. This page is
that contract. The tables below are checked by the test suite against
what the connectors declare (`app/schema.py`, each connector's
`measurements`), so they can't drift.

## Rules

- **One database per domain:** `MediaStats` (reading, listening),
  `BodyStats` (weight, body composition, medication, food, workouts, daily
  activity), `SleepStats` (sleep, recovery, the morning journal),
  `TodoistStats`, `DevStats` (commits, CI), `ShoppingStats`, `DigitalStats`... A
  domain's database holds every tracker of that domain.
- **A `source` tag on every point**, naming the tracker
  (`audiobookshelf`, `kindle`, `renpho`...). Dashboards filter and colour by
  it; nothing ever merges two sources into one series. (Exceptions:
  `body_thresholds`, which rates a person, not a tracker's reading; and
  `TodoistStats`, the layout of the job its connector replaced, Todoist
  being that database's only writer.)
- **One writer per source.** A source with an API and a richer export goes
  entirely through the same connector (both fill the same series and must
  be merged in one place). That's also what lets a connector rebuild its
  own source safely: no other tool writes `source=<it>`.
- **A `user` tag** naming the person (`TRIBUTARY_INFLUXDB_USER`, or set per
  account by a connector that syncs several people). Left out when not
  configured, and on `TodoistStats` (the job it was ported from wrote
  none).
- **Field types are fixed per measurement.** InfluxDB 1.x rejects a write
  whose field type differs from what the shard already holds, so a field
  is always written with the type in the tables below, by every tool:
  `int` (`12i` in line protocol), `float`, `str`, `bool` (`true`/`false`).
  A new field is a new name, never a new type for an old one.
- **Local time:** InfluxQL can't shift a time zone, so measurements marked
  *local time* carry `local_hour` (0-23) and `local_weekday` (1 = Monday …
  7 = Sunday) as int fields, in `TRIBUTARY_TZ`, plus a `time_precision`
  tag: `second`, or `day` for a date without a time of day, which then
  has no local hour.
- **Same key, same point.** A point is keyed by measurement + tags + time
  (to the second), and writing the same key overwrites it. Points that
  share a key within a run are merged first: numbers summed, texts joined
  with ` / ` (on `TodoistStats`, the later point's fields win instead, as
  the job it was ported from wrote them). A run only sends what changed, so the time of a point must
  not move between runs (a session's start, not its last update), and two
  records sharing a key across runs (same device, same second) would keep
  only the last one's values: choose tags and times that make that
  practically impossible.
- **Append only.** Nothing is dropped by default: a session a source later
  forgets stays in InfluxDB. A connector may opt into rebuilding its own
  source (`DROP SERIES FROM <measurement> WHERE "source" = '<it>'`, then
  rewrite) when a folder of exports is its source of truth.
- **Privacy.** InfluxDB 1.x often runs without authentication. Numbers and
  low-cardinality tags only; the only text fields are book or show titles
  (`title`), Todoist's task text (`content`, `labels`) when you ask for it
  (`TRIBUTARY_TODOIST_TASK_TEXT`), a medication dose's note when you ask for
  it (`TRIBUTARY_MEDISAFE_NOTES`), the words a medication app itself uses
  for a dose (its status, its unit, the medication's strength), and an
  exercise app's own name for an exercise (`activity`): bounded
  vocabularies. A journal's questions are a tag (WHOOP's wording, and the
  ones the member wrote), never its notes. No message, search, URL,
  address, email, IP or free-form note otherwise. A test fails on any other
  text field.
- **Databases** are created by Tributary when missing, `WITH SHARD DURATION
  52w` (the default weekly shards make hundreds of tiny shards for a few
  years of history). An existing database is never altered.

## Measurements

### `MediaStats`

Reading and listening. *Local time* on both measurements.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `reading_session` | `source`, `user`, `kind` (`audiobook`, `podcast`, `ebook`), `device`, `time_precision` | `minutes` (float), `sessions` (int), `title` (str), `local_hour` (int), `local_weekday` (int), `pages` (int) | Tributary: `audiobookshelf`. Takeback: `kindle`, `audible` (with `pages`, and its own `platform` and `via` tags) |
| `book_finished` | `source`, `user`, `kind`, `time_precision` | `count` (int), `title` (str), `local_hour` (int), `local_weekday` (int) | Tributary: `audiobookshelf`. Takeback: `kindle` |

- `reading_session`: one point per session, at its start. `minutes` is the
  time actually read or listened (not the distance between two positions,
  which a seek inflates); `sessions` is 1 per session (more when sessions
  share a key). `title` is the book, or the show for a podcast episode.
  `device` is the name a reader knows the device by.
- `book_finished`: one point per book finished, at the time it was
  finished, `count` = 1.

### `BodyStats`

Body composition: weigh-ins of smart scales, the doses of a medication
reminder app, meals and workouts, and a device's daily activity. No *local
time*.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `body_composition` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `body_water_pct` (float), `muscle_pct` (float), `bone_pct` (float), `bmr_kcal` (float), `visceral_fat` (float), `subcutaneous_fat_pct` (float), `protein_pct` (float), `body_age` (float), `lean_body_mass_kg` (float), `fat_free_weight_kg` (float), `heart_rate_bpm` (float), `cardiac_index` (float), `body_shape` (float) | Tributary: `renpho`. Other tools (a scale's history import...) under their own `source`, every field a float |
| `body_levels` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `subcutaneous_fat_pct` (float), `visceral_fat` (float), `lean_body_mass_kg` (float), `muscle_pct` (float), `bone_pct` (float), `body_age` (float), `bmr_kcal` (float), `body_water_pct` (float), `protein_pct` (float) | Tributary: `renpho` |
| `body_thresholds` | `user` | `weight_kg_t1` (float), `weight_kg_t2` (float), `weight_kg_t3` (float), `weight_kg_min` (float), `weight_kg_max` (float), `bmi_t1` (float), `bmi_t2` (float), `bmi_t3` (float), `bmi_min` (float), `bmi_max` (float), `body_fat_pct_t1` (float), `body_fat_pct_t2` (float), `body_fat_pct_t3` (float), `body_fat_pct_t4` (float), `body_fat_pct_min` (float), `body_fat_pct_max` (float), `subcutaneous_fat_pct_t1` (float), `subcutaneous_fat_pct_t2` (float), `subcutaneous_fat_pct_min` (float), `subcutaneous_fat_pct_max` (float), `visceral_fat_t1` (float), `visceral_fat_t2` (float), `visceral_fat_t3` (float), `visceral_fat_min` (float), `visceral_fat_max` (float), `lean_body_mass_kg_t1` (float), `lean_body_mass_kg_t2` (float), `lean_body_mass_kg_min` (float), `lean_body_mass_kg_max` (float), `muscle_pct_t1` (float), `muscle_pct_t2` (float), `muscle_pct_min` (float), `muscle_pct_max` (float), `bone_pct_t1` (float), `bone_pct_t2` (float), `bone_pct_min` (float), `bone_pct_max` (float), `body_age_t1` (float), `body_age_min` (float), `body_age_max` (float), `bmr_kcal_t1` (float), `bmr_kcal_min` (float), `bmr_kcal_max` (float), `body_water_pct_t1` (float), `body_water_pct_t2` (float), `body_water_pct_min` (float), `body_water_pct_max` (float), `protein_pct_t1` (float), `protein_pct_t2` (float), `protein_pct_min` (float), `protein_pct_max` (float) | Tributary: `renpho` |
| `medication` | `source`, `user`, `med`, `status` | `taken` (int), `app_status` (str), `dose` (float), `unit` (str), `delay_min` (int), `note` (str), `strength` (str) | Tributary: `mytherapy`, `medisafe` |
| `nutrition` | `source`, `user`, `meal` | `energy_kcal` (float), `fat_g` (float), `carbs_g` (float), `protein_g` (float), `saturated_fat_g` (float), `polyunsaturated_fat_g` (float), `monounsaturated_fat_g` (float), `trans_fat_g` (float), `fiber_g` (float), `sugar_g` (float), `cholesterol_mg` (float), `sodium_mg` (float), `potassium_mg` (float) | Tributary: `myfitnesspal` |
| `exercise` | `source`, `user`, `category`, `sport` | `activity` (str), `energy_kcal` (float), `duration_s` (int), `strain` (float), `avg_hr` (float), `max_hr` (float), `distance_m` (float), `altitude_gain_m` (float), `altitude_change_m` (float), `percent_recorded` (float), `zone0_s` (int), `zone1_s` (int), `zone2_s` (int), `zone3_s` (int), `zone4_s` (int), `zone5_s` (int), `zone1_pct` (float), `zone2_pct` (float), `zone3_pct` (float), `zone4_pct` (float), `zone5_pct` (float), `gps_enabled` (int) | Tributary: `myfitnesspal` (the first three fields), `whoop` |
| `daily_activity` | `source`, `user` | `strain` (float), `energy_kcal` (float), `avg_hr` (float), `max_hr` (float), `steps` (int), `duration_s` (int) | Tributary: `whoop` |

- `body_composition`: one point per weigh-in, at its time, with a field
  for each metric the scale measured (none for one it didn't: never a
  zero standing for "not measured"). Names follow the Renpho API rather
  than its app: `bone_pct` is the bone *mass* in kg, `lean_body_mass_kg`
  the app's "Muscle Mass", `muscle_pct` its "Skeletal Muscle Percentage",
  `body_age` its "Metabolic Age". `scale` is the scale's model (Renpho
  reports it for older scales only), `user_id` the Renpho account's
  numeric id.
- `body_levels`: same tags and time as the weigh-in it rates, a field per
  rated metric: the level its value falls in, as the Renpho app rates it
  (0 = the lowest: Below Avg., Average, Above Avg., Over...). Only for the
  people given boundaries.
- `body_thresholds`: those boundaries, per person: `<metric>_t1`...`_tN`
  between the levels, `<metric>_min` / `_max` the scale of a gauge. Tagged
  `user` only, stamped when written (when they change, and at least
  weekly): read the latest.
- `medication`: one point per scheduled dose, at the time it was scheduled
  -- local for `mytherapy`, so a 12:00 dose stays 12:00 across the DST
  changes; UTC for `medisafe`, whose export gives UTC times (its schedules
  move by an hour on the DST dates). `med` is the medication's name (from
  the connector's settings for `mytherapy`, whose app holds only a vague
  alias; the app's own name for `medisafe`, renamed or left out per
  `TRIBUTARY_MEDISAFE_NAMES`), `status` the contract word for what happened
  (`taken`, `missed`, `skipped`, and `unlogged` for a Medisafe reminder
  nobody answered or a Medisafe dose the app reports as deleted), `taken` 1
  when the dose was taken and 0 otherwise
  (`app_status` keeps the app's own word). `dose` and `unit` are there when
  the export carries them ("pill(s)" becomes "pills"); `delay_min` only for
  a taken dose: the scheduled time to the moment it was answered, negative
  when early. Two doses of one medication and status scheduled in the same
  second are a second apart, as the job that first wrote them wrote them.
  `strength` ("10 mg") and `note` (free text, with
  `TRIBUTARY_MEDISAFE_NOTES` only) are Medisafe's: MyTherapy's exports
  carry a note column, but no dose is ever written with one.
- `nutrition`: one point per logged meal, at noon *local* on the day (the
  export has dates only, and a day must land on the right date whether a
  query groups in UTC or in Paris). `meal` is `breakfast`, `lunch`, `dinner`
  or `snacks`. The macros (`energy_kcal`, `fat_g`, `carbs_g`, `protein_g`)
  are always written; a secondary nutrient is there only when the entry
  reported one -- the export writes a 0 for "not reported", so a 0 would
  understate it. Sum per `time(1d)` for daily totals.
- `exercise`: one point per workout or a linked device's daily calorie
  adjustment. `category` is `run`, `bike`, `strength`, `aerobic`, `walk`,
  `hike`, `swim`, `climb`, `paddle`, `elliptical`, `other` -- and
  `adjustment` for a device's calorie adjustment, which is not a workout:
  filter it out with `"category" != 'adjustment'`. `activity` is the app's
  own name for the exercise.
  - `myfitnesspal`: at noon *local* plus the entry's index within the day
    (so several workouts on one date stay separate points); `duration_s` is
    absent on an adjustment (the export's fixed "1 minute" is a
    placeholder).
  - `whoop`: at the workout's start, `category` mapped from WHOOP's sport
    name (`running` → `run`...), and `sport` = that name kept as written (a
    tag, so a panel can group by it: InfluxQL groups by tags, not fields).
    `strain` is WHOOP's (0-21), `zone0_s` ...
    `zone5_s` the seconds spent in each heart-rate zone (zone 0 below zone
    1), `percent_recorded` the share of the workout with heart-rate data.
    From WHOOP's export only: `zone1_pct` ... `zone5_pct` and
    `gps_enabled` (0/1).
- `daily_activity`: one point per device day. WHOOP's is its physiological
  cycle, from one sleep onset to the next, stamped at its start (so usually
  the evening before the day it is named after). `energy_kcal` is the
  energy burned (WHOOP's kilojoules / 4.184), `steps` WHOOP's `step_count`,
  `duration_s` absent while the cycle is still open (today's).

### `SleepStats`

Sleep: one point per sleep, from any tracker, and the morning journal that
goes with it. No *local time*: a sleep carries its own local minutes
(`bedtime_min`, `wake_min`) in the offset it was recorded in.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `sleep_session` | `source`, `user`, `kind` (`night`, `nap`) | `start` (int), `in_bed_s` (int), `asleep_s` (int), `awake_s` (int), `light_s` (int), `deep_s` (int), `rem_s` (int), `cycles` (int), `awakenings` (int), `tz_offset_min` (int), `bedtime_min` (int), `wake_min` (int), `score` (float), `respiration_rate` (float), `resting_hr` (float), `hrv_rmssd_ms` (float), `avg_spo2_pct` (float), `efficiency_pct` (float), `consistency_pct` (float), `recovery_score` (float), `skin_temp_c` (float), `calibrating` (int), `no_data_s` (int), `need_baseline_s` (int), `need_debt_s` (int), `need_strain_s` (int), `need_nap_s` (int), `sleep_need_s` (int), `sleep_debt_s` (int), `avg_hr` (float), `min_hr` (float), `max_hr` (float), `deep_pct` (float), `rating` (float), `sdnn_ms` (float), `noise_level` (float), `snore_s` (float), `tz` (str), `tags` (str), `note` (str), `manual` (bool) | Tributary: `whoop`, `sleep_as_android` |
| `sleep_stage` | `source`, `user` | `level` (int), `stage` (str), `duration_s` (int) | Tributary: `sleep_as_android` (its export) |
| `sleep_vitals` | `source`, `user` | `hr_bpm` (float), `spo2_pct` (float), `sdnn_ms` (float) | Tributary: `sleep_as_android` (its export) |
| `journal` | `source`, `user`, `question` | `answered_yes` (int), `count` (int), `recovery_score` (float), `hrv_rmssd_ms` (float), `asleep_s` (int), `strain` (float) | Tributary: `whoop` (from its export) |

- `sleep_session`: one point per sleep, at its **end** (the wake), so a
  night lands on the morning it ended, as Garmin's `SleepSummary` does.
  `start` is its start (epoch seconds); `in_bed_s` its length as the source
  reports it; `awake_s`, `light_s`, `deep_s`, `rem_s` exclusive stages
  (`deep_s` is WHOOP's slow-wave sleep); `asleep_s` the total sleep as the
  source reports it (WHOOP: light + deep + REM; its export's "Asleep
  duration" for history only the export has). `bedtime_min` is the local
  start as signed minutes from midnight (23:30 = -30, 00:45 = 45),
  `wake_min` the local end in minutes after midnight, both in
  `tz_offset_min`. `score` is the source's own sleep score, 0-100 (WHOOP's
  sleep performance), never comparable across sources.
  - WHOOP's own: `efficiency_pct`, `consistency_pct`; `no_data_s`; the sleep
    it says was needed (`need_baseline_s` plus `need_debt_s`,
    `need_strain_s`, `need_nap_s`, which may be negative), and, from its
    export, `sleep_need_s` and `sleep_debt_s`. The recovery WHOOP scores
    from the night lands on that night's point: `recovery_score` (0-100),
    `resting_hr`, `hrv_rmssd_ms`, `avg_spo2_pct`, `skin_temp_c`, and
    `calibrating` (1 while WHOOP is still learning the member's baselines).
  - Sleep as Android's own (its export): `avg_hr`, `min_hr`, `max_hr` over
    the session, `deep_pct` (its own deep-sleep percentage), `rating` (the
    night's rating), `sdnn_ms` (an SDNN HRV, a different statistic from
    `hrv_rmssd_ms`), `snore_s` and `noise_level`; `tz` (the zone's IANA
    name); `manual` (true for a session entered by hand, its cycles == -1).
    `tags` (the comment's hashtags, e.g. `#alcohol #sport`) and `note` (its
    free text, written only with `TRIBUTARY_SLEEP_AS_ANDROID_NOTES`) are
    text: the hashtags are a vocabulary the user chose and are always
    written, the note is not.
- `sleep_stage`: Sleep as Android's hypnogram, one point per segment at its
  start. `level` is Garmin's `SleepStageLevel` scale (0 deep, 1 light,
  2 rem, 3 awake) so hypnograms line up across sources, `stage` repeats it
  as a word, `duration_s` is the segment. The app draws REM on top of light
  and awake on top of either, so segments are flattened with a precedence
  (awake, rem, deep, light): each moment counts once.
- `sleep_vitals`: Sleep as Android's samples during a session, at their
  time: whichever the app recorded -- `hr_bpm`, `spo2_pct`, `sdnn_ms`.
  Both of these carry milliseconds in the export and whole seconds here
  (Tributary's precision); two samples in one second keep the later one.
- `journal`: one point per answer of WHOOP's journal, at the start of the
  cycle it was answered for (the cycle that starts with the night it is
  read against). `question` is the question as WHOOP words it, the member's
  own custom questions included; `answered_yes` is 1 or 0, `count` 1. The
  answer also carries the recovery of the cycle it is keyed to
  (`recovery_score`, `hrv_rmssd_ms`, `asleep_s`, `strain`), so a panel can compare recovery, sleep and strain on days
  a question was answered yes against no without joining two measurements. The
  notes typed with an answer are never written.

### `TodoistStats`

Tasks done in Todoist. No *local time*. This layout is the one of the sync
job the Todoist connector replaced, kept as it was so that its history and
dashboards carry on: no `source` and no `user` tag, a `task_id` tag (so a
completion rewritten overwrites itself), project names as tags.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `todoist_task_completed` | `task_id`, `project`, `section`, `priority` (`p1`...`p4`), `recurring`, `subtask` (no `source`, no `user`) | `count` (int), `lead_time_hours` (float), `postponed_count` (int), `duration_minutes` (int), `late_days` (int), `content` (str), `labels` (str) | Tributary: `todoist` |
| `todoist_completion` | `task_id`, `project` (no `source`, no `user`) | `count` (int), `content` (str) | Tributary: `todoist` |
| `todoist_karma` | none (no `source`, no `user`) | `karma` (float) | Tributary: `todoist` |
| `todoist_stats` | none (no `source`, no `user`) | `karma` (float), `completed_count` (int), `daily_goal` (int), `weekly_goal` (int), `current_daily_streak` (int), `max_daily_streak` (int), `current_weekly_streak` (int), `max_weekly_streak` (int) | Tributary: `todoist` |

- `todoist_task_completed`: one point per completed task, at its
  completion, `count` = 1. Recurring tasks never appear (completing one
  only moves its due date). `lead_time_hours` from creation to completion,
  `late_days` the completion's local date minus the due date (negative:
  early; one-off tasks only). `recurring` and `subtask` are `true` /
  `false`.
- `todoist_completion`: one point per completion, recurring tasks included:
  count completions here.
- `todoist_karma`: the daily karma average, at local midnight.
- `todoist_stats`: one point per day, at local midnight, the day's last
  sync: karma, lifetime completions, goals and streaks.
- `content` (the task's name) and `labels` (comma-joined) only with
  `TRIBUTARY_TODOIST_TASK_TEXT`, whole (other texts are cut to 250
  characters).

### `DevStats`

Software work on GitHub: who wrote the commits, and the minutes GitHub's hosted
runners spent on the workflows.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `github_commit` | `source`, `user`, `owner`, `repository`, `author` (`self`, `agent`, `bot`, `other`), `time_precision` | `count` (int), `additions` (int), `deletions` (int), `local_hour` (int), `local_weekday` (int) | Tributary: `github` |
| `github_actions_usage` | `source`, `user`, `owner`, `repository`, `sku` | `minutes` (float), `gross_usd` (float), `discount_usd` (float), `net_usd` (float), `private` (bool) | Tributary: `github` |
| `github_actions_month` | `source`, `user`, `owner` | `minutes` (float), `private_minutes` (float), `counted_minutes` (float), `gross_usd` (float), `net_usd` (float) | Tributary: `github` |

- `github_commit`: one point per commit of a repository's default branch
  (merge commits left out), at its author's time, *local time*. `author` is
  `agent` when an agent is among its authors (a `Co-authored-by` trailer
  included), else `bot` when a bot is, else `self` (the token's account) or
  `other`. `owner` is the organisation or user the repository belongs to.
- `github_actions_usage`: one point per line of GitHub's billing report with
  minutes, at its day (UTC midnight): a repository's minutes on one SKU
  (`Actions Linux`, `Actions Windows`...), with what they cost before and
  after the discount (the included minutes, and public repositories, which
  are free). `private` is the repository's visibility when read: a field, so
  a change overwrites the day rather than doubling it.
- `github_actions_month`: one point per owner and month, at its first day
  (UTC): InfluxQL has no calendar month. `counted_minutes` is what counts
  against a plan's included minutes: the private repositories' standard
  runners, Windows twice and macOS ten times.

## Adding a measurement

Declare it once (`app/schema.py` if other tools may write it too, the
connector otherwise), add its row here, and give every field its final
type from the first write. Check what already exists in the database
(`SHOW FIELD KEYS FROM <measurement>` in InfluxQL) before choosing a type.
