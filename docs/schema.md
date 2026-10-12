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
  it; nothing ever merges two sources into one series.
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
  with ` / `.
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

## Adding a measurement

Declare it once (`app/schema.py` if other tools may write it too, the
connector otherwise), add its row here, and give every field its final
type from the first write. Check what already exists in the database
(`SHOW FIELD KEYS FROM <measurement>` in InfluxQL) before choosing a type.
