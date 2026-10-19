# Running Tributary

Tributary is one process: a web UI and API (port 8000), and a scheduler
thread that runs the connectors one at a time. It needs:

- an InfluxDB 1.x it can reach (`TRIBUTARY_INFLUXDB_URL`);
- a writable folder for its own SQLite database (run history, cursors, and
  the encrypted tokens of a source connected in the browser):
  `/app/data` in the image (`TRIBUTARY_DATA_DIR`). It holds no measurement:
  losing it only means the next run reads its sources from the start again,
  rewriting the same points, and connecting Whoop again.
- for Whoop, a secret key (`TRIBUTARY_SECRET_KEY`, below), kept apart from
  that folder.

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
| `TRIBUTARY_GRAFANA_URL` | – | The Grafana a browser reaches (`https://grafana.example.com`, a subpath works): the UI's *Dashboards* link opens its dashboard browser, filtered to the dashboards in `grafana/dashboards`. Tributary never calls it. Unset: no link. |
| `TRIBUTARY_DATA_DIR` | `./data` (`/app/data` in the image) | Run history and cursors (SQLite). |
| `TRIBUTARY_FIRST_RUN_DELAY` | `60` | Seconds between the start and the first scheduled run. |
| `TRIBUTARY_MAX_UPLOAD_MB` | `200` | Largest file the upload form takes. |
| `TRIBUTARY_DEBUG` | `false` | Debug logging. |
| `TRIBUTARY_GARMIN_GRAFANA_DATABASE` | `GarminStats` | Where [garmin-grafana](compatible-tools.md#when-garmin-grafanas-data-last-arrived) writes: two timestamps are read from it for the line on its card, never a value. Empty: no line. |
| `TRIBUTARY_GARMIN_GRAFANA_STALE_HOURS` | `24` | garmin-grafana's line is marked stale (a warning) when its newest data is older than this. |
| `TRIBUTARY_SECRET_KEY` | – | Encrypts the tokens of a source you connect in the browser (Whoop): the base64 of 32 random bytes, `openssl rand -base64 32`. Without it (or `_FILE`), such a source can't be connected: tokens are never stored in plain text. |
| `TRIBUTARY_SECRET_KEY_FILE` | – | A file holding that key instead (a Docker or Kubernetes secret mounted read-only). Set one of the two. |
| `TRIBUTARY_SECRET_KEY_PREVIOUS` | – | While rotating the key: the old one, still read, never written ([how](connectors/whoop.md#rotating-the-secret-key)). |

Each connector has its own, documented with it ([connectors](connectors/README.md)).

### The secret key

Tributary keeps one kind of secret itself: the access and refresh tokens of a
source connected in the browser (Whoop's OAuth). They are encrypted with
AES-256-GCM before they reach the SQLite database, bound to their connector,
with a key that never sits in the data folder. Keep the key with your other
secrets (a secret store, a Docker secret), not next to the data: a copy of the
folder alone (a volume backup) then holds nothing usable. A lost or wrong key
costs only the connection: connect Whoop again on its page.

## Docker Compose

See the README's quick start: `docker-compose.yml` runs Tributary, InfluxDB
1.x and Grafana with the datasources and dashboards of `grafana/`
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
- **No shell in the image.** It is Python and the app on a minimal base
  (Chainguard's), with no shell or package manager: probes are HTTP, and
  `kubectl exec` runs `python`, not `sh`.
- **Probes** on `GET /api/health`. Startup never blocks: the server
  listens at once, and the first scheduled run comes
  `TRIBUTARY_FIRST_RUN_DELAY` seconds later, in the background.
- **Secrets** as environment variables from a Secret (`envFrom`),
  `TRIBUTARY_SECRET_KEY` included (or mounted as a file for
  `TRIBUTARY_SECRET_KEY_FILE`). Add a new key to the secret store before the
  release that needs it reaches the cluster: an operator that syncs secrets
  periodically may take a while to copy it.
- Reach sources and InfluxDB in-cluster rather than through the ingress and
  its authentication. Whoop's connection is the exception that needs the
  ingress: your browser comes back from WHOOP to
  `https://<host>/connectors/whoop/callback`, through your proxy's login like
  any page ([details](connectors/whoop.md#behind-an-authenticating-proxy)).
  Tributary itself only needs outbound HTTPS to `api.prod.whoop.com`.

## Upgrades

Runs are recorded and cursors kept across versions. A run still going when
the process stops is marked as interrupted at the next start, and the
next run picks up from the last saved cursor. If a new version changes how
a connector maps its data, a *Full resync* rewrites its history (the same
keys overwrite themselves; a changed tag set would be a new series, which
the release notes will say).
