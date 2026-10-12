# Connectors

| Connector | Source | Input | Writes |
|---|---|---|---|
| [Audiobookshelf](audiobookshelf.md) | `audiobookshelf` | API, hourly | `MediaStats`: `reading_session`, `book_finished` |
| [Renpho](renpho.md) | `renpho` | API, daily, one account per person | `BodyStats`: `body_composition`, `body_levels`, `body_thresholds` |
| [Todoist](todoist.md) | `todoist` | API, daily | `TodoistStats`: `todoist_task_completed`, `todoist_completion`, `todoist_karma`, `todoist_stats` |
| [MyTherapy](mytherapy.md) | `mytherapy` | export file, uploaded in the UI | `BodyStats`: `medication` |

## Before writing one

A connector is written only for a source no maintained tool covers yet: if
one exists, use it and list it in [compatible tools](../compatible-tools.md)
instead. And one source has one writer: a source with an API and an export
goes entirely through one connector, both inputs filling the same series.

## The contract

One module in `app/connectors/`, registered in `app/connectors/__init__.py`
(see `app/connectors/base.py` for the details):

- `key`: its id, also the `source` tag of every point it writes.
- `label`, `description`, `icon` (a Bootstrap icon) for the UI.
- `settings_class`: a `ConnectorSettings` subclass with its own prefix,
  `TRIBUTARY_<KEY>_`. Secrets are `SecretStr`: never logged, stored or
  returned. `interval_minutes` and `watch_dir` come with it.
- `measurements`: what it writes, each with its database, tags and field
  types (`app/schema.py` for measurements other tools share). Add them to
  [the schema](../schema.md); a test compares both.
- `required_env`: the variables the UI asks for when it isn't configured.
- An API input: `api_configured()` and `sync(cursor) -> SyncResult(points,
  cursor, summary, errors)`. The cursor is the connector's own JSON-able
  state (keep it small: timestamps, not payloads), `None` on a first or full
  run, saved only after a run that wrote everything. A source made of
  independent parts (one account per person) lists the parts that failed in
  `errors` rather than raise: the others are written, the cursor saved
  (keep the failed parts' cursor as it was), and the run shows as failed.
- A file input: `file_types` (e.g. `(".zip",)`) and
  `import_file(path) -> points`. It serves both the upload form and, when
  `TRIBUTARY_<KEY>_WATCH_DIR` is set, the files of a watched folder (new or
  changed ones at each run).
- `rebuild_own_source = True` when a file holds the connector's whole history
  (an export): importing it drops the connector's own series
  (`DROP SERIES FROM <measurement> WHERE "source" = '<key>'`) and rewrites it
  from the file, rather than adding to it or overwriting point by point -- a
  record the file no longer has is one the source no longer knows. Same for a
  folder that is the source of truth, where a change in it rebuilds the
  series. Safe only because nothing else writes that source, and a connector
  raises rather than let a rebuild write nothing back.

Points are `Point(measurement, time, tags, fields)`, time in UTC. The core
adds `source` (unless the measurement is declared `tagged_source=False`),
`user` (unless the point sets its own: a value for a connector syncing
several people, `None` for none at all; or the measurement is declared
`tagged_user=False`, e.g. a port of an old job that wrote no `user` tag),
and for measurements with `local_time` the `time_precision` tag and
`local_hour` / `local_weekday` fields. It refuses an undeclared tag or
field, cuts texts to 250 characters (`text_max`), and merges points sharing
a key within a run by summing their numbers: a source whose records may
share a key and replace each other declares `last_wins=True` (the later
one's fields win).

Porting a job that already writes into InfluxDB? Follow
[porting a feed](../porting.md): same series, proved equal before the
switch.

Tests: a fake of the source's API or a sample export (synthetic data only:
the privacy check fails on real-looking identifiers), the mapping, paging
or cursor behaviour, and what must never be kept.

To read InfluxDB (e.g. to skip what another source already measured),
use `app.influxdb.query`.
