# Tributary

Self-hosted, docker-first: Tributary pulls your personal measurements from
the apps and devices that hold them into **InfluxDB 1.x**, and ships
**Grafana dashboards** to look at them. Think
[garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana), for
many sources.

- **Connectors**, one per source: from its API on a schedule, from its
  export files (upload, or a watched folder, rebuilt from it when the folder
  is the source of truth), or both.
- **A web UI**: every integration with its schedule and last runs, *Run
  now*, *Dry run* (the exact line protocol, nothing written), *Full
  resync*, file upload, and the history of every run.
- **One shared schema** ([docs/schema.md](docs/schema.md)): a database per
  domain (`MediaStats`, `BodyStats`, `SleepStats`...), a `source` tag per
  tracker, fixed field types. Other tools write into the same databases
  ([compatible tools](docs/compatible-tools.md)), so one Grafana relates
  everything: reading in the evening and sleep, weight and activity...
- **Append-only and idempotent:** a point rewritten overwrites itself;
  nothing a source later forgets is dropped.
- **Private by default:** numbers and a few tags leave, titles as the only
  text (task names too, if you ask); secrets stay in the environment.

## Connectors

| Connector | Writes | Docs |
|---|---|---|
| Audiobookshelf | listening sessions (audiobooks, podcasts) and finished books → `MediaStats` | [docs/connectors/audiobookshelf.md](docs/connectors/audiobookshelf.md) |
| Renpho | smart-scale weigh-ins (weight, body fat, muscle, water...) of one or several people, rated with the app's levels → `BodyStats` | [docs/connectors/renpho.md](docs/connectors/renpho.md) |
| Todoist | completed tasks and every completion (recurring ones included), karma and streaks → `TodoistStats` | [docs/connectors/todoist.md](docs/connectors/todoist.md) |
| MyTherapy | the doses of a medication reminder (taken, missed, skipped), from the exports in a watched folder → `BodyStats` | [docs/connectors/mytherapy.md](docs/connectors/mytherapy.md) |

A connector is written only for a source no maintained tool covers: Garmin,
for instance, is [garmin-grafana](docs/compatible-tools.md)'s.
[Writing a connector](docs/connectors/README.md);
[porting a job that already feeds InfluxDB](docs/porting.md).

## Quick start

With Docker and Docker Compose:

```sh
git clone https://github.com/vchatela-org/tributary.git
cd tributary
cp .env.example .env    # then set the connectors you use, e.g. TRIBUTARY_ABS_URL and TRIBUTARY_ABS_TOKEN
docker compose up -d
```

This builds Tributary and starts it with InfluxDB 1.11 and Grafana:

- Tributary: <http://127.0.0.1:8000>. The first scheduled run of each
  configured connector comes a minute after the start; *Dry run* shows
  what it would write before that.
- Grafana: <http://127.0.0.1:3000> (first login `admin` / `admin`), with
  the InfluxDB datasources and the proposed dashboards of
  [grafana/](grafana/) in a *Tributary* folder.

Already running InfluxDB or Grafana? See [docs/deployment.md](docs/deployment.md).

## No login

Tributary has no accounts. Run it where only you can reach it (the compose
file binds it to `127.0.0.1`), or behind an authenticating reverse proxy
(Authentik, Authelia, oauth2-proxy...). Details in
[docs/deployment.md](docs/deployment.md), along with every setting and how
to run it on Kubernetes.

## Dashboards

[grafana/dashboards/](grafana/dashboards/) holds proposed dashboards,
provisioned by the compose file:

- **Reading & listening** (`MediaStats`): time per day and week by kind and
  source, devices, evening reading (by local hour), finished books.

They read the shared schema, not a connector: data written by another tool
following it (e.g. Kindle sessions from a GDPR-export tool) shows up in the
same panels.

## API

`/docs` has the OpenAPI description. In short: `GET /api/connectors`,
`GET /api/runs`, `GET /api/runs/{id}` (a dry run's line protocol),
`POST /api/connectors/{key}/run?dry_run=&full=`,
`POST /api/connectors/{key}/upload`, `GET /api/health`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
TRIBUTARY_DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

`python -m tests.fakes` serves a fake Audiobookshelf with a few sessions,
to try the whole chain without a real server (`TRIBUTARY_ABS_URL=http://127.0.0.1:13378`,
`TRIBUTARY_ABS_TOKEN=demo-key`).

Before pushing, `./scripts/privacy-scan.sh` checks that no personal data
(real identifiers, private addresses, internal hostnames) entered the tree.

## License

[MIT](LICENSE)
