# Portions adapted from kryoseu/whoops (https://github.com/kryoseu/whoops),
# Apache License 2.0; modified. See NOTICE and LICENSES/Apache-2.0.txt.
"""WHOOP (a strap): sleeps with their recovery, physiological cycles and
workouts from the WHOOP developer API v2 (OAuth: connected once on the
source's page), and the journal -- plus what the API lacks -- from the app's
data export, uploaded in the UI. Into SleepStats and BodyStats.

Built on kryoseu's Whoops (https://github.com/kryoseu/whoops, Apache-2.0),
which this replaces for Tributary's users: its field mapping (the models'
`from_json`, the API's JSON paths and units), its `score_state` handling
(`score` absent unless SCORED, `data.get("score") or {}`), its WHOOP client
(app/connectors/whoop_api.py) and its fork's export parser
(app/connectors/whoop_export.py). Thank you, kryoseu.

API (whoop_api.py; scopes `offline read:recovery read:cycles read:sleep
read:workout`, never the profile nor the body measurements):
  sleep_session  SleepStats. One point per sleep, at its **end** (the wake),
                 tag kind = night / nap. The contract's fields (start, in_bed_s,
                 awake_s, light_s, deep_s = slow wave, rem_s, asleep_s = light
                 + deep + REM, cycles, awakenings, score = sleep performance %,
                 respiration_rate, tz_offset_min, bedtime_min, wake_min) and
                 WHOOP's (efficiency_pct, consistency_pct, no_data_s,
                 need_baseline_s, need_debt_s, need_strain_s, need_nap_s).
                 The recovery scored from that sleep (its `sleep_id`) lands on
                 the same point: recovery_score, resting_hr, hrv_rmssd_ms,
                 avg_spo2_pct, skin_temp_c, calibrating (0/1).
  daily_activity BodyStats. One point per cycle, at its start: strain,
                 energy_kcal (kJ / 4.184), avg_hr, max_hr, steps
                 (`step_count`), duration_s (absent while the cycle is open).
  exercise       BodyStats, shared with MyFitnessPal. One point per workout,
                 at its start, tag category (MyFitnessPal's words: run,
                 bike, strength...): activity (WHOOP's sport name), duration_s,
                 energy_kcal, strain, avg_hr, max_hr, distance_m,
                 altitude_gain_m, altitude_change_m, percent_recorded,
                 zone0_s ... zone5_s (seconds per heart-rate zone).
Rules:
  - score_state: a SCORED record carries its score; PENDING_SCORE is not
    written yet -- the cursor stays before it, and a later run writes it once
    scored; UNSCORABLE writes what needs no score (times, steps, the sport),
    never a score field.
  - a missing value is no field, never a 0.
  - times must not move: a sleep edited in the app moves its end, which would
    leave a second point behind. The cursor pins each sleep, cycle and
    workout to the time -- and the `kind` or `category` tag -- it was first
    written with ({id: [time, start, tag]}, the start following edits), and
    a full resync keeps them.

Cursor: per resource, the newest start read (never past a record still
awaiting its score, unless it has waited over 30 days). A run reads from
there minus TRIBUTARY_WHOOP_LOOKBACK_DAYS (default 7: rescoring and edits),
25 a page, and rewrites it; the same keys overwrite themselves. A first run
or a *Full resync* reads the whole history (no `start`).

Export (whoop_export.py: the zip, or one of its CSV files):
  - every row is matched to the API record whose start is within 60 s (the
    pins above). Matched, it adds only what the API lacks, on the API's point
    (same time, same tags): sleep_need_s, sleep_debt_s on a sleep;
    zone1_pct ... zone5_pct and gps_enabled on a workout. An API field is
    never overwritten by an export value. A row older than every record the
    API returned (history before its reach) is written whole at its own
    time; a newer unmatched one is left out: the API's to write, at its own
    time. Once the API is connected, an import waits for its first run.
  - journal (SleepStats, export only): one point per answer, at 12:00 local
    on the day it is about (the day before the morning it was given: the
    cycle's start, its bedtime, minus 12 hours), tag question =
    WHOOP's wording, custom questions included; fields answered_yes (0/1),
    count (1), and the cycle's recovery, sleep and strain (recovery_score,
    hrv_rmssd_ms, asleep_s, strain — so a panel can compare them on yes-days
    against no-days without a join).
    Notes are never read. An answer the export gives no cycle at all (some
    custom questions are answered outside a journal) has no date: it is
    counted and left out. Every export holds the whole
    history, so an import replaces this source's journal over the days it
    covers (FileImport.spans); nothing else is ever dropped.

Tokens are app/oauth.py's: encrypted at rest, refreshed a minute before they
expire or once after a 401, never logged, returned or shown.
"""
import bisect
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from app import oauth, secretbox
from app.__version__ import __version__
from app.config import settings as app_settings
from app.connectors import whoop_api, whoop_export
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport, Span, SyncResult
from app.points import Measurement, Point, epoch_seconds
from app.schema import DAILY_ACTIVITY, EXERCISE, SLEEP_SESSION

PREFIX = "TRIBUTARY_WHOOP_"
SOURCE = "whoop"
KJ_PER_KCAL = 4.184
# An export row and an API record are the same when their starts are this
# close (the export is to the second, in local time; the API to the
# millisecond, in UTC).
MATCH_SECONDS = 60
# A journal answer is about the day before the morning it is given, and WHOOP
# keys it to the cycle that starts with that night's sleep: the day is the
# local date of that bedtime minus this, so 23:10 and 00:22 both name the
# evening's day.
JOURNAL_DAY_SHIFT = timedelta(hours=12)
JOURNAL_HOUR = time(12)
# A record awaiting its score holds the cursor back (the next run reads it
# again), but not one older than this: WHOOP left it unscored for good, and
# re-reading from it every day would only grow the window.
PENDING_HOLD = timedelta(days=30)
# Where WHOOP sends the browser back (app/routers/connections.py): the redirect
# URI to register is https://<Tributary's host> followed by this.
CALLBACK_PATH = "/connectors/whoop/callback"

JOURNAL = Measurement(
    database="SleepStats", name="journal", tags=("question",),
    fields={"answered_yes": int, "count": int, "recovery_score": float, "hrv_rmssd_ms": float,
            "asleep_s": int, "strain": float},
)

# WHOOP's sport name -> MyFitnessPal's category words, first match wins (the
# name lower-cased, "-" and "_" read as spaces). Anything else is `other`.
CATEGORIES = (
    ("elliptical", "elliptical"),
    ("cycling", "bike"), ("biking", "bike"), ("spin", "bike"), ("bike", "bike"),
    ("running", "run"),
    ("weightlifting", "strength"), ("powerlifting", "strength"), ("strength", "strength"),
    ("functional fitness", "strength"), ("crossfit", "strength"),
    ("walking", "walk"),
    ("hiking", "hike"), ("rucking", "hike"),
    ("swimming", "swim"),
    ("climbing", "climb"),
    ("rowing", "paddle"), ("kayaking", "paddle"), ("canoeing", "paddle"), ("paddle", "paddle"),
    ("rafting", "paddle"),
    ("hiit", "aerobic"), ("aerobic", "aerobic"), ("dance", "aerobic"), ("zumba", "aerobic"),
    ("cardio", "aerobic"),
)

# What the export adds to a point the API already wrote (rule: an API field is
# never overwritten by an export value).
SLEEP_EXPORT_ONLY = ("sleep_need_s", "sleep_debt_s")
WORKOUT_EXPORT_ONLY = tuple(f"zone{n}_pct" for n in range(1, 6)) + ("gps_enabled",)


class WhoopSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # Your WHOOP app's (developer-dashboard.whoop.com): every user registers
    # their own, and none ships with Tributary.
    client_id: Optional[str] = None
    client_secret: Optional[SecretStr] = None
    # The redirect URI registered with the app: Tributary's
    # https://<host>/connectors/whoop/callback, or, for a plain http:// local
    # install, an https:// address whose page you paste back (docs).
    redirect_uri: Optional[str] = None
    # Days read again before the newest record of the last run (rescoring,
    # edits made in the app).
    lookback_days: int = 7


# ────────────────────────────────────────────────
# Mapping, adapted from Whoops' models (from_json)
# ────────────────────────────────────────────────

def parse_time(raw: Any) -> Optional[datetime]:
    """A WHOOP time ("2026-10-02T06:15:30.120Z"): aware UTC, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


# A point's second, as the core keys it.
epoch = epoch_seconds


def offset_minutes(raw: Any) -> Optional[int]:
    """A record's `timezone_offset` ("+01:00", "-05:30"), in minutes."""
    if not isinstance(raw, str) or len(raw) < 6 or raw[0] not in "+-" or raw[3] != ":":
        return None
    try:
        minutes = int(raw[1:3]) * 60 + int(raw[4:6])
    except ValueError:
        return None
    return -minutes if raw[0] == "-" else minutes


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _seconds(milli: Any) -> Optional[int]:
    value = _number(milli)
    return None if value is None else round(value / 1000)


def _kcal(kilojoule: Any) -> Optional[float]:
    value = _number(kilojoule)
    return None if value is None else round(value / KJ_PER_KCAL, 1)


def _put(fields: Dict[str, Any], name: str, value: Any, kind: type = float) -> None:
    """A field when the value is there: never a 0 standing for "missing"."""
    if value is None:
        return
    if kind is int:
        number = _number(value)
        if number is not None:
            fields[name] = int(round(number))
    elif kind is float:
        number = _number(value)
        if number is not None:
            fields[name] = number
    else:
        fields[name] = value


def scored(record: Dict) -> Dict:
    """The record's score, when WHOOP scored it ("Only present if the score
    state is SCORED"); {} otherwise (Whoops: `data.get("score") or {}`)."""
    score = record.get("score")
    return score if record.get("score_state") == "SCORED" and isinstance(score, dict) else {}


def pending(record: Dict) -> bool:
    return record.get("score_state") == "PENDING_SCORE"


def local_fields(start: datetime, end: Optional[datetime], offset: Optional[int]) -> Dict[str, int]:
    """tz_offset_min, and the contract's bedtime_min (local start, signed
    minutes from midnight: 23:30 = -30, 00:45 = 45) and wake_min (local end,
    minutes after midnight), in the record's own offset."""
    if offset is None:
        return {}
    zone = timezone(timedelta(minutes=offset))
    begin = start.astimezone(zone)
    minutes = begin.hour * 60 + begin.minute
    fields = {"tz_offset_min": offset, "bedtime_min": minutes - 1440 if minutes >= 12 * 60 else minutes}
    if end is not None:
        wake = end.astimezone(zone)
        fields["wake_min"] = wake.hour * 60 + wake.minute
    return fields


def sleep_fields(sleep: Dict) -> Dict[str, Any]:
    """A sleep's fields (Whoops' `WhoopSleep.from_json`): its times always,
    its stages, performance and need when scored."""
    start, end = parse_time(sleep.get("start")), parse_time(sleep.get("end"))
    fields: Dict[str, Any] = {"start": epoch(start)}
    fields.update(local_fields(start, end, offset_minutes(sleep.get("timezone_offset"))))
    score = scored(sleep)
    stage = score.get("stage_summary") or {}
    need = score.get("sleep_needed") or {}
    for name, key in (("in_bed_s", "total_in_bed_time_milli"), ("awake_s", "total_awake_time_milli"),
                      ("no_data_s", "total_no_data_time_milli"), ("light_s", "total_light_sleep_time_milli"),
                      ("deep_s", "total_slow_wave_sleep_time_milli"), ("rem_s", "total_rem_sleep_time_milli")):
        _put(fields, name, _seconds(stage.get(key)), int)
    stages = [_number(stage.get(key)) for key in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                                                  "total_rem_sleep_time_milli")]
    if all(value is not None for value in stages):
        fields["asleep_s"] = round(sum(stages) / 1000)
    _put(fields, "cycles", stage.get("sleep_cycle_count"), int)
    _put(fields, "awakenings", stage.get("disturbance_count"), int)
    _put(fields, "score", score.get("sleep_performance_percentage"))
    _put(fields, "respiration_rate", score.get("respiratory_rate"))
    _put(fields, "efficiency_pct", score.get("sleep_efficiency_percentage"))
    _put(fields, "consistency_pct", score.get("sleep_consistency_percentage"))
    for name, key in (("need_baseline_s", "baseline_milli"), ("need_debt_s", "need_from_sleep_debt_milli"),
                      ("need_strain_s", "need_from_recent_strain_milli"),
                      ("need_nap_s", "need_from_recent_nap_milli")):
        _put(fields, name, _seconds(need.get(key)), int)
    return fields


def recovery_fields(recovery: Dict) -> Dict[str, Any]:
    """A recovery's fields (Whoops' `WhoopRecovery.from_json`), scored only."""
    score = scored(recovery)
    fields: Dict[str, Any] = {}
    _put(fields, "recovery_score", score.get("recovery_score"))
    _put(fields, "resting_hr", score.get("resting_heart_rate"))
    _put(fields, "hrv_rmssd_ms", score.get("hrv_rmssd_milli"))
    _put(fields, "avg_spo2_pct", score.get("spo2_percentage"))
    _put(fields, "skin_temp_c", score.get("skin_temp_celsius"))
    if isinstance(score.get("user_calibrating"), bool):
        fields["calibrating"] = int(score["user_calibrating"])
    return fields


def cycle_fields(cycle: Dict) -> Dict[str, Any]:
    """A cycle's fields (Whoops' `WhoopCycle.from_json`, plus the steps it
    left out): steps and duration always, strain, energy and heart rate when
    scored."""
    start, end = parse_time(cycle.get("start")), parse_time(cycle.get("end"))
    score = scored(cycle)
    fields: Dict[str, Any] = {}
    steps = cycle.get("step_count")
    _put(fields, "steps", steps if steps is not None else (cycle.get("score") or {}).get("step_count"), int)
    if end is not None and end > start:
        fields["duration_s"] = epoch(end) - epoch(start)
    _put(fields, "strain", score.get("strain"))
    _put(fields, "energy_kcal", _kcal(score.get("kilojoule")))
    _put(fields, "avg_hr", score.get("average_heart_rate"))
    _put(fields, "max_hr", score.get("max_heart_rate"))
    return fields


def category(sport: Any) -> str:
    """MyFitnessPal's category word for a WHOOP sport name."""
    name = " ".join(str(sport or "").lower().replace("-", " ").replace("_", " ").split())
    return next((word for key, word in CATEGORIES if key in name), "other")


def sport_name(raw: Any) -> Optional[str]:
    """WHOOP's sport name as the `sport` tag: trimmed and bounded, or None."""
    if not isinstance(raw, str):
        return None
    return raw.strip()[:64] or None


def workout_tags(pin: list, sport: Optional[str]) -> Dict[str, Optional[str]]:
    """A workout point's tags: the shared `category`, plus WHOOP's own `sport`
    (pinned like `category`, so editing the sport in the app does not move the
    point). A pin written before the tag existed (3 elements) is extended."""
    if len(pin) <= 3:
        pin.append(sport)
    tags: Dict[str, Optional[str]] = {"category": pin[2]}
    if pin[3]:
        tags["sport"] = pin[3]
    return tags


def workout_fields(workout: Dict) -> Dict[str, Any]:
    """A workout's fields (Whoops' `WhoopWorkout.from_json`): the sport and
    duration always, the score's when scored."""
    start, end = parse_time(workout.get("start")), parse_time(workout.get("end"))
    score = scored(workout)
    zones = score.get("zone_durations") or {}
    fields: Dict[str, Any] = {}
    sport = workout.get("sport_name")
    if isinstance(sport, str) and sport.strip():
        fields["activity"] = sport.strip()[:64]
    if end is not None and end > start:
        fields["duration_s"] = epoch(end) - epoch(start)
    _put(fields, "energy_kcal", _kcal(score.get("kilojoule")))
    _put(fields, "strain", score.get("strain"))
    _put(fields, "avg_hr", score.get("average_heart_rate"))
    _put(fields, "max_hr", score.get("max_heart_rate"))
    _put(fields, "percent_recorded", score.get("percent_recorded"))
    _put(fields, "distance_m", score.get("distance_meter"))
    _put(fields, "altitude_gain_m", score.get("altitude_gain_meter"))
    _put(fields, "altitude_change_m", score.get("altitude_change_meter"))
    for n, word in enumerate(("zero", "one", "two", "three", "four", "five")):
        _put(fields, f"zone{n}_s", _seconds(zones.get(f"zone_{word}_milli")), int)
    return fields


def journal_day(cycle_start: datetime, zone: Any) -> date:
    """The day a journal answer is about, from the start (the bedtime) of the
    cycle WHOOP keys it to, read in the cycle's own time zone."""
    return (cycle_start.astimezone(zone) - JOURNAL_DAY_SHIFT).date()


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc)


def _now() -> datetime:
    """Now (tests fix it)."""
    return datetime.now(timezone.utc)


def collapse(points: List[Point]) -> List[Point]:
    """One point per key (measurement, time, tags), the later one's fields
    winning: the core would sum two points sharing a key."""
    merged: Dict[Tuple, Point] = {}
    for point in points:
        key = (point.measurement.name, epoch(point.time), tuple(sorted(point.tags.items())))
        if key in merged:
            merged[key].fields.update(point.fields)
        else:
            merged[key] = Point(point.measurement, point.time, dict(point.tags), dict(point.fields))
    return list(merged.values())


# ────────────────────────────────────────────────
# The cursor: where each resource stopped, and the pins
# ────────────────────────────────────────────────

RESOURCES = ("sleep", "cycle", "workout")


def _state(cursor: Any) -> Dict[str, Any]:
    cursor = cursor if isinstance(cursor, dict) else {}
    since = cursor.get("since") if isinstance(cursor.get("since"), dict) else {}
    pinned = cursor.get("pinned") if isinstance(cursor.get("pinned"), dict) else {}
    return {"since": {r: since[r] for r in RESOURCES if isinstance(since.get(r), int)},
            "pinned": {r: dict(pinned.get(r) or {}) for r in RESOURCES}}


def _pin(pinned: Dict[str, list], key: str, at: int, start: int, *tag: Any) -> list:
    """[time, start, (tag)]: the time and tag a record was first written with,
    kept; its start, the latest WHOOP gave (what an export row is matched on,
    an edit included)."""
    pin = pinned.setdefault(key, [at, start, *tag])
    pin[1] = start
    return pin


def api_points(sleeps: List[Dict], recoveries: List[Dict], cycles: List[Dict], workouts: List[Dict],
               state: Dict[str, Any], now: Optional[datetime] = None
               ) -> Tuple[List[Point], Dict[str, Any], Dict[str, int]]:
    """(points, the new cursor, counts) for what a run read."""
    pinned = state["pinned"]
    since = dict(state["since"])
    counts = {"sleep": 0, "nap": 0, "recovery": 0, "cycle": 0, "workout": 0, "pending": 0}
    points: List[Point] = []
    waiting: Dict[str, List[int]] = {r: [] for r in RESOURCES}
    newest: Dict[str, List[int]] = {r: [] for r in RESOURCES}
    hold_from = epoch((now or _now()) - PENDING_HOLD)

    def wait_for(resource: str, start: datetime) -> None:
        if epoch(start) >= hold_from:
            waiting[resource].append(epoch(start))

    def unique(records: List[Dict]) -> List[Dict]:
        by_id = {}
        for record in records:
            if record.get("id") is not None and parse_time(record.get("start")) is not None:
                by_id[str(record["id"])] = record
        return list(by_id.values())

    recovery_of = {str(r["sleep_id"]): r for r in recoveries if r.get("sleep_id") and recovery_fields(r)}
    for sleep in unique(sleeps):
        key, start, end = str(sleep["id"]), parse_time(sleep["start"]), parse_time(sleep.get("end"))
        newest["sleep"].append(epoch(start))
        if pending(sleep) or end is None:
            wait_for("sleep", start)
            counts["pending"] += 1
            continue
        pin = _pin(pinned["sleep"], key, epoch(end), epoch(start), int(bool(sleep.get("nap"))))
        fields = sleep_fields(sleep)
        recovery = recovery_of.pop(key, None)
        if recovery is not None:
            fields.update(recovery_fields(recovery))
            counts["recovery"] += 1
        counts["nap" if pin[2] else "sleep"] += 1
        points.append(Point(SLEEP_SESSION, _at(pin[0]), {"kind": "nap" if pin[2] else "night"}, fields))
    # A recovery scored after its sleep was read (the sleep outside this run's
    # window): on the point that sleep was written at.
    for key, recovery in recovery_of.items():
        pin = pinned["sleep"].get(key)
        if pin is not None:
            points.append(Point(SLEEP_SESSION, _at(pin[0]), {"kind": "nap" if pin[2] else "night"},
                                recovery_fields(recovery)))
            counts["recovery"] += 1

    for cycle in unique(cycles):
        key, start = str(cycle["id"]), parse_time(cycle["start"])
        newest["cycle"].append(epoch(start))
        if pending(cycle) or parse_time(cycle.get("end")) is None:
            # Not final yet: read again by the next runs.
            wait_for("cycle", start)
        if pending(cycle):
            counts["pending"] += 1
            continue
        pin = _pin(pinned["cycle"], key, epoch(start), epoch(start))
        fields = cycle_fields(cycle)
        if fields:
            counts["cycle"] += 1
            points.append(Point(DAILY_ACTIVITY, _at(pin[0]), {}, fields))

    for workout in unique(workouts):
        key, start = str(workout["id"]), parse_time(workout["start"])
        newest["workout"].append(epoch(start))
        if pending(workout):
            wait_for("workout", start)
            counts["pending"] += 1
            continue
        pin = _pin(pinned["workout"], key, epoch(start), epoch(start),
                   category(workout.get("sport_name")), sport_name(workout.get("sport_name")))
        counts["workout"] += 1
        points.append(Point(EXERCISE, _at(pin[0]),
                            workout_tags(pin, sport_name(workout.get("sport_name"))),
                            workout_fields(workout)))

    for resource in RESOURCES:
        if newest[resource]:
            since[resource] = max([since.get(resource, 0), *newest[resource]])
        if waiting[resource]:
            since[resource] = min(since[resource], *waiting[resource])
    return collapse(points), {"since": since, "pinned": pinned}, counts


# ────────────────────────────────────────────────
# The export, merged onto the API's points
# ────────────────────────────────────────────────

class Matcher:
    """The API records the runs pinned, by their latest start: an export row
    is the record whose start is within MATCH_SECONDS of its own."""

    def __init__(self, pins: Dict[str, Dict[str, list]]):
        self._sorted: Dict[str, List[Tuple[int, list]]] = {}
        for resource in RESOURCES:
            rows = [(pin[1], pin) for pin in (pins.get(resource) or {}).values()
                    if isinstance(pin, list) and len(pin) >= 3 - (resource == "cycle")]
            self._sorted[resource] = sorted(rows, key=lambda row: row[0])

    def match(self, resource: str, start: datetime) -> Optional[list]:
        rows = self._sorted.get(resource) or []
        at = epoch(start)
        index = bisect.bisect_left(rows, at, key=lambda row: row[0])
        near = [rows[i] for i in (index - 1, index) if 0 <= i < len(rows)]
        near = [row for row in near if abs(row[0] - at) <= MATCH_SECONDS]
        return min(near, key=lambda row: abs(row[0] - at))[1] if near else None

    def before_the_api(self, resource: str, start: datetime) -> bool:
        """Whether a row is older than every record the API returned: history
        the API never had, which only the export can write. A row the API
        should have (newer: the API's next run brings it) is left out instead
        of written at a time the API's point may not share."""
        rows = self._sorted.get(resource) or []
        return not rows or epoch(start) < rows[0][0] - MATCH_SECONDS


def _offset(row: Dict) -> int:
    return int(row["tz"].utcoffset(None).total_seconds() // 60)


def _export_sleep_fields(row: Dict) -> Dict[str, Any]:
    """Every field an export sleep row carries (sleeps.csv, or the main
    sleep's columns of physiological_cycles.csv)."""
    fields: Dict[str, Any] = {"start": epoch(row["sleep_onset"])}
    fields.update(local_fields(row["sleep_onset"], row.get("wake_onset"), _offset(row)))
    for name, attr in (("in_bed_s", "total_in_bed_ms"), ("awake_s", "total_awake_ms"),
                       ("light_s", "total_light_sleep_ms"), ("deep_s", "total_slow_wave_sleep_ms"),
                       ("rem_s", "total_rem_sleep_ms"), ("asleep_s", "asleep_ms"),
                       ("sleep_need_s", "sleep_need_ms"), ("sleep_debt_s", "sleep_debt_ms")):
        _put(fields, name, _seconds(row.get(attr)), int)
    _put(fields, "score", row.get("performance_perc"))
    _put(fields, "respiration_rate", row.get("respiratory_rate"))
    _put(fields, "efficiency_pct", row.get("efficiency_perc"))
    _put(fields, "consistency_pct", row.get("consistency_perc"))
    return fields


def _export_recovery_fields(row: Dict) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    _put(fields, "recovery_score", row.get("recovery_score"))
    _put(fields, "resting_hr", row.get("resting_heart_rate"))
    _put(fields, "hrv_rmssd_ms", row.get("hrv_rmssd_ms"))
    _put(fields, "skin_temp_c", row.get("skin_temp_celsius"))
    _put(fields, "avg_spo2_pct", row.get("spo2_perc"))
    return fields


def _export_workout_fields(row: Dict) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    sport = row.get("sport_name")
    if isinstance(sport, str) and sport.strip():
        fields["activity"] = sport.strip()[:64]
    _put(fields, "duration_s", _seconds(row.get("duration_ms")), int)
    _put(fields, "strain", row.get("strain"))
    _put(fields, "energy_kcal", row.get("energy_kcal"))
    _put(fields, "avg_hr", row.get("avg_heart_rate"))
    _put(fields, "max_hr", row.get("max_heart_rate"))
    for n, word in enumerate(("one", "two", "three", "four", "five"), start=1):
        _put(fields, f"zone{n}_pct", row.get(f"zone_{word}_perc"))
    if isinstance(row.get("gps_enabled"), bool):
        fields["gps_enabled"] = int(row["gps_enabled"])
    return fields


def export_points(tables: Dict[str, List[Dict]], pins: Dict[str, Dict[str, list]], tz: ZoneInfo
                  ) -> Tuple[List[Point], Optional[Span], List[str]]:
    """(points, the days the journal covers, the summary) of an export, merged
    onto the API's points."""
    matcher = Matcher(pins)
    points: List[Point] = []
    # Per resource: on the API's points, written whole (before the API's
    # first record), left out (the API's own to write).
    told = {"sleep": [0, 0, 0], "cycle": [0, 0, 0], "workout": [0, 0, 0]}

    # Sleeps: sleeps.csv, and the main sleep of each cycle (its recovery, and
    # the sleep itself when sleeps.csv isn't in the upload).
    sleeps: Dict[int, Dict[str, Any]] = {}
    for row in tables.get("sleeps", []):
        sleeps[epoch(row["sleep_onset"])] = {"row": row, "nap": bool(row.get("nap")), "recovery": {}}
    onsets = sorted(sleeps)
    for row in tables.get("cycles", []):
        if row.get("sleep_onset") is None:
            continue
        at = epoch(row["sleep_onset"])
        index = bisect.bisect_left(onsets, at)
        near = [onsets[i] for i in (index - 1, index) if 0 <= i < len(onsets) and abs(onsets[i] - at) <= MATCH_SECONDS]
        key = min(near, key=lambda onset: abs(onset - at)) if near else at
        entry = sleeps.setdefault(key, {"row": row, "nap": False, "recovery": {}})
        entry["recovery"] = _export_recovery_fields(row)
    for entry in sleeps.values():
        row = entry["row"]
        pin = matcher.match("sleep", row["sleep_onset"])
        if pin is not None:
            told["sleep"][0] += 1
            fields = {name: value for name, value in _export_sleep_fields(row).items() if name in SLEEP_EXPORT_ONLY}
            points.append(Point(SLEEP_SESSION, _at(pin[0]), {"kind": "nap" if pin[2] else "night"}, fields))
        elif not matcher.before_the_api("sleep", row["sleep_onset"]):
            told["sleep"][2] += 1
        elif row.get("wake_onset") is not None:
            told["sleep"][1] += 1
            fields = {**_export_sleep_fields(row), **entry["recovery"]}
            points.append(Point(SLEEP_SESSION, row["wake_onset"], {"kind": "nap" if entry["nap"] else "night"},
                                fields))

    # Cycles: the API has every field the export does; the export's own only
    # for the days before the API's first one.
    for row in tables.get("cycles", []):
        if matcher.match("cycle", row["cycle_start"]) is not None:
            told["cycle"][0] += 1
            continue
        if not matcher.before_the_api("cycle", row["cycle_start"]):
            told["cycle"][2] += 1
            continue
        fields: Dict[str, Any] = {}
        _put(fields, "strain", row.get("strain"))
        _put(fields, "energy_kcal", row.get("energy_kcal"))
        _put(fields, "avg_hr", row.get("avg_heart_rate"))
        _put(fields, "max_hr", row.get("max_heart_rate"))
        if row.get("cycle_end") is not None and row["cycle_end"] > row["cycle_start"]:
            fields["duration_s"] = epoch(row["cycle_end"]) - epoch(row["cycle_start"])
        if fields:
            told["cycle"][1] += 1
            points.append(Point(DAILY_ACTIVITY, row["cycle_start"], {}, fields))

    for row in tables.get("workouts", []):
        pin = matcher.match("workout", row["workout_start"])
        if pin is not None:
            told["workout"][0] += 1
            fields = {name: value for name, value in _export_workout_fields(row).items()
                      if name in WORKOUT_EXPORT_ONLY}
            points.append(Point(EXERCISE, _at(pin[0]),
                                workout_tags(pin, sport_name(row.get("sport_name"))), fields))
        elif not matcher.before_the_api("workout", row["workout_start"]):
            told["workout"][2] += 1
        else:
            told["workout"][1] += 1
            points.append(Point(EXERCISE, row["workout_start"],
                                {"category": category(row.get("sport_name")), "sport": sport_name(row.get("sport_name"))},
                                _export_workout_fields(row)))

    # The journal is keyed by cycle: carry that cycle's recovery on the answer, so
    # a panel can compare recovery, sleep and strain on days a question was
    # answered yes vs no without joining two measurements (InfluxQL cannot).
    cycles_by_start = {row["cycle_start"]: row for row in tables.get("cycles", [])}
    journal: List[Point] = []
    days: List[date] = []
    for row in tables.get("journal", []):
        question = " ".join(str(row.get("question") or "").split())
        if not question:
            continue
        day = journal_day(row["cycle_start"], row.get("tz") or tz)
        # Earlier versions stamped an answer at its cycle's start: the span
        # covers that day too, so an import replaces those points as well.
        days += [day, row["cycle_start"].astimezone(tz).date()]
        fields: Dict[str, Any] = {"count": 1}
        if isinstance(row.get("answered_yes"), bool):
            fields["answered_yes"] = int(row["answered_yes"])
        cycle = cycles_by_start.get(row["cycle_start"])
        if cycle is not None:
            _put(fields, "recovery_score", cycle.get("recovery_score"))
            _put(fields, "hrv_rmssd_ms", cycle.get("hrv_rmssd_ms"))
            _put(fields, "asleep_s", _seconds(cycle.get("asleep_ms")), int)
            _put(fields, "strain", cycle.get("strain"))
        journal.append(Point(JOURNAL, datetime.combine(day, JOURNAL_HOUR, tzinfo=tz),
                             {"question": question[:200]}, fields))
    journal = collapse(journal)
    span = Span(min(days), max(days)) if journal else None

    summary = [f"{what}: {matched} on the API's points, {alone} from before the API's first record"
               + (f", {left} left out (the API's to write: let it run, then import again)" if left else "")
               for what, (matched, alone, left) in (("sleeps", told["sleep"]), ("cycles", told["cycle"]),
                                                    ("workouts", told["workout"])) if matched or alone or left]
    if journal:
        summary.append(f"{len(journal)} journal answer(s)")
    return collapse(points) + journal, span, summary


# ────────────────────────────────────────────────
# Connector
# ────────────────────────────────────────────────

class WhoopConnector(Connector):
    key = SOURCE
    label = "Whoop"
    description = ("Sleep, recovery, cycles and workouts from the WHOOP API (connect it on this page), and the "
                   "journal from the app's data export. Built on kryoseu's Whoops.")
    icon = "bi-heart-pulse"
    category = "Sleep"
    # After the night is scored.
    default_schedule = "35 10 * * *"
    settings_class = WhoopSettings
    measurements = (SLEEP_SESSION, JOURNAL, DAILY_ACTIVITY, EXERCISE)
    required_env = (f"{PREFIX}CLIENT_ID", f"{PREFIX}CLIENT_SECRET", f"{PREFIX}REDIRECT_URI", secretbox.KEY_ENV)
    file_types = (".zip", ".csv")
    # Only the journal is rebuilt from an export (FileImport.spans): the API's
    # series are never dropped.
    rebuild_own_source = True
    uses_oauth = True

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: a watched folder's rebuild would drop every series of
        # this source, the API's included.
        return None

    # -- OAuth (app/oauth.py) ------------------------------------------------

    def _secret(self) -> str:
        secret = self.settings.client_secret
        return secret.get_secret_value().strip() if secret else ""

    def oauth_problem(self) -> Optional[str]:
        values = ((f"{PREFIX}CLIENT_ID", (self.settings.client_id or "").strip()),
                  (f"{PREFIX}CLIENT_SECRET", self._secret()),
                  (f"{PREFIX}REDIRECT_URI", (self.settings.redirect_uri or "").strip()))
        missing = [name for name, value in values if not value]
        if missing:
            return f"Set {', '.join(missing)} (your WHOOP app's) to connect Whoop."
        redirect = urlparse(self.settings.redirect_uri.strip())
        if redirect.scheme not in ("https", "http") or not redirect.netloc:
            return f"{PREFIX}REDIRECT_URI must be a full URL, the one registered with your WHOOP app."
        return None

    def oauth_authorize_url(self, state: str) -> str:
        return whoop_api.authorization_url(self.settings.client_id.strip(), self.settings.redirect_uri.strip(),
                                           state)

    def _token_call(self, call, *args) -> Dict[str, Any]:
        try:
            return call(self.settings.client_id.strip(), self._secret(), *args,
                        user_agent=f"Tributary/{__version__}")
        except whoop_api.InvalidGrant:
            raise oauth.GrantRevoked() from None
        except whoop_api.InvalidClient:
            raise oauth.OAuthError(f"WHOOP refused the app's credentials (invalid_client): check {PREFIX}CLIENT_ID "
                                   f"and {PREFIX}CLIENT_SECRET.") from None
        except whoop_api.WhoopError as exc:
            raise oauth.OAuthError(str(exc)) from None

    def oauth_exchange(self, code: str) -> Dict[str, Any]:
        try:
            return self._token_call(whoop_api.exchange_code, self.settings.redirect_uri.strip(), code)
        except oauth.GrantRevoked:
            raise oauth.OAuthError("WHOOP refused the authorization code (invalid_grant): it expires within "
                                   "minutes, and works once. Connect again.") from None

    def oauth_refresh(self, refresh_token: str) -> Dict[str, Any]:
        return self._token_call(whoop_api.refresh_token, refresh_token)

    def oauth_revoke(self, access_token: str) -> None:
        try:
            whoop_api.revoke_access(access_token, user_agent=f"Tributary/{__version__}")
        except whoop_api.WhoopError as exc:
            raise ConnectorError(str(exc)) from None

    def api_configured(self) -> bool:
        return self.oauth_problem() is None and oauth.has_grant(self.key)

    def setup_hint(self) -> Optional[str]:
        problem = self.oauth_problem() or secretbox.problem()
        if problem:
            return problem
        if not oauth.has_grant(self.key):
            return "Not connected: connect Whoop on its page."
        return None

    # -- API -----------------------------------------------------------------

    def _token(self, force: bool) -> str:
        return oauth.access_token(self, force_refresh=force)

    def full_cursor(self, cursor: Any) -> Any:
        """A full resync reads everything again, but keeps the pins: a point
        keeps the time and tags it was first written with."""
        return {"pinned": _state(cursor)["pinned"]}

    def sync(self, cursor: Any) -> SyncResult:
        state = _state(cursor)
        lookback = timedelta(days=max(0, self.settings.lookback_days))
        client = whoop_api.Client(self._token, user_agent=f"Tributary/{__version__}")

        def lower(resource: str) -> Optional[datetime]:
            seen = state["since"].get(resource)
            return None if seen is None else _at(seen) - lookback

        # Recoveries have no start of their own: the sleeps' window, a day
        # wider (a recovery belongs to the cycle the sleep ends).
        sleeps_from = lower("sleep")
        try:
            # The first request refreshes the token when it is due: the new
            # pair is stored before any data is read.
            sleeps = client.records("sleep", sleeps_from)
            recoveries = client.records("recovery", sleeps_from - timedelta(days=1) if sleeps_from else None)
            cycles = client.records("cycle", lower("cycle"))
            workouts = client.records("workout", lower("workout"))
        except whoop_api.WhoopError as exc:
            raise ConnectorError(str(exc)) from None

        points, new_cursor, counts = api_points(sleeps, recoveries, cycles, workouts, state, _now())
        tz = ZoneInfo(app_settings.tz)
        froms = [moment for moment in (lower(r) for r in RESOURCES) if moment is not None]
        since = (f"since {min(froms).astimezone(tz).date().isoformat()}" if len(froms) == len(RESOURCES)
                 else "over the whole history")
        summary = [f"{counts['sleep']} night(s), {counts['nap']} nap(s), {counts['recovery']} recovery score(s), "
                   f"{counts['cycle']} cycle(s) and {counts['workout']} workout(s) {since}"]
        if counts["pending"]:
            summary.append(f"{counts['pending']} awaiting their score (read again by the next run)")
        return SyncResult(points=points, cursor=new_cursor, summary=summary)

    # -- Export --------------------------------------------------------------

    def _pins(self) -> Dict[str, Dict[str, list]]:
        """The API records the saved cursor pinned: what an export merges
        onto."""
        from app.database import SessionLocal
        from app.runner import load_cursor

        with SessionLocal() as db:
            return _state(load_cursor(db, self.key).get("sync"))["pinned"]

    def import_file(self, path: Path) -> FileImport:
        try:
            tables, no_cycle = whoop_export.read_export(path)
        except whoop_export.ExportFormatError as exc:
            raise ConnectorError(str(exc)) from None
        pins = self._pins()
        if not any(pins.values()) and oauth.has_grant(self.key):
            # Its rows would be written at their own times, and the API's
            # first run next to them at its own: let the API go first.
            raise ConnectorError("Whoop's API is connected but has not run yet: run it first (Run now), then "
                                 "import the export, so that it lands on the API's points.")
        points, span, summary = export_points(tables, pins, ZoneInfo(app_settings.tz))
        if no_cycle:
            summary.append(f"{no_cycle} journal answer(s) with no cycle, left out (no date to place them)")
        if not points:
            raise ConnectorError("This export holds nothing to import: nothing is written, nothing dropped.")
        return FileImport(points, {JOURNAL.name: span} if span else {}, summary)
