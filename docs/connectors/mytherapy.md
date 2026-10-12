# MyTherapy

[MyTherapy](https://www.mytherapyapp.com/) is a medication reminder: it
knows every dose it scheduled and whether it was taken. It has no API, so
Tributary reads the exports you save to a folder and writes the doses into
`BodyStats..medication` (see [the schema](../schema.md)).

The folder is the **source of truth**: each run reads every export it holds,
in order, and rebuilds `source=mytherapy` from them. A run with no new export
writes nothing; a dose you answered after an export, or deleted in the app,
follows the newest export.

## Setup

1. In MyTherapy, export your data (the app saves `MyTherapy Archive.zip`) and
   save it to a folder Tributary can read -- a share on a NAS, for instance,
   mounted read-only into the container. Keep the earlier exports: they are
   read together.
2. Give each of your medications a name, as a JSON object mapping the alias
   the app shows to the name the `med` tag should hold:

   | Variable | |
   |---|---|
   | `TRIBUTARY_MYTHERAPY_NAMES` | `{"<alias>": "<name>"}`, the app's alias to the name written in the `med` tag. Required. |
   | `TRIBUTARY_MYTHERAPY_WATCH_DIR` | The folder holding the exports (`.zip`, `.csv`). Read-only. |
   | `TRIBUTARY_MYTHERAPY_START_DATE` | Doses scheduled before this date (`YYYY-MM-DD`) are not imported. Optional: see below. |
   | `TRIBUTARY_MYTHERAPY_INTERVAL_MINUTES` | Minutes between two runs (default 1440, once a day). |

   The app itself keeps a deliberately vague alias per medication (that is
   how it is meant to be used): `TRIBUTARY_MYTHERAPY_NAMES` is where the real
   name enters, and it belongs to your environment, never to this repository.
   An alias the mapping doesn't know fails the run and lists the aliases to
   add: the dose is not imported under a guessed name.
3. In Tributary, *Dry run* shows the line protocol of what the next run would
   write; *Run now* reads the folder and writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `medication` | scheduled dose | the time it was scheduled, local | `med`, `status` | `taken` (1 or 0), `app_status`, `dose`, `unit`, `delay_min` |

- `status` is `taken`, `missed` or `skipped`; `app_status` keeps the app's own
  word (`confirmed`, `auto skipped`, `rejected`). `taken` is 1 for `taken` and
  0 otherwise, so a dashboard can `sum()` doses taken and `mean()` the share.
- `dose` and `unit` are there when the export carries them ("pill(s)" becomes
  "pills"); `delay_min` is there for a taken dose only: the scheduled time to
  the moment it was answered, negative when it was answered early.
- Times are **local** (`TRIBUTARY_TZ`): a dose scheduled at 12:00 stays 12:00
  across the DST changes, as the app shows it. Do not compare them with a
  source that stores UTC (Medisafe's doses, for instance).
- Two doses of one medication and status scheduled in the same second are
  written a second apart, as the job that first wrote these series wrote them.
- The exports are applied oldest first (by their last scheduled dose) and each
  replaces what the older ones said over its own date range: one point per
  dose, the newest answer winning.

## How it syncs

- A run reads the whole folder every time: the exports are small, and a dose
  you answer late, or delete in the app, has to follow the newest export.
  *Full resync* reads it again, which changes nothing here.
- A change in the folder (a file added, replaced or removed) drops
  `source=mytherapy` from `medication` (`DROP SERIES`, every `user` tag of
  it: one writer per source) and rewrites the series from every export. Safe:
  nothing else writes that source. Nothing a dose is missing afterwards is
  kept: the folder has the final say.
- A folder with **no export** (or exports holding no dose) fails the run
  rather than leave the series empty, on a first run too: a `WATCH_DIR`
  pointing at the wrong folder, or a share that didn't mount, must not look
  like a source with nothing new, and must never wipe the history. Nothing is
  dropped in that case. The NAS's own sidecars (`._MyTherapy Archive.csv`,
  `@eaDir`, `#recycle`) are not exports: hidden files are ignored.
- There is no upload form: one file alone can't be merged with the exports
  around it, and the series is rebuilt from the folder. Save the export to the
  folder instead.

## Privacy

The exports hold the names your app displays (aliases), the doses' notes and
the app's ids: none of it leaves. Only the names
`TRIBUTARY_MYTHERAPY_NAMES` gives, the app's own word for a dose's status and
its unit reach InfluxDB, plus numbers and dates -- as the job that first wrote
these series did.

The export's `note` column, free text, is **not** written: no import of these
series ever carried one, and free-form text stays out of InfluxDB
(`docs/schema.md`). A dose's note therefore stays in the export.

## Ported from

A daily CronJob (`mytherapy-import`, 07:30 Europe/Paris) that ran
`mytherapy_import.py` over the same folder: the parsing, the alias mapping and
the dedup rules are the same, and this connector writes exactly its series
(tags, fields, types and times), so the switch rewrote the history in place
and did not duplicate it (`docs/porting.md`). Left out, and why:

- the note column (above);
- `type=measurement` rows (weight reminders: a scale already records weight);
- the job's cut-off date: **set `TRIBUTARY_MYTHERAPY_START_DATE`** to the day
  the overlap with the other import ended, exactly as the job had it (it held
  the date as a constant). Unset, every dose the exports hold is imported --
  including the range that import already wrote, in the same measurement under
  its own `source`: a dashboard summing doses across sources would count them
  twice.
