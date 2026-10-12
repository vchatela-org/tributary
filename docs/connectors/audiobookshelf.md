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
  `finishedAt`, else its last update; a book without `finishedAt` is then
  written once only (its last update moves whenever it is reopened, and
  would count it again). A finished book no longer in any library still
  counts, without a title.

## How it syncs

- `GET /api/me/listening-sessions` is paged, most recently updated first.
  The cursor is the newest update seen: a sync reads pages until one brings
  nothing newer, usually a single page. A session updated since (still
  being listened to) is read again and its point rewritten with the new
  duration.
- `GET /api/me` gives the progress of each book; the libraries
  (`GET /api/libraries`, `GET /api/libraries/{id}/items?minified=1`) are
  read only when there is something to title.
- *Full resync* ignores the cursor and rewrites every session: harmless,
  the same points overwrite themselves.
- Nothing is ever dropped: a session deleted in Audiobookshelf stays in
  InfluxDB.

## Privacy

`GET /api/me` also returns the user's email and a legacy non-expiring
token, and every session's device info carries an IP address. Tributary
keeps none of it: each answer is reduced to the few values a point needs as
soon as it is read, the payloads are never stored, and the cursor is two
timestamps (plus the library ids of books finished without a date). The API key lives in the environment only, never in a log, the
database or an API answer. Tests check all of this.
