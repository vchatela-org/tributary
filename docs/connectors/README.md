# Connectors

## The connectors

Grouped by domain, the way the UI shows them. `Input` says how the data arrives
and, for an API, how often it runs by default (change it in the UI).

### Body & health

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**Renpho**](renpho.md) | `renpho` | API, daily, one account per person | `BodyStats`: `body_composition`, `body_levels`, `body_thresholds` |

### Medication

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**MyTherapy**](mytherapy.md) | `mytherapy` | export file, imported in the UI | `BodyStats`: `medication` |
| [**Medisafe**](medisafe.md) | `medisafe` | export file, imported in the UI | `BodyStats`: `medication` |

### Nutrition & fitness

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**MyFitnessPal**](myfitnesspal.md) | `myfitnesspal` | export file, imported in the UI | `BodyStats`: `body_composition`, `nutrition`, `exercise` |

### Sleep

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**Whoop**](whoop.md), built on kryoseu's [Whoops](https://github.com/kryoseu/whoops) | `whoop` | API (OAuth, connected on its page), daily; and its export file, imported in the UI | `SleepStats`: `sleep_session`, `journal`; `BodyStats`: `daily_activity`, `exercise` |
| [**Sleep as Android**](sleep_as_android.md) | `sleep_as_android` | backup file, imported in the UI | `SleepStats`: `sleep_session`, `sleep_stage`, `sleep_vitals` |

garmin-grafana, a [compatible tool](../compatible-tools.md), covers Garmin's
sleep.

### Reading & media

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**Audiobookshelf**](audiobookshelf.md) | `audiobookshelf` | API, daily | `MediaStats`: `reading_session`, `book_finished` |

### Productivity

| Connector | Source | Input | Writes |
|---|---|---|---|
| [**Todoist**](todoist.md) | `todoist` | API, daily | `TodoistStats`: `todoist_task_completed`, `todoist_completion`, `todoist_karma`, `todoist_stats` |

A test compares this table with the code: a connector that is registered but
missing here, or filed under another `category`, fails the suite.

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
- `category`: one of `app/categories.py`'s `CATEGORIES`, the section the UI
  shows it under and its row's category in the README. A test fails when it is
  missing or unknown.
- `settings_class`: a `ConnectorSettings` subclass with its own prefix,
  `TRIBUTARY_<KEY>_`. Secrets are `SecretStr`: never logged, stored or
  returned. `watch_dir` comes with it, and so do the two schedule values:
  `schedule` (a cron expression) and the deprecated `interval_minutes`.
- `default_schedule`: the cron it runs on when neither the UI nor the
  environment says otherwise, in `TRIBUTARY_TZ` (see
  [scheduling](../scheduling.md)). `None` for an upload-only source, which has
  no schedule at all. A test enforces one or the other.
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
- `import_fingerprint()`: whatever *else* of this connector's settings decides
  what an import writes (a names map, a cut-off date), as JSON-able values. It
  is hashed into the record a **check** keeps, so that changing one invalidates
  the checked file instead of letting "Import for real" apply a diff the user
  never saw (`app/runner.py`). Empty when only the file decides.
- `import_file` returns `FileImport(points, spans)` when an export covers a
  date range the user picks (MyFitnessPal): `spans` is `{measurement name:
  Span(first, last)}`, the days (`TRIBUTARY_TZ`, both included) the file
  covers of each series. Only those are replaced, for the connector's source
  and `TRIBUTARY_INFLUXDB_USER` (`DELETE FROM ... WHERE "source" = '<key>' AND
  "user" = '<user>' AND time >= ... AND time < ...`; without a user the
  import is refused), so an export of the last month never empties the years
  before it; a measurement without a span is left as it is. Bare points mean the whole history (the whole series is
  replaced). Such a connector takes uploads only (its `watch_dir` is `None`):
  a folder of range exports is not a whole history.
- `upload_label(filename)`: how a run names the uploaded file (stored, shown,
  logged). The name by default; a connector masks what it holds that is
  personal (Medisafe: the account number of `Doses_<account>_000.csv`). The
  masked name is also what a checked upload keeps, never the raw one.
- A source that grants access in a browser (OAuth 2.0, authorization code)
  sets `uses_oauth = True` and implements `app/oauth.py`'s hooks:
  `oauth_problem()` (what its settings lack), `oauth_authorize_url(state)`,
  `oauth_exchange(code)`, `oauth_refresh(refresh_token)` (raising
  `GrantRevoked` on `invalid_grant`) and `oauth_revoke(access_token)`. The
  core does the rest: Connect, the callback at `/connectors/<key>/callback`
  (and its pasted-address fallback), Disconnect, the single-use `state`, the
  tokens sealed with `TRIBUTARY_SECRET_KEY` (`app/secretbox.py`) and stored
  the moment they arrive, the refresh a minute before expiry or after a 401
  (`oauth.access_token(connector, force_refresh)`), and a lock around it.
  `setup_hint()` tells the overview what is missing (connect it on its
  page) when the variables alone don't. [Whoop](whoop.md) is the example.

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
