# Renpho

[Renpho](https://renpho.com/) smart scales store each weigh-in in the
Renpho cloud. Tributary syncs the weigh-ins of one or several Renpho
accounts (one per person) into `BodyStats` (see [the schema](../schema.md)),
and, for the people you give level boundaries, the level the Renpho app
rates each reading with.

Renpho has no public API: Tributary speaks the app's through
[renpho-api](https://pypi.org/project/renpho-api/), an unofficial client.
A change on Renpho's side can break it until that client follows.

## Setup

1. Each person needs a Renpho account with a password: an account created
   with Google or Apple needs one set in the app first.
2. Set:

   | Variable | |
   |---|---|
   | `TRIBUTARY_RENPHO_EMAIL` | The first account's email. |
   | `TRIBUTARY_RENPHO_PASSWORD` | Its password. |
   | `TRIBUTARY_RENPHO_USER` | Its `user` tag (default: `TRIBUTARY_INFLUXDB_USER`). Required when there are several accounts. |
   | `TRIBUTARY_RENPHO_EMAIL_2`, `TRIBUTARY_RENPHO_PASSWORD_2`, `TRIBUTARY_RENPHO_USER_2` | A second person's account, all three together; then `_3`... up to `_9`, without a gap. Each `user` must differ. |
   | `TRIBUTARY_RENPHO_THRESHOLDS` | Optional: the boundaries of each person's levels, JSON, see [Levels](#levels). |
   | `TRIBUTARY_RENPHO_INTERVAL_MINUTES` | Minutes between two syncs (default 1440, once a day). |

3. In Tributary, *Dry run* shows the line protocol of what the first sync
   would write; *Run now* writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `body_composition` | weigh-in | the weigh-in's | `source=renpho`, `user`, `scale`, `user_id` | a float per metric measured: `weight_kg`, `bmi`, `body_fat_pct`, `body_water_pct`, `muscle_pct`, `bone_pct`, `bmr_kcal`, `visceral_fat`, `subcutaneous_fat_pct`, `protein_pct`, `body_age`, `lean_body_mass_kg`, `fat_free_weight_kg`, `heart_rate_bpm`, `cardiac_index`, `body_shape` |
| `body_levels` | weigh-in, for a person with boundaries | the weigh-in's | the weigh-in's | per rated metric, its level (float, 0 = the lowest) |
| `body_thresholds` | person with boundaries, when they change | the run's | `user` only | `<metric>_t1`...`_tN` (the boundaries), `<metric>_min` / `_max` (a gauge's scale) |

- Renpho reports a metric the scale didn't measure as 0: Tributary leaves
  it out rather than write a false zero. A record without a time or any
  metric is skipped.
- Field names follow the Renpho API, not always the app: `bone_pct` is the
  bone *mass* in kg, `lean_body_mass_kg` the app's "Muscle Mass",
  `muscle_pct` its "Skeletal Muscle Percentage", `body_age` its "Metabolic
  Age".
- `scale` is the scale's model, which Renpho reports for some scales only
  (a scale's older and newer readings may differ); `user_id` is the Renpho
  account's numeric id.
- Two records of an account in the same second with the same tags are one
  point, the later record's values winning.

## Levels

The Renpho app rates each reading: Below Avg., Average, Above Avg.,
Over... The boundaries between those levels depend on the person (Renpho
derives them from sex, age and height), so you copy them from the app's
*Metric Detail* screens (the numbers above the coloured bar), per person
and metric, lowest first:

```sh
TRIBUTARY_RENPHO_THRESHOLDS='{"alex": {"weight_kg": [58.5, 79.0, 94.8], "bmi": [18.5, 25.0, 30.0], "body_fat_pct": [6.0, 13.0, 17.0, 25.0], "bmr_kcal": [1550]}}'
```

The keys are `user` tags. A metric has one boundary fewer than its levels:

| Metric | Levels | Boundaries |
|---|---|---|
| `weight_kg`, `bmi`, `visceral_fat` | 4 | 3 |
| `body_fat_pct` | 5 | 4 |
| `subcutaneous_fat_pct`, `lean_body_mass_kg`, `muscle_pct`, `bone_pct`, `body_water_pct`, `protein_pct` | 3 | 2 |
| `body_age`, `bmr_kcal` | 2 | 1 |

A value on a boundary gets the upper level. A person or metric left out
gets no level. A mistake in the variable fails the runs (with what is
wrong), not Tributary's start.

When the boundaries change, the next run writes them (`body_thresholds`)
and rewrites the level of every weigh-in it changes. A dashboard reads the
latest boundaries of a person with `ORDER BY time DESC LIMIT 1` and no time
filter.

## How it syncs

- Each run logs in to every account and reads its whole history: Renpho
  has no "since", and a few hundred weigh-ins take a second or two. It
  writes the weigh-ins that are new or changed: a new one, one assigned to
  the person later in the app (with its own, older time), one whose level
  moved. Nothing when nothing changed.
- An account that fails (a changed password...) doesn't stop the others:
  their weigh-ins are written and the run shows the failure; the failed
  account is read again at the next run.
- *Full resync* rewrites every weigh-in and the boundaries: harmless, the
  same points overwrite themselves.
- Nothing is ever dropped: a weigh-in deleted in the app stays in InfluxDB.
- Not synced (yet): the Renpho smart tape measure's girths, and the weigh-ins
  of another Renpho user id read with an account's login.

## Privacy

Renpho's login answer carries the account's profile (email, name, birth
date...) and a token; each record carries the person's height and sex, and
details of the scale. Tributary keeps none of it: the client lives for one
run, each record is reduced to its time, scale model, user id and metrics,
and the cursor holds short digests of what was written (no value). Passwords live in the
environment only, never in a log, the database or an API answer, and an
error from Renpho is shown without the account's email or password. Tests
check all of this.
