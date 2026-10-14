# Whoop

> **Built on [Whoops](https://github.com/kryoseu/whoops), by kryoseu.** Whoops
> imports one's own WHOOP data into a database, and this connector is mostly a
> port of it into Tributary: its WHOOP client (endpoints, scopes, token
> requests, paging), its field mapping and its `score_state` handling, and its
> export import (from its fork's CSV import, offered upstream as
> [kryoseu/whoops#5](https://github.com/kryoseu/whoops/pull/5)) were adapted
> rather than rewritten. Thank you, kryoseu. Those files keep Whoops' licence,
> the [Apache License 2.0](../../LICENSES/Apache-2.0.txt), and say so in their
> header; see [NOTICE](../../NOTICE).

WHOOP is a strap that scores sleep, recovery and strain. Tributary reads it two
ways, both filling the same `source=whoop` series:

- **the WHOOP developer API** (v2), daily: sleeps, recoveries, physiological
  cycles and workouts. You connect it once, in the browser, on the source's
  page (OAuth);
- **the app's data export**, uploaded in the UI: the **journal**, which the API
  doesn't have, the sleep need and debt, the workouts' heart-rate zones in
  percent and GPS flag, and any history from before the API was read.

Tributary names WHOOP only to say where the data comes from: it is not made,
endorsed or supported by WHOOP.

## Setup

### 1. Register your own WHOOP app

WHOOP's terms don't let an open-source project ship credentials, so every user
registers an app of their own, in the
[WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/) (a new app
starts on the Sandbox tier, enough for up to 10 members: a household). Use a
**new app for Tributary** rather than one another tool (Whoops) already uses:
each keeps its own grant.

- **Scopes**: `offline`, `read:recovery`, `read:cycles`, `read:sleep`,
  `read:workout`. Nothing else: Tributary never asks for `read:profile` (name,
  email) nor `read:body_measurement`. `offline` is what gives a refresh token,
  so a connection lasts.
- **Redirect URI**: `https://<the address you open Tributary at>/connectors/whoop/callback`,
  exactly. WHOOP never calls it: your browser is sent there after you grant
  access, so it works behind a login proxy and needs no public route (see
  [behind an authenticating proxy](#behind-an-authenticating-proxy)). Running
  Tributary at a plain `http://127.0.0.1:8000`? Register
  `https://localhost/connectors/whoop/callback` and use
  [the pasted-address fallback](#a-plain-http-install).

### 2. Set the environment

| Variable | |
|---|---|
| `TRIBUTARY_WHOOP_CLIENT_ID` | Your app's client id. |
| `TRIBUTARY_WHOOP_CLIENT_SECRET` | Your app's client secret. Never logged, stored or shown. |
| `TRIBUTARY_WHOOP_REDIRECT_URI` | The redirect URI registered with the app, character for character. |
| `TRIBUTARY_SECRET_KEY` | The key that encrypts the tokens Tributary keeps: the base64 of 32 random bytes, made with `openssl rand -base64 32`. Or `TRIBUTARY_SECRET_KEY_FILE`, a file holding it (a Docker or Kubernetes secret). **Required**: without it, Tributary refuses to connect rather than store a token in plain text. |
| `TRIBUTARY_SECRET_KEY_PREVIOUS` | Only while rotating the key: the old one, still read, never written (below). |
| `TRIBUTARY_WHOOP_LOOKBACK_DAYS` | Days read again before the newest record of the last run, for rescoring and edits made in the app (default 7). |
| `TRIBUTARY_WHOOP_SCHEDULE` | A 5-field cron expression, in `TRIBUTARY_TZ` (default: daily at 10:35, after the night is scored). Set it on the source's page instead, when you can: see [Scheduling](../scheduling.md). |

Keep the secret key with your other secrets, apart from Tributary's data
folder: a copy of the folder alone (a volume backup, a NAS copy) is then
useless, which is the point. Losing the key, or the data folder, only means
connecting Whoop again.

### 3. Connect

On Whoop's page in Tributary, **Connect Whoop** sends you to WHOOP's own page,
where you log in and grant access, then back to Tributary. The page then says
*Connected*, and the first run follows a minute later: it reads the **whole
history** (a few dozen requests for a year), then each daily run reads the last
week again. **Reconnect** grants access again (after a revocation, a lost key);
**Disconnect** revokes Tributary's access at WHOOP, then forgets the tokens.

### Behind an authenticating proxy

The code WHOOP gives comes back **through your browser**, which already holds
your proxy session (Authentik, Authelia, oauth2-proxy...): WHOOP never calls
Tributary, so nothing needs to be reachable from the internet. Tributary only
needs outbound HTTPS to `api.prod.whoop.com`.

One thing to check on the first connection: the browser arrives from WHOOP's
site, and browsers send `SameSite=Lax` cookies on such a top-level navigation,
but not `Strict` ones. If your proxy's session cookie is `Strict`, it asks you
to log in again; you must then land back on the callback address **with its
query string** (`?code=…&state=…`), within 10 minutes. If it drops it, use
[the pasted-address fallback](#a-plain-http-install).

The callback is the one page of Tributary that accepts a request coming from
another site (the others refuse it): the single-use `state` it was sent with,
10 minutes old at most, is what protects it. Its address, `code` included, is
redacted from Tributary's access log; your proxy may log it (the code is single
use, and worthless without the client secret).

### A plain http:// install

WHOOP wants an `https://` redirect URI. Without one (Docker on 127.0.0.1),
register `https://localhost/connectors/whoop/callback`, press *Connect Whoop*,
grant access: the browser then lands on that address, which doesn't load. Copy
the whole address from the address bar and paste it in *Back from Whoop, on a
page that doesn't load?* on Whoop's page, within 10 minutes.

### Rotating the secret key

Set the new key in `TRIBUTARY_SECRET_KEY` and the old one in
`TRIBUTARY_SECRET_KEY_PREVIOUS`, restart, and let one run go by (or open
Whoop's page and *Run now*): the tokens are encrypted again with the new key
the first time they are read. Then remove `TRIBUTARY_SECRET_KEY_PREVIOUS`.
Without the old key, nothing is lost but the connection: connect again.

## What it writes

Every point is tagged `source=whoop` and `user` (`TRIBUTARY_INFLUXDB_USER`);
the full contract is in [the schema](../schema.md).

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `SleepStats..sleep_session` | sleep (night or nap), with the recovery scored from it | the sleep's **end** | `kind` (`night`, `nap`) | `start`, `in_bed_s`, `awake_s`, `light_s`, `deep_s` (slow wave), `rem_s`, `asleep_s` (light + deep + REM), `no_data_s`, `cycles`, `awakenings`, `score` (sleep performance %), `respiration_rate`, `efficiency_pct`, `consistency_pct`, `need_baseline_s`, `need_debt_s`, `need_strain_s`, `need_nap_s`, `tz_offset_min`, `bedtime_min`, `wake_min`; the recovery's `recovery_score`, `resting_hr`, `hrv_rmssd_ms`, `avg_spo2_pct`, `skin_temp_c`, `calibrating`; from the export, `sleep_need_s`, `sleep_debt_s` |
| `BodyStats..daily_activity` | physiological cycle (sleep onset to the next) | its start | none | `strain`, `energy_kcal` (WHOOP's kJ / 4.184), `avg_hr`, `max_hr`, `steps`, `duration_s` (absent while open) |
| `BodyStats..exercise` (shared with MyFitnessPal) | workout | its start | `category` (`run`, `bike`, `strength`... as MyFitnessPal's), `sport` (WHOOP's own sport name) | `activity` (WHOOP's sport name), `duration_s`, `energy_kcal`, `strain`, `avg_hr`, `max_hr`, `distance_m`, `altitude_gain_m`, `altitude_change_m`, `percent_recorded`, `zone0_s` ... `zone5_s`; from the export, `zone1_pct` ... `zone5_pct`, `gps_enabled` |
| `SleepStats..journal` (export only) | journal answer | the start of the cycle it was answered for | `question` (WHOOP's wording) | `answered_yes` (0/1), `count` (1), and that cycle's `recovery_score`, `hrv_rmssd_ms` |

- **Scores.** A record WHOOP has scored carries its score. One still
  awaiting its score (`PENDING_SCORE`) is not written yet: the next run reads
  it again (the cursor doesn't move past it, unless it has waited for more
  than 30 days; a *Full resync* still reads it). One WHOOP could not score
  (`UNSCORABLE`) is written with what needs no score (its times, its sport,
  its steps), never a score field.
- **A missing value is no field**, never a 0 (an old cycle without steps has
  no `steps`).
- **Points don't move.** A sleep edited in the app moves its end; a workout
  edited moves its start or changes sport. Tributary pins each sleep, cycle
  and workout to the time, and the `kind` or `category`, it was first
  written with, so an edit rewrites the same point instead of leaving a
  second one behind; a *Full resync* keeps the pins.
- **The journal**: every question, the member's own custom ones included,
  as the `question` tag. **The notes** typed with an answer are never read.
- **Not written**: the profile (name, email) and the body measurements
  (height, weight, maximum heart rate), whose scopes are never asked for;
  WHOOP's ids (they only key the pins); the export's notes.

## How it syncs

- The first run, and a *Full resync*, read the whole history, 25 records a
  page. Later runs read each collection from its newest start minus the
  lookback (7 days), and rewrite it: the same points overwrite themselves.
- WHOOP allows 100 requests a minute and 10,000 a day. A `429` is waited out
  until the `X-RateLimit-Reset` it gives, when that is within 90 s; a longer
  one (the daily limit) stops the run, and the next one reads it again. Other
  server errors are retried three times. A redirect is refused: the token
  would follow it.
- **Tokens**: an access token lasts about an hour. A run refreshes it when it
  expires within a minute, and once more if WHOOP refuses it (`401`).
  WHOOP **rotates** refresh tokens and kills the old one when it issues the
  new one, so the new pair is stored the moment it arrives, before any data is
  read, in a dry run too, and kept even if the run fails afterwards. If WHOOP
  refuses the refresh token (`invalid_grant`: revoked, or used elsewhere),
  the run fails with *Reconnect Whoop on its page*.
- Nothing is ever dropped from the API's series: a record deleted in WHOOP
  stays in InfluxDB.

## The export

In the WHOOP app, *More → App Settings → Data Export*: WHOOP emails a link to a
zip within a day (it expires after 7 days). Upload the zip on Whoop's page
(*Check file*, then *Import for real*), or one of its CSV files
(`physiological_cycles.csv`, `sleeps.csv`, `workouts.csv`,
`journal_entries.csv`), up to 50 MB each. Upload the files as WHOOP made them:
one re-saved by a spreadsheet (other separators, other date formats) is refused.

- **Connect the API and let its first run finish first**: once connected, an
  import before that first run is refused. Each export row is then matched
  to the API's record whose start is within **60 seconds** of its own (the
  export is in local time to the second; the API in UTC to the millisecond;
  a start edited in the app is followed), and only adds what the API lacks,
  on the API's point: `sleep_need_s` and `sleep_debt_s` on a sleep,
  `zone1_pct` ... `zone5_pct` and `gps_enabled` on a workout. **An API value
  is never overwritten by an export value.**
- A row older than every record the API returned (history from before the
  API's reach) is written whole, at its own time. A newer row no API record
  matches is **left out**: it is the API's to write (a night newer than its
  last run), and written beside it, it would be a second point. Let the API
  run, then import again. Without the API (never connected), every row is
  written at its own time.
- **The journal** is rebuilt from each export: every export holds the whole
  history, so an import replaces this source's journal over the days it
  covers (an answer deleted in the app goes too). Nothing else is ever
  dropped. It needs `TRIBUTARY_INFLUXDB_USER`, like every import that
  replaces days.
- The check's message counts what was matched: *sleeps: 412 on the API's
  points, 0 from before the API's first record...*, and what was left out.
  On your first real export, read those counts before importing.

## Coming from Whoops

The first run re-reads the whole history from the API, so nothing needs
migrating from Whoops' PostgreSQL. To prove nothing is missing,
[`scripts/whoop-counts.py`](../../scripts/whoop-counts.py) compares the number
of records per day in Whoops' tables with the points per day in InfluxDB
(counts only, never a value):

```sh
python3 scripts/whoop-counts.py sql > counts.sql      # the query to run on Whoops' database
psql -At -F, -d whoop_data -f counts.sql > postgres-counts.csv
python3 scripts/whoop-counts.py compare postgres-counts.csv \
    --influx-url http://127.0.0.1:8086 --user <your TRIBUTARY_INFLUXDB_USER>
```

Days present in InfluxDB only are expected where Whoops never stored a record
(an unscored sleep, the open cycle of today); days present in PostgreSQL only
are the ones to look at (records the API no longer returns). The journal is
compared once an export was imported. Keep Whoops and its database until the
counts agree.

## Limits

- **Not verified against a real account yet** (to check, counts only, on the
  first connection):
  - that a run without `start` returns the account's whole history;
  - that old cycles carry `step_count` (`steps` is simply absent when not);
  - whether an unused refresh token expires (if so, a connection left without
    a run for that long needs reconnecting);
  - that the export's times are the API's truncated to the second (the
    60-second match tolerates the difference either way);
  - that WHOOP accepts the 16-character `state` (Whoops sends one this long,
    and it works).
- No heart-rate or step time series, no VO₂ max, no stress: the API has none.
- Webhooks are not used: a daily run is enough, and they would need a public
  route.
- An export taken after a sleep or a workout was edited in the app, but
  imported before the API's next run read that edit, doesn't match it: the
  row is left out (import again after the run).
- The cursor keeps a small pin per sleep, cycle and workout ever read (about
  70 bytes each): what keeps points from moving, even on a *Full resync*.
- Only one WHOOP account per Tributary (the `user` tag is
  `TRIBUTARY_INFLUXDB_USER`).
