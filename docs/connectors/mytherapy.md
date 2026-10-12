# MyTherapy

[MyTherapy](https://www.mytherapyapp.com/) is a medication reminder: it knows
every dose it scheduled and whether it was taken. It has no API, and its
export holds the whole history, so Tributary takes the export you import in
its UI and writes the doses into `BodyStats..medication` (see
[the schema](../schema.md)).

An import **replaces** the connector's series: a dose you deleted in the app,
or answered after the export, follows the export you import. Nothing else in
`BodyStats` is touched.

## Setup

1. In MyTherapy, export your data (the app saves `MyTherapy Archive.zip`).
2. Name each of your medications, mapping the alias the app shows to the name
   the `med` tag should hold:

   | Variable | |
   |---|---|
   | `TRIBUTARY_MYTHERAPY_NAMES` | `{"<alias>": "<name>"}`, the app's alias to the name written in the `med` tag. Required. |
   | `TRIBUTARY_MYTHERAPY_START_DATE` | Doses scheduled before this date (`YYYY-MM-DD`) are not imported. Optional: see below. |

   The app itself keeps a deliberately vague alias per medication (that is how
   it is meant to be used): `TRIBUTARY_MYTHERAPY_NAMES` is where the real name
   enters, and it belongs to your environment, never to this repository. An
   alias the mapping doesn't know fails the import and lists the aliases to
   add: the dose is not written under a guessed name.
3. In Tributary, *Dry run* next to the file shows the line protocol of what
   the import would write; *Import file* writes it.

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
- One point per dose, the newest export's answer winning: importing an export
  the app wrote later replaces what the earlier one said.

## Privacy

The export holds the names your app displays (aliases), the doses' notes and
the app's ids: none of it leaves. Only the names `TRIBUTARY_MYTHERAPY_NAMES`
gives, the app's own word for a dose's status and its unit reach InfluxDB,
plus numbers and dates -- as the job that first wrote these series did.

The export's `note` column, free text, is **not** written: no import of these
series ever carried one, and free-form text stays out of InfluxDB
(`docs/schema.md`). A dose's note therefore stays in the export.

## Ported from

A daily CronJob (`mytherapy-import`, 07:30 Europe/Paris) that ran
`mytherapy_import.py` over a folder of exports, rebuilding the same series
from it every morning. The parsing, the alias mapping and the dedup rules are
the same, and this connector writes exactly its series (tags, fields, types
and times), so the imports rewrote the history in place and did not duplicate
it (`docs/porting.md`). Left out, and why:

- reading a folder: the exports are imported in the UI, one at a time, when
  the app has exported one (there is no share to watch);
- the note column (above);
- `type=measurement` rows (weight reminders: a scale already records weight);
- the job's cut-off date: **set `TRIBUTARY_MYTHERAPY_START_DATE`** to the day
  the overlap with the other import ended, exactly as the job had it (it held
  the date as a constant). Unset, every dose the export holds is imported --
  including the range that import already wrote, in the same measurement under
  its own `source`: a dashboard summing doses across sources would count them
  twice.
