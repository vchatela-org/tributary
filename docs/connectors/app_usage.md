# App Usage

[App Usage](https://play.google.com/store/apps/details?id=com.a0soft.gphone.uninstaller)
(a0soft) tracks how an Android phone is used: how long each app is in the
foreground, how often the phone is unlocked, how long its screen is on. It has
no API, but its backup is the app's own tables, so Tributary takes the backup
you import in its UI and writes the phone's screen time into `DigitalStats`
(see [the schema](../schema.md)).

An import **replaces** the connector's series over the days the backup covers:
an app renamed, or moved to another category in the app, does not leave its
old series behind. The days before the backup's first one are left as they
are, so a backup from after the app's retention dropped old days never empties
them.

## Setup

1. In App Usage, make a backup (by hand, or the app's daily backup): it
   writes `AUM_V4_<date>_<time>.zip`. Copy it off the phone.
2. Set `TRIBUTARY_INFLUXDB_USER`: an import replaces the days it covers of
   this user's points, so without one it is refused.
3. In Tributary, *Dry run* next to the file shows the line protocol of what
   the import would write; *Import file* writes it.

Each backup holds the whole history the app keeps, so importing every daily
backup or one a week gives the same series: a day the next backup still holds
is rewritten in place, never doubled. A day's totals (`screen_time`,
`app_usage`) are in the backups made after the day is over.

## What it writes

| Measurement | One point per | Time | Tags | Fields |
|---|---|---|---|---|
| `screen_time` | day | local midnight | | `unlocks`, `screen_on_s`, `app_s`, `launches`, `notifications` |
| `app_usage` | app and day | local midnight | `app`, `package`, `category` | `foreground_s`, `launches`, `notifications` |
| `app_session` | time an app was in the foreground | its start, *local time* | `app`, `package`, `category` | `duration_s`, `count` (1) |
| `unlock` | unlock | its time, *local time* | | `count` (1) |

- `screen_on_s` and `unlocks` are the app's own daily figures; `app_s` and
  `launches` are `app_usage` summed over the day's apps. The screen can be on
  with no app in the foreground (the lock screen, a call), so `screen_on_s`
  is usually the larger.
- `category` is the one you chose for the app in App Usage, else the app's own
  (`social`, `video`, `game`, `productivity`... `others` for none).
- `notifications` stays 0 unless App Usage's notification tracking is on.
- The launcher (your home screen) counts as an app: a panel that wants the
  time spent in apps leaves its `package` out.
- An unlock is a screen turned on *and* unlocked: the screen woken by a
  notification, or to glance at the time, and turned off again locked, is not
  written.
- `app_session` and `unlock` carry `local_hour` and `local_weekday`: when in
  the day (or night) the phone is used, next to the night's sleep in
  `SleepStats`.

## Privacy

The backup holds, besides the usage, the list of installed apps, their install
and update history, the battery's level, the places you set for the app's
"at home / at work" feature, and the apps' icons. Only the usage leaves: the
apps' names and packages and the categories' names (tags), and numbers.
Installs, battery, places and icons are not read.

Nothing in the backup is a message, a title or a URL: App Usage records which
app was open and for how long, not what it showed.
