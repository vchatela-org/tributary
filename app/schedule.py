"""When a connector runs: a 5-field cron expression, in TRIBUTARY_TZ.

Where the value comes from, in order (the UI shows which one won, and offers
*Reset to default*):

1. the value saved in the UI (the `connector_schedules` table);
2. else `TRIBUTARY_<KEY>_SCHEDULE`, a cron expression;
3. else the deprecated `TRIBUTARY_<KEY>_INTERVAL_MINUTES`, still honoured so an
   existing deployment keeps working (mapped to the nearest cron, see
   `cron_from_interval`);
4. else the connector's own `default_schedule`.

An expression is parsed by `cronsim` (a maintained cron parser: no scheduler
framework, no dependencies of its own). Times are absolute, so a restart no
longer drifts a schedule the way the old "every N minutes from the start" did.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from itertools import islice
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError

logger = logging.getLogger(__name__)

# How many upcoming runs the UI previews.
PREVIEW_COUNT = 5


class CronError(ValueError):
    """An expression the user must fix, with a message meant for the UI."""


WEEKDAYS: Tuple[Tuple[int, str], ...] = (
    (0, "Sunday"), (1, "Monday"), (2, "Tuesday"), (3, "Wednesday"),
    (4, "Thursday"), (5, "Friday"), (6, "Saturday"),
)


# ────────────────────────────────────────────────
# Presets (what the UI offers; every one is just a cron)
# ────────────────────────────────────────────────

# The presets the schedule editor offers. `manual` has no expression (the
# connector is not scheduled); `custom` takes the expression as typed.
PRESETS: Tuple[Tuple[str, str], ...] = (
    ("manual", "Manual only"),
    ("hourly", "Every hour"),
    ("every6h", "Every 6 hours"),
    ("daily", "Daily at a time"),
    ("weekly", "Weekly on a day and time"),
    ("custom", "Custom (cron)"),
)


def _clock(value: str) -> Tuple[int, int]:
    """'HH:MM' -> (hour, minute). The UI posts a <input type=time>."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise CronError(f"Time must look like 08:30, not “{value}”.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise CronError(f"Time out of range: “{value}”.")
    return hour, minute


def build_cron(preset: str, at: str = "", weekday: str = "", custom: str = "") -> Optional[str]:
    """The cron a preset stands for. None: no schedule (manual only)."""
    if preset == "manual":
        return None
    if preset == "hourly":
        return "0 * * * *"
    if preset == "every6h":
        return "0 */6 * * *"
    if preset == "daily":
        hour, minute = _clock(at)
        return f"{minute} {hour} * * *"
    if preset == "weekly":
        hour, minute = _clock(at)
        if not (weekday or "").strip().isdigit() or not 0 <= int(weekday) <= 6:
            raise CronError("Pick a day of the week.")
        return f"{minute} {hour} * * {int(weekday)}"
    if preset == "custom":
        return validate(custom)
    raise CronError(f"Unknown preset “{preset}”.")


def validate(expr: str) -> str:
    """The expression, normalised, or CronError with what is wrong. Exactly
    five fields: the seconds field cronsim also accepts would let a run land
    off the minute, and nothing here needs one."""
    text = " ".join((expr or "").split())
    if not text:
        raise CronError("A cron expression is required.")
    fields = text.split(" ")
    if len(fields) != 5:
        raise CronError(f"A cron expression has 5 fields (minute hour day month weekday), not {len(fields)}.")
    try:
        CronSim(text, _zone_aware(datetime.now(timezone.utc)))
    except CronSimError as exc:
        raise CronError(f"{exc}.") from None
    return text


def _zone_aware(moment: datetime, tz: Optional[ZoneInfo] = None) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz or _zone("UTC"))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown TZ must not stop a page
        logger.warning("Unknown TRIBUTARY_TZ %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def next_runs(expr: str, tz_name: str, count: int = PREVIEW_COUNT,
              after: Optional[datetime] = None) -> List[datetime]:
    """The next `count` occurrences, in TRIBUTARY_TZ (aware datetimes)."""
    tz = _zone(tz_name)
    start = after or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return list(islice(CronSim(expr, start.astimezone(tz)), count))


def next_run_after(expr: str, tz_name: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """The next occurrence, or None if the expression has none left."""
    runs = next_runs(expr, tz_name, 1, after)
    return runs[0] if runs else None


def to_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """Aware -> naive UTC, the way every time is stored (app/models.utcnow)."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


# ────────────────────────────────────────────────
# Describing an expression
# ────────────────────────────────────────────────

def _shape(expr: str) -> Optional[str]:
    """A preset-shaped expression, described in words ("Daily at 03:10"); None
    when it is something else, and cronsim's explain() says it instead."""
    minute, hour, dom, month, dow = expr.split(" ")
    every_minutes = minute.startswith("*/") and minute[2:].isdigit()
    every_hours = hour.startswith("*/") and hour[2:].isdigit()
    plain = lambda part, limit: part.isdigit() and int(part) <= limit  # noqa: E731
    if dom == "*" and month == "*" and dow == "*":
        if every_minutes and hour == "*":
            return f"Every {int(minute[2:])} minutes"
        if minute == "0" and hour == "*":
            return "Every hour"
        if minute == "0" and every_hours:
            return f"Every {int(hour[2:])} hours"
        if plain(minute, 59) and plain(hour, 23):
            return f"Daily at {int(hour):02d}:{int(minute):02d}"
    if dom == "*" and month == "*" and dow.isdigit() and plain(minute, 59) and plain(hour, 23):
        day = dict(WEEKDAYS).get(int(dow), dow)
        return f"Weekly on {day} at {int(hour):02d}:{int(minute):02d}"
    return None


def describe(expr: str) -> str:
    """An expression in words, for the UI and the API."""
    try:
        return _shape(expr) or CronSim(expr, _zone_aware(datetime.now(timezone.utc))).explain()
    except CronSimError:
        return expr


# ────────────────────────────────────────────────
# The deprecated interval, mapped to the nearest cron
# ────────────────────────────────────────────────

def cron_from_interval(minutes: int) -> Optional[str]:
    """TRIBUTARY_<KEY>_INTERVAL_MINUTES, deprecated, as a cron. An interval
    counted from the start cannot always be said in cron: None when it cannot
    (the caller falls back to the connector's default and says so)."""
    if minutes is None or minutes < 1:
        return None
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"          # on the clock, not from the start
    if minutes == 60:
        return "0 * * * *"
    if minutes % 60 == 0 and 1 <= minutes // 60 <= 23:
        return f"0 */{minutes // 60} * * *"
    return None


# ────────────────────────────────────────────────
# Resolving what a connector actually runs on
# ────────────────────────────────────────────────

# Where a resolved schedule came from, for the "currently: ..." line.
SOURCES = {
    "ui": "set here",
    "env": "from the environment",
    "interval": "from the deprecated interval",
    "default": "from the connector's default",
    "none": "no schedule",
}


@dataclass(frozen=True)
class Resolved:
    cron: Optional[str] = None
    source: str = "none"
    label: str = "No schedule"
    next_run: Optional[datetime] = None   # naive UTC
    note: Optional[str] = None            # a value that could not be used

    @property
    def scheduled(self) -> bool:
        return self.cron is not None

    @property
    def source_label(self) -> str:
        return SOURCES.get(self.source, self.source)


def _env_cron(connector) -> Optional[str]:
    """TRIBUTARY_<KEY>_SCHEDULE, absent or blank as None (the settings field
    defaults to None, so "not set" is distinguishable from the default)."""
    value = getattr(connector.settings, "schedule", None)
    return (value or "").strip() or None


def _legacy_interval(connector) -> Optional[int]:
    return getattr(connector.settings, "interval_minutes", None)


def resolve(connector, stored: Optional[str], tz_name: str,
            after: Optional[datetime] = None) -> Resolved:
    """What this connector runs on, and where that came from."""
    if not connector.is_configured():
        # An upload-only source has no schedule of its own: the user imports a
        # file when they have one. A saved value is kept, not applied.
        return Resolved()

    note = None
    cron = None
    source = "none"
    if stored is not None and not stored.strip():
        # Saved as "Manual only": no schedule at all, and the environment does
        # not bring one back -- the user asked for it by name.
        return Resolved(source="ui", label="Manual only")
    if stored:
        try:
            cron, source = validate(stored), "ui"
        except CronError as exc:
            note = f"The saved schedule could not be read ({exc})."
    if cron is None:
        env = _env_cron(connector)
        if env:
            try:
                cron, source = validate(env), "env"
            except CronError as exc:
                note = (f"TRIBUTARY_{connector.key.upper()}_SCHEDULE is not a cron expression "
                        f"({exc}); using the next value below.")
    if cron is None:
        minutes = _legacy_interval(connector)
        if minutes is not None:
            mapped = cron_from_interval(minutes)
            if mapped:
                cron, source = mapped, "interval"
            elif minutes >= 1440 and connector.default_schedule:
                # "Once a day" or less often: the interval decided the cadence, and
                # the connector's daily default is the closest cron there is. No
                # note: it is exactly what the deployment was doing.
                cron, source = connector.default_schedule, "interval"
            else:
                note = (f"TRIBUTARY_{connector.key.upper()}_INTERVAL_MINUTES={minutes} has no cron "
                        f"equivalent (it is an interval counted from the start, not a time of day); "
                        f"using the connector's default instead.")
    if cron is None and connector.default_schedule:
        cron, source = connector.default_schedule, "default"

    if cron is None:
        return Resolved(source="none", note=note)
    return Resolved(cron=cron, source=source, label=describe(cron),
                    next_run=to_utc(next_run_after(cron, tz_name, after)), note=note)


def clock_of(cron: Optional[str], fallback: str = "03:10") -> str:
    """The 'HH:MM' a preset editor should start on, read from an expression."""
    fields = (cron or "").split(" ")
    if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
        hour, minute = int(fields[1]), int(fields[0])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return dtime(hour, minute).strftime("%H:%M")
    return fallback


def weekday_of(cron: Optional[str], fallback: str = "1") -> str:
    fields = (cron or "").split(" ")
    if len(fields) == 5 and fields[4].isdigit():
        return fields[4]
    return fallback


def presets_for(cron: Optional[str]) -> List[Dict[str, object]]:
    """The preset list with the one the expression matches marked, for the
    editor; an expression matching none of them marks *Custom*."""
    if not cron:
        return [{"key": key, "label": label, "chosen": key == "manual"} for key, label in PRESETS]
    editor = {"at": clock_of(cron), "weekday": weekday_of(cron)}
    out: List[Dict[str, object]] = []
    matched = False
    for key, label in PRESETS:
        if key == "custom":
            continue
        try:
            matches = bool(cron) and build_cron(key, editor["at"], editor["weekday"]) == cron
        except CronError:
            matches = False
        matched = matched or matches
        out.append({"key": key, "label": label, "chosen": matches})
    # Anything the presets above do not cover is a custom expression.
    out.append({"key": "custom", "label": "Custom (cron)", "chosen": bool(cron) and not matched})
    return out

