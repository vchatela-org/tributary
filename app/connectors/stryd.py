"""Stryd (running footpod): the Footpath of an export Stryd sends on request,
into BodyStats..footpath_step and ..footpath_loop.

A Stryd Duo (a pod on each shoe) records the 3D path of the feet. Stryd's
Footpath view draws it; the data comes as an export, on request to Stryd's
support: a zip of `<id>_footdata_<n>.json` files, one per recording. Each file
holds `foot_data_list`, a sample of the run's steps (one every 2 to 7 seconds
per foot), and for each step:
  side             1 the left foot, 2 the right one;
  timestamp        epoch seconds (UTC), and `timestamp_frac` its fraction in
                   the newer files;
  speed, power     m/s and W at that step, `incline` the slope (a ratio);
  sample_freq      the sampling rate of the path (160 Hz);
  positions        the foot's path over one gait cycle, from mid-stance to the
                   next mid-stance, in metres, with the forward motion taken
                   out: x forward (positive ahead of where the cycle started),
                   y sideways (the left foot's negative), z up. It starts and
                   ends at (0, 0, 0).
`velocities`, `angles` and the `scale_*` factors are left out: the path is
already in metres (`positions` alone draws Stryd's Footpath), and the angles
are only in the newer files. The pod's Bluetooth address, the account id and
the file paths in the export are never read into a point.

The export repeats itself: a run synced from the phone and uploaded again
later is there two to five times, whole or in part, under different names. A
step is the same step in every copy (same foot, same timestamp), so the steps
are merged on (foot, second) before anything is written, and a step written
again lands on the same key. Uploading the export again, or in several parts,
therefore rewrites the same points (an import only adds: nothing is dropped,
rebuild_own_source is off).

What is written:
  footpath_step  one point per sampled step, at its second. Tags: `side`
                 (left / right) and `pace`, the 15 s/km band its speed falls
                 in ("4:30" is 4:30 to 4:44 per km), so a dashboard compares
                 the form at the same pace. Fields, in cm from the path: lift
                 (the highest point), back (how far behind the cycle's start
                 the foot travels), reach (how far ahead it swings), width
                 (sideways range), touchdown (where it meets the ground again,
                 ahead of the cycle's start); and speed (m/s), power (W),
                 incline. Steps slower than RUNNING_MIN_SPEED (walking, a
                 stop) are left out.
  footpath_loop  each run's typical cycle, per foot: the median, point by
                 point, of its steps at the run's usual pace (within 5% of the
                 median speed), resampled to LOOP_POINTS points. Tags: `run`
                 (its local start, "2026-03-01 08:15", in TRIBUTARY_TZ) and
                 `side`; one point per position, a second apart from the run's
                 start, with `phase` (its index), x_cm, y_cm, z_cm, and the
                 steps and speed it is the median of. A run is a stretch of
                 steps with no gap longer than RUN_GAP_S: the copies of a run
                 belong in the same upload, or its loop is drawn from the part
                 each one holds.
"""
import json
import math
import statistics
import zipfile
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_STRYD_"
SOURCE = "stryd"

FOOTPATH_STEP = Measurement(
    database="BodyStats", name="footpath_step", tags=("side", "pace"),
    fields={name: float for name in ("lift_cm", "back_cm", "reach_cm", "width_cm", "touchdown_cm",
                                     "speed_mps", "power_w", "incline")},
    last_wins=True,
)
FOOTPATH_LOOP = Measurement(
    database="BodyStats", name="footpath_loop", tags=("run", "side"),
    fields={"phase": int, "x_cm": float, "y_cm": float, "z_cm": float, "steps": int, "speed_mps": float},
    last_wins=True,
)

SIDES = {1: "left", 2: "right"}
# Below it a step is walking or a stop (2 m/s is 8:20 per km): not a running stride.
RUNNING_MIN_SPEED = 2.0
# Pace bands, s/km: 15 s wide, the slowest one open-ended.
PACE_BAND_S = 15
PACE_FASTEST_S, PACE_SLOWEST_S = 150, 480
# A gap longer than this between two sampled steps starts another run.
RUN_GAP_S = 600
# The loop: points per cycle; the steps it is the median of are those within
# LOOP_SPEED_BAND of the run's median speed, at least LOOP_MIN_STEPS of them.
LOOP_POINTS = 64
LOOP_SPEED_BAND = 0.05
LOOP_MIN_STEPS = 10
# The foot is on the ground below this height (m).
GROUND_M = 0.01
# A path with fewer points, or a coordinate beyond this (m), is not a step.
MIN_PATH_POINTS = 16
MAX_COORD_M = 3.0
# Largest JSON file read from the export (the biggest real ones are ~35 MB).
MAX_MEMBER_BYTES = 200 * 1024 * 1024


def pace_band(speed: float) -> str:
    """The 15 s/km band of a speed in m/s, by its lower bound: 3.5 m/s is 4:45/km, band "4:45"."""
    pace = 1000 / speed
    band = min(max(int(pace // PACE_BAND_S) * PACE_BAND_S, PACE_FASTEST_S), PACE_SLOWEST_S)
    return f"{band // 60}:{band % 60:02d}"


def resample(path: List[Tuple[float, float, float]], n: int = LOOP_POINTS) -> array:
    """The path at n evenly spaced instants of its cycle, first and last
    included (x, y, z interleaved, metres)."""
    out = array("f")
    last = len(path) - 1
    for i in range(n):
        at = i * last / (n - 1)
        lo = min(int(at), last - 1)
        f = at - lo
        a, b = path[lo], path[lo + 1]
        out.extend(a[k] + (b[k] - a[k]) * f for k in range(3))
    return out


@dataclass
class Step:
    side: int
    time: float
    speed: float
    power: Optional[float]
    incline: Optional[float]
    fields: Dict[str, float]
    loop: array


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def read_step(raw: Dict[str, Any]) -> Optional[Step]:
    """One step of `foot_data_list`, or None when it is not a running step with
    a usable path."""
    side = raw.get("side")
    t = _number(raw.get("timestamp"))
    speed = _number(raw.get("speed"))
    if side not in SIDES or t is None or speed is None or speed < RUNNING_MIN_SPEED:
        return None
    t += _number(raw.get("timestamp_frac")) or 0.0
    positions = raw.get("positions")
    if not isinstance(positions, list) or len(positions) < MIN_PATH_POINTS:
        return None
    path = []
    for p in positions:
        if not isinstance(p, tuple):
            return None
        if not all(c is not None and abs(c) <= MAX_COORD_M for c in p):
            return None
        path.append(p)
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    zs = [p[2] for p in path]
    top = max(range(len(zs)), key=zs.__getitem__)
    fields = {
        "lift_cm": zs[top] * 100,
        "back_cm": -min(xs) * 100,
        "reach_cm": max(xs) * 100,
        "width_cm": (max(ys) - min(ys)) * 100,
    }
    touchdown = next((i for i in range(top, len(zs)) if zs[i] < GROUND_M), None)
    if touchdown is not None:
        fields["touchdown_cm"] = xs[touchdown] * 100
    return Step(side, t, speed, _number(raw.get("power")), _number(raw.get("incline")),
                {k: round(v, 2) for k, v in fields.items()}, resample(path))


def _xyz(obj: Dict[str, Any]) -> Any:
    """json object_hook: a point {"x", "y", "z"} becomes a tuple as soon as it
    is read (a run holds ~100,000 of them: tuples keep the memory down)."""
    if len(obj) == 3 and "x" in obj and "y" in obj and "z" in obj:
        return tuple(_number(obj[k]) for k in ("x", "y", "z"))
    return obj


def read_footdata(data: bytes) -> Optional[List[Dict[str, Any]]]:
    """The `foot_data_list` of one export file; None when the file is not one."""
    try:
        doc = json.loads(data, object_hook=_xyz)
    except ValueError:
        return None
    steps = doc.get("foot_data_list") if isinstance(doc, dict) else None
    return steps if isinstance(steps, list) else None


def _members(path: Path) -> Iterator[Tuple[str, bytes]]:
    """(name, bytes) of each JSON file of the upload: a zip's, or the file."""
    try:
        if path.suffix.lower() == ".json":
            if path.stat().st_size > MAX_MEMBER_BYTES:
                raise ConnectorError(f"the file is larger than {MAX_MEMBER_BYTES // 2**20} MB.")
            yield path.name, path.read_bytes()
            return
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".json"):
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ConnectorError(f"{Path(info.filename).name} is larger than {MAX_MEMBER_BYTES // 2**20} MB.")
                yield info.filename, archive.read(info)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None


def split_runs(steps: List[Step]) -> List[List[Step]]:
    """Steps in time order, cut where RUN_GAP_S passes without one."""
    runs: List[List[Step]] = []
    for step in sorted(steps, key=lambda s: s.time):
        if runs and step.time - runs[-1][-1].time <= RUN_GAP_S:
            runs[-1].append(step)
        else:
            runs.append([step])
    return runs


def typical_loop(steps: List[Step]) -> Optional[Tuple[List[Tuple[float, float, float]], int, float]]:
    """(the median loop, in cm, the number of steps, their median speed) of one
    foot's steps in a run: those within LOOP_SPEED_BAND of its median speed, or
    all of them when too few are; None under LOOP_MIN_STEPS."""
    if len(steps) < LOOP_MIN_STEPS:
        return None
    speed = statistics.median(s.speed for s in steps)
    chosen = [s for s in steps if abs(s.speed - speed) <= speed * LOOP_SPEED_BAND]
    if len(chosen) < LOOP_MIN_STEPS:
        chosen = steps
    loop = []
    for i in range(LOOP_POINTS):
        loop.append(tuple(round(statistics.median(s.loop[i * 3 + k] for s in chosen) * 100, 2) for k in range(3)))
    return loop, len(chosen), statistics.median(s.speed for s in chosen)


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(math.floor(seconds), timezone.utc)


class StrydSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")


class StrydConnector(Connector):
    key = SOURCE
    label = "Stryd"
    description = ("The Footpath of a Stryd Duo (the 3D path of each foot, step by step): the export Stryd "
                   "sends on request, imported in the UI.")
    icon = "bi-person-walking"
    category = "Nutrition & fitness"
    settings_class = StrydSettings
    measurements = (FOOTPATH_STEP, FOOTPATH_LOOP)
    file_types = (".zip", ".json")

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: the export arrives once, on request.
        return None

    def import_file(self, path: Path) -> FileImport:
        from app.config import settings

        tz = ZoneInfo(settings.tz)
        files = skipped_files = 0
        unique: Dict[Tuple[int, int], Step] = {}
        seen = 0
        for name, data in _members(path):
            raw_steps = read_footdata(data)
            del data
            if raw_steps is None:
                skipped_files += 1
                continue
            files += 1
            for raw in raw_steps:
                step = read_step(raw) if isinstance(raw, dict) else None
                if step is None:
                    continue
                seen += 1
                # The same step in another copy of the run: same foot, same second.
                unique.setdefault((step.side, math.floor(step.time)), step)
        if not unique:
            raise ConnectorError("This file holds no Footpath step to import: is it a Stryd export "
                                 "(<id>_footdata_<n>.json files)?")

        points: List[Point] = []
        for step in unique.values():
            fields: Dict[str, Any] = dict(step.fields)
            fields["speed_mps"] = round(step.speed, 3)
            if step.power is not None:
                fields["power_w"] = step.power
            if step.incline is not None:
                fields["incline"] = round(step.incline, 4)
            points.append(Point(FOOTPATH_STEP, _at(step.time),
                                {"side": SIDES[step.side], "pace": pace_band(step.speed)}, fields))

        runs = split_runs(list(unique.values()))
        loops = 0
        for run in runs:
            start = math.floor(run[0].time)
            label = datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d %H:%M")
            for side, name in SIDES.items():
                found = typical_loop([s for s in run if s.side == side])
                if found is None:
                    continue
                loop, count, speed = found
                loops += 1
                for i, (x, y, z) in enumerate(loop):
                    points.append(Point(FOOTPATH_LOOP, _at(start + i), {"run": label, "side": name},
                                        {"phase": i, "x_cm": x, "y_cm": y, "z_cm": z, "steps": count,
                                         "speed_mps": round(speed, 3)}))

        sides = Counter(SIDES[s] for s, _ in unique)
        summary = [f"{files} Footpath file(s)" + (f" ({skipped_files} other file(s) skipped)" if skipped_files else ""),
                   f"{len(unique)} running step(s) ({sides['left']} left, {sides['right']} right; "
                   f"{seen - len(unique)} repeated in another copy)",
                   f"{len(runs)} run(s), {loops} typical loop(s)"]
        return FileImport(points, summary=summary)
