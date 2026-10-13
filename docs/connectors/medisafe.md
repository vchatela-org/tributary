# Medisafe

[Medisafe](https://www.medisafeapp.com/) is a medication reminder: it knows
every dose it scheduled and what you answered. It has no API, so Tributary
takes the dose history export you import in its UI and writes the doses into
`BodyStats..medication` (see [the schema](../schema.md)), the measurement the
[MyTherapy](mytherapy.md) connector writes too, under its own `source`.

An import **replaces** the connector's series **over the days the export
covers** (its first dose to its last): a dose you answered after an earlier
export (pending, then taken) follows the export you import instead of staying
twice. Only `source=medisafe` points of `TRIBUTARY_INFLUXDB_USER` are
replaced; the days outside the export, and everything else in `BodyStats`,
are left as they are. The run says which days it replaced.

## Setup

1. In Medisafe, export your dose history (`Doses_<account>_000.csv`, or a
   zip holding it).
2. Optionally, curate the names and the notes:

   | Variable | |
   |---|---|
   | `TRIBUTARY_MEDISAFE_NAMES` | `{"<export name>": "<name>" or null}`: rename a medication (two names may become one), or leave it out (`null`). A name not in it is written as the export spells it. |
   | `TRIBUTARY_MEDISAFE_NOTES` | `true` to write each dose's note (free text you typed in the app). Off by default. |

   The names are your medications': the mapping belongs to your environment,
   never to this repository. It is read at each import, so a mistake fails
   the import (and says why), not the app's start.
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
  `unlogged` (`pending` or `snooze`: a reminder nobody answered says nothing
  about the dose); `app_status` keeps the app's own word. `taken` is 1 for
  `taken` and 0 otherwise.
- `delay_min`, for a taken dose only: from the scheduled time to the moment
  it was recorded, in whole minutes (floored; negative: early).
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
note reach InfluxDB. The account number and the schedule ids never do, and no
name is ever logged.

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
