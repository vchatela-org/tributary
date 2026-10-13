# The shared schema

Tributary writes into InfluxDB 1.x (InfluxQL) following a contract that
other tools can follow too, so that one Grafana sees every source of a
domain side by side and can relate one domain to another. This page is
that contract. The tables below are checked by the test suite against
what the connectors declare (`app/schema.py`, each connector's
`measurements`), so they can't drift.

## Rules

- **One database per domain:** `MediaStats` (reading, listening),
  `BodyStats` (weight, body composition, medication), `SleepStats`,
  `TodoistStats`, `ShoppingStats`, `DigitalStats`... A
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
  `int` (`12i` in line protocol), `float`, `str`. A new field is a new
  name, never a new type for an old one.
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
  vocabularies. No message, search, URL, address, email, IP or free-form
  note otherwise. A test fails on any other text field.
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

Body composition: weigh-ins of smart scales, and the doses of a medication
reminder app. No *local time*.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `body_composition` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `body_water_pct` (float), `muscle_pct` (float), `bone_pct` (float), `bmr_kcal` (float), `visceral_fat` (float), `subcutaneous_fat_pct` (float), `protein_pct` (float), `body_age` (float), `lean_body_mass_kg` (float), `fat_free_weight_kg` (float), `heart_rate_bpm` (float), `cardiac_index` (float), `body_shape` (float) | Tributary: `renpho`. Other tools (a scale's history import...) under their own `source`, every field a float |
| `body_levels` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `subcutaneous_fat_pct` (float), `visceral_fat` (float), `lean_body_mass_kg` (float), `muscle_pct` (float), `bone_pct` (float), `body_age` (float), `bmr_kcal` (float), `body_water_pct` (float), `protein_pct` (float) | Tributary: `renpho` |
| `body_thresholds` | `user` | `weight_kg_t1` (float), `weight_kg_t2` (float), `weight_kg_t3` (float), `weight_kg_min` (float), `weight_kg_max` (float), `bmi_t1` (float), `bmi_t2` (float), `bmi_t3` (float), `bmi_min` (float), `bmi_max` (float), `body_fat_pct_t1` (float), `body_fat_pct_t2` (float), `body_fat_pct_t3` (float), `body_fat_pct_t4` (float), `body_fat_pct_min` (float), `body_fat_pct_max` (float), `subcutaneous_fat_pct_t1` (float), `subcutaneous_fat_pct_t2` (float), `subcutaneous_fat_pct_min` (float), `subcutaneous_fat_pct_max` (float), `visceral_fat_t1` (float), `visceral_fat_t2` (float), `visceral_fat_t3` (float), `visceral_fat_min` (float), `visceral_fat_max` (float), `lean_body_mass_kg_t1` (float), `lean_body_mass_kg_t2` (float), `lean_body_mass_kg_min` (float), `lean_body_mass_kg_max` (float), `muscle_pct_t1` (float), `muscle_pct_t2` (float), `muscle_pct_min` (float), `muscle_pct_max` (float), `bone_pct_t1` (float), `bone_pct_t2` (float), `bone_pct_min` (float), `bone_pct_max` (float), `body_age_t1` (float), `body_age_min` (float), `body_age_max` (float), `bmr_kcal_t1` (float), `bmr_kcal_min` (float), `bmr_kcal_max` (float), `body_water_pct_t1` (float), `body_water_pct_t2` (float), `body_water_pct_min` (float), `body_water_pct_max` (float), `protein_pct_t1` (float), `protein_pct_t2` (float), `protein_pct_min` (float), `protein_pct_max` (float) | Tributary: `renpho` |
| `medication` | `source`, `user`, `med`, `status` | `taken` (int), `app_status` (str), `dose` (float), `unit` (str), `delay_min` (int), `note` (str), `strength` (str) | Tributary: `mytherapy`, `medisafe` |
| `nutrition` | `source`, `user`, `meal` | `energy_kcal` (float), `fat_g` (float), `carbs_g` (float), `protein_g` (float), `saturated_fat_g` (float), `polyunsaturated_fat_g` (float), `monounsaturated_fat_g` (float), `trans_fat_g` (float), `fiber_g` (float), `sugar_g` (float), `cholesterol_mg` (float), `sodium_mg` (float), `potassium_mg` (float) | Tributary: `myfitnesspal` |
| `exercise` | `source`, `user`, `category` | `activity` (str), `energy_kcal` (float), `duration_s` (int) | Tributary: `myfitnesspal` |

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
  nobody answered), `taken` 1 when the dose was taken and 0 otherwise
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
  adjustment, at noon *local* plus the entry's index within the day (so
  several workouts on one date stay separate points). `category` is `run`,
  `bike`, `strength`, `aerobic`, `walk`, `hike`, `swim`, `climb`, `paddle`,
  `elliptical`, `other` -- and `adjustment` for a device's calorie
  adjustment, which is not a workout: filter it out with
  `"category" != 'adjustment'`. `activity` is the app's own name for the
  exercise; `duration_s` is absent on an adjustment (the export's fixed
  "1 minute" is a placeholder).

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

## Adding a measurement

Declare it once (`app/schema.py` if other tools may write it too, the
connector otherwise), add its row here, and give every field its final
type from the first write. Check what already exists in the database
(`SHOW FIELD KEYS FROM <measurement>` in InfluxQL) before choosing a type.
