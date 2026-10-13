# MyFitnessPal

[MyFitnessPal](https://www.myfitnesspal.com/) logs food, exercise and
weigh-ins. It has no API, so Tributary takes the export you import in its UI
and writes it into `BodyStats..body_composition`, `..nutrition` and
`..exercise` (see [the schema](../schema.md)).

An export covers the date range you pick when exporting (the app writes it in
each file's name: `...-du-2019-01-01-au-2024-12-31.csv`). An import
**replaces** the connector's three series **over those days**: a workout you
removed from the app, or a meal it no longer has, follows the export you
import. The days outside the range keep what they had, so exporting the last
month only adds it to the history already there. Nothing else in `BodyStats`
is touched. The run says which days it replaced ("after dropping its own
series from 2019-01-01 to 2024-12-31").

## Setup

1. In MyFitnessPal, *Export Data* for the dates you want (a zip of three
   CSVs: weigh-ins, food by meal, exercise). The first time, the whole
   history; then any range, overlapping the last one or not.
2. In Tributary, *Dry run* next to the file shows the line protocol of what
   the import would write and the days it would replace; *Import file*
   writes it.

Upload only: Tributary never watches a folder for this connector, since a
folder of exports, each its own range, is not the whole history a watched
folder's rebuild would make it.

There is nothing else to configure: the export carries it all. The `user` tag
and the weigh-in dedup both read `TRIBUTARY_INFLUXDB_USER` (see below); without
it the import fails rather than risk importing the scales' readings twice.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `body_composition` | weigh-in the app alone holds | noon local on the date | -- | `weight_kg`, `body_fat_pct` (float) |
| `nutrition` | logged meal | noon local on the date | `meal` | the four macros (float), and a nutrient the entry reported |
| `exercise` | workout or calorie adjustment | noon local + the entry's index that day | `category` | `activity`, `energy_kcal` (float), `duration_s` (int, absent on an adjustment) |

- **The scales' weigh-ins are not copied.** The app shows the readings your
  Withings and Renpho scales synced into it, and those are already in
  `BodyStats` under their own `source`. A weigh-in is imported only when no
  other source has a reading the same local day within 0.15 kg. This reads
  InfluxDB, so an import needs `TRIBUTARY_INFLUXDB_URL` too (as the job did,
  even for a dry run). It is also why a weigh-in a scale reported on the same
  day *after* you imported the export is not written again: the day's reading
  belongs to the scale, and importing an export that covers that day drops
  the app's copy.
- **Times are noon local** (`TRIBUTARY_TZ`, the export has dates only): a day
  lands on the right date whether a query groups in UTC or in Paris. Several
  workouts on one date are a second apart, by their order in the day. Do not
  compare these times with a source that stamps the real clock time.
- **Duplicate workouts are collapsed**, as the job did: rows identical in
  date, activity, calories and minutes (a linked app pushed the same workout
  again), and same-day workouts with the same calories (two linked apps each
  pushed every one) -- the shortest is kept, the one whose duration fits the
  calories.
- **A calorie adjustment is not a workout** (`category=adjustment`): it is
  the extra calories a linked device added to the day's budget, and its
  fixed "1 minute" is a placeholder. Filter it out with
  `"category" != 'adjustment'`. Garmin's daily step count on those rows is
  not imported either (`GarminStats` has it).
- **A food entry's 0 in a secondary nutrient means "not reported"**, so it is
  left absent and daily sums of those are lower bounds. The macros always
  carry their 0. The four vitamin/mineral columns (A, C, calcium, iron) are
  not imported: the export states no unit and mixes % of daily value with
  amounts.

## Privacy

The export holds the food and workout names you typed, the entries' notes and
the app's ids. Only the records' numbers and dates, the app's own name for an
exercise (`activity`), and the contract's fixed words for a meal and a
category reach InfluxDB -- as the job that first wrote these series did. The
export's `note` column, free text, is **not** written, and neither is anything
identifying the account.

## Ported from

A one-off import script (`myfitnesspal_import.py`), run by hand through a
port-forward over a single export, writing the same three series. The parsing,
the dedup and the duplicate-workout rules are the same, and this connector
writes exactly its series (tags, fields, types and times), so importing the
export rewrites the history in place and does not duplicate it
(`docs/porting.md`). Left out, and why:

- the four vitamin/mineral columns and the free-text `note` column (above);
- Garmin's step count on the calorie-adjustment rows (above);
- `--replace`: an import always rebuilds its own series over the export's
  days, as the script did with `--replace` (over the whole history) on a
  newer export. A newer export shifts the per-day workout timestamps, and
  only a rebuild keeps them right. Unlike the script, an export of a shorter
  range leaves the days before and after it as they were.
