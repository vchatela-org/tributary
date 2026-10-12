# Running Tributary

Tributary is one process: a web UI and API (port 8000), and a scheduler
thread that runs the connectors one at a time. It needs:

- an InfluxDB 1.x it can reach (`TRIBUTARY_INFLUXDB_URL`);
- a writable folder for its own SQLite database (run history, cursors):
  `/app/data` in the image (`TRIBUTARY_DATA_DIR`). It holds no measurement:
  losing it only means the next run reads its sources from the start again,
  rewriting the same points.

## No login: keep it private

Tributary has no accounts. Anyone who reaches its port can start runs and
read the run history (which names books). Either:

- keep it local: the provided `docker-compose.yml` binds it to
  `127.0.0.1:8000`; or
- put an authenticating reverse proxy in front: Authentik, Authelia or
  oauth2-proxy as forward auth on Traefik, Caddy or nginx. Only the proxy
  should reach the port.

Requests that a browser marks as coming from another site are refused, so
a malicious page can't start a run through your logged-in proxy session.

## Configuration

Every setting is an environment variable (or a line in `.env`).

| Variable | Default | |
|---|---|---|
| `TRIBUTARY_INFLUXDB_URL` | – | InfluxDB 1.x base URL, e.g. `http://influxdb:8086`. Without it nothing is written; dry runs still work. |
| `TRIBUTARY_INFLUXDB_USER` | – | The `user` tag of every point. |
| `TRIBUTARY_INFLUXDB_CREATE_DATABASES` | `true` | Create missing databases (52-week shards). Existing ones are never altered. |
| `TRIBUTARY_TZ` | `Europe/Paris` | Time zone of `local_hour` / `local_weekday`, and of the UI's times. |
| `TRIBUTARY_DATA_DIR` | `./data` (`/app/data` in the image) | Run history and cursors (SQLite). |
| `TRIBUTARY_FIRST_RUN_DELAY` | `60` | Seconds between the start and the first scheduled run. |
| `TRIBUTARY_MAX_UPLOAD_MB` | `200` | Largest file the upload form takes. |
| `TRIBUTARY_DEBUG` | `false` | Debug logging. |

Each connector has its own, documented with it ([connectors](connectors/README.md)).

## Docker Compose

See the README's quick start: `docker-compose.yml` runs Tributary, InfluxDB
1.11 and Grafana with the datasources and dashboards of `grafana/`
provisioned. To use an InfluxDB you already have, drop the `influxdb`
service and point `TRIBUTARY_INFLUXDB_URL` at yours; for an existing
Grafana, copy the datasource from `grafana/provisioning/datasources/` and
import the dashboards from `grafana/dashboards/`.

## Kubernetes

Tributary isn't Kubernetes-specific: it's the same container.

- **One replica.** The scheduler runs in the process; two replicas would
  run every sync twice (harmless for the data, wasteful for the sources).
  Use the `Recreate` strategy so two never overlap during an upgrade.
- **A small persistent volume** at `/app/data`. The image runs as uid
  10001: set `securityContext.fsGroup: 10001` so it can write there.
- **Probes** on `GET /api/health`. Startup never blocks: the server
  listens at once, and the first scheduled run comes
  `TRIBUTARY_FIRST_RUN_DELAY` seconds later, in the background.
- **Secrets** as environment variables from a Secret (`envFrom`).
- Reach sources and InfluxDB in-cluster rather than through the ingress and
  its authentication.

## Upgrades

Runs are recorded and cursors kept across versions. A run still going when
the process stops is marked as interrupted at the next start, and the
next run picks up from the last saved cursor. If a new version changes how
a connector maps its data, a *Full resync* rewrites its history (the same
keys overwrite themselves; a changed tag set would be a new series, which
the release notes will say).
