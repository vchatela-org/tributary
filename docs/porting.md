# Porting a feed into Tributary

How to replace a job that already writes a source into InfluxDB (a
CronJob, a cron script...) with a Tributary connector, **without a
duplicate or a gap**. The [Renpho connector](connectors/renpho.md) was the
first port and serves as the example throughout.

The idea: InfluxDB keys a point by measurement + tag set + time, and
writing the same key again overwrites it. A connector that writes *exactly*
the job's series rewrites its history in place. One that adds, drops or
renames a single tag starts new series next to the old ones, and every
`sum()`, `count()` or `mean()` that doesn't group by that tag counts each
reading twice. So: same series, proved equal, then switch, then retire.

1. [Inventory](#1-inventory-what-the-job-really-writes) what the job really writes.
2. [Port](#2-port-the-same-series) it as a connector writing the same series.
3. [Prove equality](#3-prove-equality) with a dry run against the live data.
4. [Switch](#4-switch-suspend-the-job-dont-delete-it): release, deploy, suspend the job.
5. [Retire](#5-retire-after-a-clean-cycle) the job after a clean cycle.

## 1. Inventory what the job really writes

Read the database, not the job's README: docs drift, the data doesn't.

```sh
influx -database BodyStats -execute 'SHOW MEASUREMENTS'
influx -database BodyStats -execute 'SHOW SERIES FROM body_composition'
influx -database BodyStats -execute 'SHOW TAG KEYS'
influx -database BodyStats -execute 'SHOW FIELD KEYS'
influx -database BodyStats -execute 'SHOW RETENTION POLICIES'
```

Then the job's code, for what the data can't tell:

- **Every measurement**, including side ones a dashboard needs (Renpho's
  job also wrote `body_levels` and `body_thresholds`, which the dashboard
  colours its tiles with: leave them out and the colours freeze).
- **Every tag key and where its value comes from**. The Renpho job's docs
  named `source` and `user`; the series also carried `scale` and `user_id`,
  and older readings had a `scale` value newer ones lack. All of them are
  part of the key.
- **Field names and types** (`SHOW FIELD KEYS`). InfluxDB 1.x refuses a
  write whose type differs from what the shard holds: `72` (float) and
  `72i` (integer) are not the same field.
- **Times**: the reading's own time, or the run's? To the second? The
  Renpho job stamped weigh-ins with their time, and `body_thresholds` with
  the run's.
- **What it drops**: empty values, zeros standing for "not measured"
  (Renpho), records without a time.
- **What happens to two records with the same key**: written one after the
  other, the later one's fields win. Tributary's core *sums* the numbers of
  points sharing a key within a run (two reading sessions in the same
  second must add up), so a port whose records replace each other declares
  its measurements `last_wins=True` (Todoist), or merges them itself
  (Renpho).
- **How much history each run rewrites** (the whole history, a window of
  days...), and its schedule.
- **Its configuration**: accounts, the `user` tag of each, optional
  features actually turned on (port those; leave the unused ones out and
  say so).

## 2. Port: the same series

Write the connector ([the contract](connectors/README.md)) so that each
point is the job's, key and values:

- **Tags**: the same keys and values, nothing more, nothing less. A point
  sets its own `user` (one account per person), or `None` when the job
  wrote none; a measurement without a `source` tag is declared
  `tagged_source=False`, one never carrying a `user` tag
  `tagged_user=False`.
- **Fields**: the same names, every one declared with the type the database
  holds; values formatted the same (Python floats print the same digits
  InfluxDB stores).
- **Times**: the same instant, to the second. A weigh-in or a session
  carries its own time: never ingest "now" for data that has one.
- **No schema change** while porting: a rename, a new tag or field, a
  fixed typo all wait for a separate change, made with the dashboards.
- **History**: the connector must be able to rewrite everything the job
  wrote (its first run, or a *Full resync*); later runs may send only what
  changed (a cursor), as long as nothing the job would have caught is
  missed (Renpho: a weigh-in assigned later in the app keeps its old time,
  so a "newest time seen" cursor would miss it; the connector keeps digests
  of what it wrote instead).
- Same calls to the source where it matters: the Renpho connector uses the
  same client library, at the same version, as the job.
- **A job that reads a folder of exports** has that folder as its source of
  truth: the connector declares `rebuild_own_source` (its own series is
  dropped and rewritten from the folder at each change, as the [contract](connectors/README.md)
  says) and `accepts_uploads = False` (one file alone can't be merged with the
  others). When the exports supersede one another -- a newer one replacing
  what the older ones said over its own date range -- the connector merges
  them in `import_files`, which the core calls with the whole folder at once.
  A run that finds no export must fail rather than let the core drop the
  series with nothing to write back.
- Tests pin the exact line of a typical record, written by hand from the
  job's code.

## 3. Prove equality

Before anything is switched, compare the connector's dry run with what the
job wrote. [`scripts/equality-diff.py`](../scripts/equality-diff.py) does
the comparison (standard library only) and prints counts and field names,
never a value.

1. **The dry run**, with the job's configuration (same accounts, same
   `user` tags, same options) and an **empty cursor**, so it covers the
   whole history: a fresh `TRIBUTARY_DATA_DIR`, or *Full resync* + *Dry
   run*. Save its line protocol: the run's `preview`
   (`GET /api/runs/{id}`, capped at 2000 lines), or for a longer history:

   ```sh
   TRIBUTARY_DATA_DIR=$(mktemp -d) python - > dryrun.lp <<'EOF'
   from app import connectors
   from app.config import settings
   from app.points import encode
   c = connectors.get("renpho")
   for db, lines in encode(c.sync(None).points, c.source, settings.influxdb_user, settings.tz).items():
       print("\n".join(lines))
   EOF
   ```

2. **What InfluxDB holds**, tags grouped, times in seconds, and the stored
   field types:

   ```sh
   influx -database BodyStats -format json -precision s -execute \
     "SELECT * FROM body_composition WHERE \"source\"='renpho' GROUP BY *; \
      SELECT * FROM body_levels WHERE \"source\"='renpho' GROUP BY *; \
      SELECT * FROM body_thresholds GROUP BY *" > influx.json
   influx -database BodyStats -format json -execute 'SHOW FIELD KEYS' > field-keys.json
   ```

3. **The diff**:

   ```sh
   python3 scripts/equality-diff.py dryrun.lp influx.json --types field-keys.json \
     --latest body_thresholds                       # stamped with the run time: newest point per series
   python3 scripts/equality-diff.py dryrun.lp influx.json --types field-keys.json \
     --latest body_thresholds --since $(( $(date +%s) - 90*86400 ))   # the last 90 days only
   ```

   Every measurement must read `N equal, 0 differing, 0 only in the dry
   run, 0 only in InfluxDB`, and the types line must say every field has
   the type InfluxDB holds. The output ends with `EQUAL` (exit 0) or
   `DIFFERENT` (exit 1). `--show-values` prints the differing points to
   investigate, on your own machine only.

   "Only in InfluxDB" can be legitimate: a record the source deleted since
   (nothing is ever dropped from InfluxDB, by the job or by Tributary).
   Anything else is a mapping difference to fix before going further.

Keep the dry-run output and the dumps out of the repository and delete them
afterwards: they are the person's data.

Credentials for the dry run are the job's. Use them where they already
are; copying them to their new home is part of the switch (step 4).

## 4. Switch: suspend the job, don't delete it

1. A Tributary release with the connector.
2. Its credentials in Tributary's environment, renamed `TRIBUTARY_<KEY>_*`
   (for Renpho: `RENPHO_EMAIL` → `TRIBUTARY_RENPHO_EMAIL`, `RENPHO_USER_2`
   → `TRIBUTARY_RENPHO_USER_2`...), plus whatever configuration the job had
   (Renpho's level boundaries → `TRIBUTARY_RENPHO_THRESHOLDS`).
3. One deployment change that does both: the new Tributary with the
   connector's settings, and the old job **suspended** (Kubernetes:
   `suspend: true` on the CronJob; cron: comment the line). Not deleted:
   resuming it is the rollback.
4. Before the connector's first run, count the points per day:

   ```sh
   influx -database BodyStats -execute "SELECT count(weight_kg) FROM body_composition \
     WHERE \"source\"='renpho' AND time > now() - 400d GROUP BY time(1d), * fill(none)" > before.txt
   ```

   After its first run (a minute after the start), check the run in
   Tributary (status, points written), count again into `after.txt` and
   compare: the same counts, day by day and series by series (a new day
   only where a new reading arrived). One more series, or a day counting
   double, is a duplicate: suspend Tributary's connector (unset its
   credentials), resume the job, and go back to step 2.

## 5. Retire after a clean cycle

Leave the job suspended for at least one of its own cycles (a week for a
weekly job) while Tributary's runs come out clean: status ok, the same
counts per day as before (plus the new readings). Then, in a second
deployment change:

- delete the job and what only it used (its secrets objects, service
  account, config);
- revoke what gave it access (its secret store role and policy), delete
  its credentials entry, and archive its repository.

Write down what was ported and what wasn't (Renpho: the job's optional tape
measure (`girth`) sync and its extra-user-ids option were off, so they were
left out), so that nobody looks for them later.
