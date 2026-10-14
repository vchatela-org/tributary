# Compatible tools

Tributary writes a connector only for a source that has no maintained tool.
When one exists, use it: these tools write into the same InfluxDB (or sit
next to it in Grafana) and their data lines up with Tributary's, so one
Grafana can relate a domain to another (reading in the evening and sleep,
weight and activity...).

| Tool | Category it feeds | Source | Writes | Notes |
|---|---|---|---|---|
| [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) | Body & health, Sleep | Garmin Connect | InfluxDB `GarminStats` | Activity, sleep, heart rate, body battery... from the Garmin API, with its own dashboards. Point it at the same InfluxDB: Tributary's Healthspan, Sleep, Activity & fitness and Correlations dashboards draw it next to the other sources ([what they read](dashboards-design.md#series-written-by-other-tools)). |
| [Whoops](https://github.com/kryoseu/whoops) | Sleep | Whoop | PostgreSQL | Sleep, recovery, strain. Add a Grafana PostgreSQL datasource next to the InfluxDB ones. |
| [Takeback](https://github.com/vchatela-org/takeback) | Reading & media, Productivity | GDPR "download your data" archives (Amazon, Google, Meta...) | InfluxDB `MediaStats`, `ShoppingStats`, `DigitalStats` | Browses an archive, and pushes the numbers no dedicated tool provides: Kindle reading sessions (`source=kindle`), Audible, purchases, activity counts. Follows [the schema](schema.md). |

The categories are the ones the UI and the README group every integration under
([`app/categories.py`](../app/categories.py)): a tool appears beneath each
category it feeds, and Tributary neither runs nor configures it. It is listed so
a domain is complete on the page, not to be set up there.

## When garmin-grafana's data last arrived

Tributary does not run garmin-grafana, but it shows how fresh its data is: one
read-only line on its card, on the overview (under each category it feeds) and
in Settings, under *Compatible tools*:

> Garmin data up to **2026-10-12 13:14** · last watch upload **2026-10-13 07:47**

- *Data up to* is the time of the newest `HeartRateIntraday` point
  (`SELECT last("HeartRate") FROM "HeartRateIntraday"`): garmin-grafana's own
  cursor, where its next fetch starts.
- *Last watch upload* is the time of the newest `DeviceSync` point
  (`SELECT * FROM "DeviceSync" ORDER BY time DESC LIMIT 1`), which
  garmin-grafana writes when it sees a new upload from the watch.
- **Only the times are read.** The values that come back with them (a heart
  rate, the device's name) are dropped unread: never kept, logged or shown.
- Times are in `TRIBUTARY_TZ`, like the rest of the UI. When the newest data is
  older than `TRIBUTARY_GARMIN_GRAFANA_STALE_HOURS`, the line is marked
  *Over 24 h old* with the warning sign: a warning, never an error, since
  Tributary does not run the tool.
- No InfluxDB, an unreachable or slow one, no `GarminStats` database or no point
  in it: the line reads *No Garmin data seen*, and its tooltip says which. It
  never fails a page.
- It is read when the overview or Settings is opened, at most once every 5
  minutes (garmin-grafana itself looks for an upload at that pace), and given up
  on after 2 seconds; the overview's own refresh never reads it.

**What it cannot tell is why.** garmin-grafana writes nothing when it polls and
finds no new upload, so a stopped fetcher and a watch that has not synced look
the same from InfluxDB: the line says *when* data last arrived, not *why*.
garmin-grafana's own logs say which. To be told rather than to look, a Grafana
alert on the same query ("no `HeartRateIntraday` point for 24 h") is the better
tool.

| Variable | Default | |
|---|---|---|
| `TRIBUTARY_GARMIN_GRAFANA_DATABASE` | `GarminStats` | The database garmin-grafana writes (its `INFLUXDB_DATABASE`). Empty: no line. |
| `TRIBUTARY_GARMIN_GRAFANA_STALE_HOURS` | `24` | Older than this, the line is marked stale. |

It reads InfluxDB 1.x, as the rest of Tributary does: a garmin-grafana writing
to InfluxDB 3 is not seen. With several Garmin accounts in one database, the
line shows the newest of them all.

## The rules that keep them compatible

- One writer per source: never sync the same source with two tools into
  the same database. A tool that already covers a source wins.
- A tool writing into a shared database follows [the schema](schema.md):
  its own `source` value, the same field types for a shared measurement.
