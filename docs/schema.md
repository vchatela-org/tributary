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
  it; nothing ever merges two sources into one series. (One exception,
  `body_thresholds`: it rates a person, not a tracker's reading.)
- **One writer per source.** A source with an API and a richer export goes
  entirely through the same connector (both fill the same series and must
  be merged in one place). That's also what lets a connector rebuild its
  own source safely: no other tool writes `source=<it>`.
- **A `user` tag** naming the person (`TRIBUTARY_INFLUXDB_USER`, or set per
  account by a connector that syncs several people). Left out when not
  configured.
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
  with ` / `. A run only sends what changed, so the time of a point must
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
  (`title`). No message, search, URL, address, email, IP or free-form
  note, ever. A test fails on any other text field.
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

Body composition: weigh-ins of smart scales. No *local time*.

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `body_composition` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `body_water_pct` (float), `muscle_pct` (float), `bone_pct` (float), `bmr_kcal` (float), `visceral_fat` (float), `subcutaneous_fat_pct` (float), `protein_pct` (float), `body_age` (float), `lean_body_mass_kg` (float), `fat_free_weight_kg` (float), `heart_rate_bpm` (float), `cardiac_index` (float), `body_shape` (float) | Tributary: `renpho`. Other tools (a scale's history import...) under their own `source`, every field a float |
| `body_levels` | `source`, `user`, `scale`, `user_id` | `weight_kg` (float), `bmi` (float), `body_fat_pct` (float), `subcutaneous_fat_pct` (float), `visceral_fat` (float), `lean_body_mass_kg` (float), `muscle_pct` (float), `bone_pct` (float), `body_age` (float), `bmr_kcal` (float), `body_water_pct` (float), `protein_pct` (float) | Tributary: `renpho` |
| `body_thresholds` | `user` | `weight_kg_t1` (float), `weight_kg_t2` (float), `weight_kg_t3` (float), `weight_kg_min` (float), `weight_kg_max` (float), `bmi_t1` (float), `bmi_t2` (float), `bmi_t3` (float), `bmi_min` (float), `bmi_max` (float), `body_fat_pct_t1` (float), `body_fat_pct_t2` (float), `body_fat_pct_t3` (float), `body_fat_pct_t4` (float), `body_fat_pct_min` (float), `body_fat_pct_max` (float), `subcutaneous_fat_pct_t1` (float), `subcutaneous_fat_pct_t2` (float), `subcutaneous_fat_pct_min` (float), `subcutaneous_fat_pct_max` (float), `visceral_fat_t1` (float), `visceral_fat_t2` (float), `visceral_fat_t3` (float), `visceral_fat_min` (float), `visceral_fat_max` (float), `lean_body_mass_kg_t1` (float), `lean_body_mass_kg_t2` (float), `lean_body_mass_kg_min` (float), `lean_body_mass_kg_max` (float), `muscle_pct_t1` (float), `muscle_pct_t2` (float), `muscle_pct_min` (float), `muscle_pct_max` (float), `bone_pct_t1` (float), `bone_pct_t2` (float), `bone_pct_min` (float), `bone_pct_max` (float), `body_age_t1` (float), `body_age_min` (float), `body_age_max` (float), `bmr_kcal_t1` (float), `bmr_kcal_min` (float), `bmr_kcal_max` (float), `body_water_pct_t1` (float), `body_water_pct_t2` (float), `body_water_pct_min` (float), `body_water_pct_max` (float), `protein_pct_t1` (float), `protein_pct_t2` (float), `protein_pct_min` (float), `protein_pct_max` (float) | Tributary: `renpho` |

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
  `user` only, stamped when written: read the latest
  (`ORDER BY time DESC LIMIT 1`, no time filter).

## Adding a measurement

Declare it once (`app/schema.py` if other tools may write it too, the
connector otherwise), add its row here, and give every field its final
type from the first write. Check what already exists in the database
(`SHOW FIELD KEYS FROM <measurement>` in InfluxQL) before choosing a type.
