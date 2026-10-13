# Medisafe

[Medisafe](https://www.medisafeapp.com/) is a medication reminder: it knows
every dose it scheduled and what you answered. It has no API, so Tributary
takes the dose history export you import in its UI and writes the doses into
`BodyStats..medication` (see [the schema](../schema.md)), the measurement the
[MyTherapy](mytherapy.md) connector writes too, under its own `source`.

An import **replaces** the connector's series **over the days the export
covers** (its first dose to its last, up to `TRIBUTARY_MEDISAFE_END_DATE`): a
dose you answered after an earlier export (pending, then taken) follows the
export you import instead of staying twice. Only `source=medisafe` points of
`TRIBUTARY_INFLUXDB_USER` are replaced (without it the import is refused);
the days outside the export, and everything else in `BodyStats`, are left as
they are. The run says which days it replaced.

## Setup

1. In Medisafe, export your dose history (`Doses_<account>_000.csv`, or a
   zip holding it). If the app splits it into parts (`_000`, `_001`...),
   import the zip holding them all: they are read as one export, whereas a
   part alone would replace the days it shares with the others. Its first
   line names the columns:
   `"group_id","name","condition","Scheduled for",...`. A re-save with `;` or
   tabs between them, or the names padded or cased differently, reads the
   same. Any other file is refused, and the run says what it holds instead:
   empty, a web page (a failed download), the columns it has, or a first line
   that isn't column names.
2. Optionally, curate the names and the notes:

   | Variable | |
   |---|---|
   | `TRIBUTARY_MEDISAFE_NAMES` | `{"<export name>": "<name>" or null}`: rename a medication (two names may become one), or leave it out (`null`). A name not in it is written as the export spells it. |
   | `TRIBUTARY_MEDISAFE_NOTES` | `true` to write each dose's note (free text you typed in the app), whole. Off by default. |
   | `TRIBUTARY_MEDISAFE_END_DATE` | Doses scheduled after this date (`YYYY-MM-DD`) are not imported, and nothing after it is replaced: the day another app (MyTherapy) took the doses over, so a reminder left running in Medisafe never doubles them. |

   The names are your medications': the mapping belongs to your environment,
   never to this repository. It is read at each import, so a mistake fails
   the import (and says why), not the app's start. Set the mapping and the
   notes **before** the first import of a history another tool wrote with
   them: the import replaces the export's days with what these settings
   make of it, and the dry run shows it.
3. In Tributary, *Dry run* next to the file shows the line protocol of what
   the import would write and the days it would replace; *Import file*
   writes it.

Upload only: Tributary never watches a folder for this connector.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `medication` | scheduled dose | the time it was scheduled, **UTC** as the export gives it | `med`, `status` | `taken` (1 or 0), `app_status`, `dose`, `unit`, `strength`, `delay_min`, and `note` with `TRIBUTARY_MEDISAFE_NOTES` |

- **Times are UTC.** The export doesn't say so, but every schedule moves by an
  hour on the DST dates (a 12:00 Paris dose is 11:00 UTC in winter, 10:00 in
  summer); MyTherapy's are local. A stretch with off-hour schedules usually
  is the phone's time zone while travelling.
- `status` is `taken`, `skipped` (the app's `dismissed`), `missed`, or
  `unlogged` (the app's `pending`, `snooze` or `deleted`: a reminder nobody
  answered, or a dose the user removed in the app, says nothing about the
  dose); `app_status` keeps the app's own word. `taken` is 1 for `taken` and 0
  otherwise.
  - A dose the app reports as `deleted` (removed in the app) is kept too:
    `status=unlogged`, `app_status=deleted`, `taken=0`, no `delay_min`. It is
    in the days the import replaces, so re-importing a current export rewrites
    any such dose an earlier export left as `taken` -- intended, not a
    regression.
- `delay_min`, for a taken dose only: from the scheduled time to the moment
  it was recorded, in whole minutes (floored; negative: early). A blank
  record time writes no delay, a blank dosage no `dose`.
- **Names are today's.** Medisafe exports a renamed entry under its current
  name back to its first dose, and a dose change makes a new schedule under
  the same name: two doses of one name and status in the same second (or two
  names `TRIBUTARY_MEDISAFE_NAMES` merges) are written a second apart rather
  than overwrite each other.
- `strength` is the medication's ("10 mg"), as the app stores it.

## Privacy

The export holds your medications' names, the notes you typed, the schedules'
ids and, in its file name, your account number. Only the doses' numbers and
times, the app's own words (status, unit, strength), the `med` name (as the
export gives it or as your mapping renames it) and, if you ask for it, the
note reach InfluxDB. The schedule ids never do, and no name is ever logged: a
refused file's message quotes its first line only when it reads as column
names, never a row.
The account number is masked in the run's message (`Doses_<account>_000.csv`):
it is neither stored, shown nor logged.

## Ported from

A one-off import script (`medisafe_import.py`), run by hand through a
port-forward, with a curation of the names (some renamed or merged, two left
out) applied before the points are built. The parsing, the statuses, the UTC
times, the one-second shift and the fields are the same, and with that
curation as `TRIBUTARY_MEDISAFE_NAMES` and `TRIBUTARY_MEDISAFE_NOTES=true`
this connector writes exactly its series, so importing the export rewrites
the history in place (`docs/porting.md`). Left out: `group_id` (the schedule's
id) and `condition` (empty in every row). Unlike the script's `--replace`
(the whole history), an import replaces the days its export covers only.
