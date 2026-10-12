# Audiobookshelf

[Audiobookshelf](https://www.audiobookshelf.org/) is a self-hosted
audiobook and podcast server. Tributary syncs one user's listening from its
API into `MediaStats` (see [the schema](../schema.md)). Checked against
Audiobookshelf 2.37.

## Setup

1. In Audiobookshelf, create an API key for the user whose listening you
   want: *Settings → API Keys → Add API Key* (an admin can create one for
   any user). Give it no expiry, or remember to renew it.
2. Set:

   | Variable | |
   |---|---|
   | `TRIBUTARY_ABS_URL` | The server's base URL, e.g. `http://audiobookshelf:80`. Reach it directly rather than through an authenticating proxy. |
   | `TRIBUTARY_ABS_TOKEN` | The API key. |
   | `TRIBUTARY_ABS_INTERVAL_MINUTES` | Minutes between two syncs (default 60). |
   | `TRIBUTARY_ABS_LOOKBACK_DAYS` | How far back each sync looks for sessions uploaded late by the mobile apps (default 30, see below). |

3. In Tributary, *Dry run* shows the line protocol of what the first sync
   would write; *Run now* writes it.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `reading_session` | listening session | its start | `kind` = `audiobook` or `podcast`, `device` | `minutes`, `sessions`, `title`, `local_hour`, `local_weekday` |
| `book_finished` | finished book | when finished | `kind` = `audiobook` | `count`, `title`, `local_hour`, `local_weekday` |

Every point also has `source=audiobookshelf`, `time_precision=second` and,
when set, `user`.

- `minutes` is `timeListening`, the time actually played: not the
  distance between the start and end positions, which a seek inflates.
  Sessions with nothing played are skipped.
- `kind` is `podcast` for an episode, `audiobook` otherwise.
- `device` is the device name the app reports, else its make and model
  (mobile apps), else browser and system (web player), else the app's name.
- `title` is the book's current title in the library, or the show's for a
  podcast episode (never the episode's). A book no longer in any library
  keeps the title its session recorded.
- A book is finished when its progress says so (never an episode), at
  `finishedAt`. Without one, at its last update when Tributary first sees
  it finished, and that time is kept from then on (the last update moves
  whenever the book is reopened, and would count it again). A finished
  book no longer in any library still counts, without a title.

## How it syncs

- `GET /api/me/listening-sessions` is paged, most recently updated first.
  A sync reads pages until one holds nothing updated within
  `TRIBUTARY_ABS_LOOKBACK_DAYS` of the newest update already synced:
  usually one or two pages. The look back is for sessions listened offline
  in the mobile apps: they are uploaded later, with the time they were last
  updated on the device, so they can sort behind sessions already synced.
  The ids of the sessions in that window are kept, so only a new or
  changed session is written (one still being listened to is rewritten
  with its new duration). Offline for longer than the window? Run a *Full
  resync*.
- `GET /api/me` gives the progress of each book. When a finished book is
  new or changed, all finished books are written again: books finished in
  the same second share a point, and rewriting only one would replace
  their summed count.
- Titles come from `POST /api/items/batch/get`, only for what is about to
  be written (or, if the server refuses it, from the libraries:
  `GET /api/libraries`, `GET /api/libraries/{id}/items?minified=1`).
- *Full resync* reads every session and finished book again and rewrites
  them: harmless, the same points overwrite themselves (it keeps the times
  chosen for books finished without a date).
- Nothing is ever dropped: a session deleted in Audiobookshelf stays in
  InfluxDB.

## Privacy

`GET /api/me` also returns the user's email and a legacy non-expiring
token, and every session's device info carries an IP address. Tributary
keeps none of it: each answer is reduced to the few values a point needs as
soon as it is read, the payloads are never stored, and the cursor holds
timestamps and opaque ids only (recent sessions, books finished without a
date). The API key lives in the environment only, never in a log, the
database or an API answer. Tests check all of this.
