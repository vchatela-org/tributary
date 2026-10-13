# Compatible tools

Tributary writes a connector only for a source that has no maintained tool.
When one exists, use it: these tools write into the same InfluxDB (or sit
next to it in Grafana) and their data lines up with Tributary's, so one
Grafana can relate a domain to another (reading in the evening and sleep,
weight and activity...).

| Tool | Category it feeds | Source | Writes | Notes |
|---|---|---|---|---|
| [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) | Body & health, Sleep | Garmin Connect | InfluxDB `GarminStats` | Activity, sleep, heart rate, body battery... from the Garmin API, with its own dashboards. Point it at the same InfluxDB. |
| [Whoops](https://github.com/kryoseu/whoops) | Sleep | Whoop | PostgreSQL | Sleep, recovery, strain. Add a Grafana PostgreSQL datasource next to the InfluxDB ones. |
| [Takeback](https://github.com/vchatela-org/takeback) | Reading & media, Productivity | GDPR "download your data" archives (Amazon, Google, Meta...) | InfluxDB `MediaStats`, `ShoppingStats`, `DigitalStats` | Browses an archive, and pushes the numbers no dedicated tool provides: Kindle reading sessions (`source=kindle`), Audible, purchases, activity counts. Follows [the schema](schema.md). |

The categories are the ones the UI and the README group every integration under
([`app/categories.py`](../app/categories.py)): a tool appears beneath each
category it feeds, and Tributary neither runs nor configures it. It is listed so
a domain is complete on the page, not to be set up there.

## The rules that keep them compatible

- One writer per source: never sync the same source with two tools into
  the same database. A tool that already covers a source wins.
- A tool writing into a shared database follows [the schema](schema.md):
  its own `source` value, the same field types for a shared measurement.
