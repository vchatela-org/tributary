"""Sleep as Android (sleep tracking): the sessions of an uploaded backup, into
SleepStats..sleep_session, ..sleep_stage and ..sleep_vitals.

Ported from a one-off import script, writing exactly its series (same
measurements, tags, fields, types and times), so importing the backup again
rewrites that history in place and never duplicates it (docs/porting.md).
source=sleep_as_android had that script as its only writer.

Sleep as Android has no API: the app's backup (*Settings → Backup → Export
data*, `sleep-export.zip` holding `sleep-export.csv`, or the CSV on its own) is
imported in the UI (*Dry run* first). The backup is the whole history, so an
import replaces the connector's own series (rebuild_own_source, bare points):
a session the export no longer holds is one the app does not know any more.
Upload only: a folder of backups is not the whole history a watched folder's
rebuild would make it, so `watch_dir` is left out.

A session:
  sleep_session  at its end (the wake), tags kind = night / nap (the app has no
                 nap flag: a session that starts in the daytime and is short is
                 a nap, except the short post-alarm re-tracks before 10:00,
                 which stay night). Fields: the contract's (start, in_bed_s,
                 asleep_s as the app reports it -- Hours shortened by
                 LenAdjust, the exclusive awake/light/deep/rem seconds,
                 cycles, tz_offset_min, bedtime_min as signed minutes from
                 midnight, wake_min), the app's own (avg_hr, min_hr, max_hr
                 from its HR samples, deep_pct, rating, sdnn_ms -- SDNN, not
                 RMSSD, snore_s, noise_level), tz (the zone's name), manual
                 (true for a hand-entered session), and tags (the #hashtags of
                 its comment). The comment's free text is `note`, written only
                 with TRIBUTARY_SLEEP_AS_ANDROID_NOTES.
  sleep_stage    one point per hypnogram segment, at its start: level (deep 0,
                 light 1, rem 2, awake 3, Garmin's scale so hypnograms line up
                 across sources), stage (the word) and duration_s. Sleep as
                 Android draws REM on top of light and awake on top of either;
                 the segments are flattened with a precedence (awake beats rem
                 beats deep beats light) so each moment counts once.
  sleep_vitals   the samples the app recorded, at their time: hr_bpm, spo2_pct,
                 sdnn_ms -- whichever it has.

The times carry milliseconds in the export; Tributary writes whole seconds, so
each is floored (its contract is "the same instant, to the second": a sub-second
loss no dashboard sees, and the one the equality check compares at).
"""
import collections
import csv
import datetime
import io
import statistics
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app.connectors.base import Connector, ConnectorError, ConnectorSettings, FileImport
from app.points import Point
from app.schema import SLEEP_SESSION, SLEEP_STAGE, SLEEP_VITALS

PREFIX = "TRIBUTARY_SLEEP_AS_ANDROID_"
SOURCE = "sleep_as_android"
# The CSV the backup holds (or the user uploads on its own).
MEMBER = "sleep-export.csv"

# Same levels as Garmin's SleepStageLevel, so hypnograms line up across sources.
LEVELS = {"deep": 0, "light": 1, "rem": 2, "awake": 3}
# Sleep as Android draws REM on top of light (and occasionally deep), and awake
# on top of either, whereas Garmin and WHOOP report exclusive stages that add up
# to the time in bed. Flatten with this precedence: a moment flagged both REM
# and light counts as REM, awake beats everything.
PRECEDENCE = ("awake", "rem", "deep", "light")

# The app has no nap flag. A session is a nap when it starts in the daytime and
# is short; the 07:00-10:00 short records are post-alarm re-tracks, i.e. the
# tail of the night, and stay night.
NAP_START_HOURS = range(10, 20)
NAP_MAX_IN_BED_S = 4 * 3600

# The plausible range of a heart-rate sample (bpm): the app also writes 0 / junk.
HR_RANGE = (25.0, 220.0)

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _read_text(path: Path) -> str:
    """The export CSV, from the backup zip (its `sleep-export.csv`) or the file
    itself when a CSV was uploaded."""
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                if MEMBER not in archive.namelist():
                    raise ConnectorError(f"the zip holds no {MEMBER}: is this a Sleep as Android backup "
                                         "(Settings → Backup → Export data)?")
                return archive.read(MEMBER).decode("utf-8")
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ConnectorError(f"unreadable ({type(exc).__name__}: {exc}).") from None


def parse_records(text: str):
    """Yield (header, values) per record.

    Each record is a header line ("Id,Tz,...") followed by its data line, and
    for tracked nights a third line of noise samples, which is skipped. The
    header differs per record: the actigraphy columns are labelled with that
    night's clock times and the trailing columns are all named "Event".
    """
    header = None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if row[0] == "Id":
            header = row
        elif row[0].isdigit() and header is not None:
            yield header, row


def pair_intervals(starts: List[int], ends: List[int]) -> List[Tuple[int, int]]:
    """Match each *_START to the first unused *_END after it."""
    out, ends = [], sorted(ends)
    for s in sorted(starts):
        for i, e in enumerate(ends):
            if e > s:
                out.append((s, e))
                del ends[i]
                break
    return out


def flatten_stages(intervals: Dict[str, List[Tuple[int, int]]], start_ms: int,
                   end_ms: int) -> List[Tuple[str, int, int]]:
    """Exclusive (level_name, seg_start_ms, seg_end_ms) segments, merged."""
    clipped = {
        k: [(max(a, start_ms), min(b, end_ms)) for a, b in v if min(b, end_ms) > max(a, start_ms)]
        for k, v in intervals.items()
    }
    points = sorted({t for v in clipped.values() for ab in v for t in ab})
    segs: List[Tuple[str, int, int]] = []
    for a, b in zip(points, points[1:]):
        mid = (a + b) / 2
        stage = next((k for k in PRECEDENCE if any(x <= mid < y for x, y in clipped.get(k, ()))), None)
        if stage is None:
            continue  # untracked: paused, or before the first / after the last stage
        if segs and segs[-1][0] == stage and segs[-1][2] == a:
            segs[-1] = (stage, segs[-1][1], b)
        else:
            segs.append((stage, a, b))
    return segs


def signed_minutes(local_dt: datetime.datetime) -> int:
    """Minutes after local midnight, negative for the evening: 23:30 -> -30."""
    m = local_dt.hour * 60 + local_dt.minute
    return m - 1440 if m >= 12 * 60 else m


def _at(ms: int) -> datetime.datetime:
    """UTC from an epoch-millisecond stamp (whole seconds once encoded)."""
    return _EPOCH + datetime.timedelta(milliseconds=ms)


class Record:
    """One parsed session: its points' fields and times, and the comment's
    hashtags and free text (the tags always written, the note only when asked)."""

    def __init__(self, kind: str, end_ms: int, fields: Dict[str, Any],
                 segments: List[Tuple[str, int, int]], vitals: Dict[int, Dict[str, float]],
                 tags: List[str], note: str):
        self.kind = kind
        self.end_ms = end_ms
        self.fields = fields
        self.segments = segments
        self.vitals = vitals
        self.tags = tags
        self.note = note


def build_record(header: List[str], values: List[str]) -> Optional[Record]:
    """One session from its header and data line, mapped as the script did."""
    m = dict(zip(header[:15], values[:15]))
    events = [v for name, v in zip(header, values) if name == "Event" and v]

    try:
        tz = ZoneInfo(m["Tz"])
    except (KeyError, ValueError):
        raise ConnectorError(f"unreadable time zone {m.get('Tz')!r} in the export.") from None

    start_ms = int(m["Id"])
    hours = float(m["Hours"])
    in_bed_s = round(hours * 3600)
    if in_bed_s <= 0:
        return None
    # `Hours` is exactly To - From (verified on every record of the 2023 export),
    # so the end is derived from the epoch Id rather than re-parsing the local
    # "To" string - which would need DST disambiguation.
    end_ms = start_ms + in_bed_s * 1000
    start_local = datetime.datetime.fromtimestamp(start_ms / 1000, tz)
    end_local = datetime.datetime.fromtimestamp(end_ms / 1000, tz)

    # Events are KIND-<epoch ms>[-<value>].
    starts: Dict[str, List[int]] = collections.defaultdict(list)
    ends: Dict[str, List[int]] = collections.defaultdict(list)
    vitals: Dict[int, Dict[str, float]] = collections.defaultdict(dict)
    for e in events:
        kind, _, rest = e.partition("-")
        ts, _, val = rest.partition("-")
        if not ts.isdigit():
            continue
        ts = int(ts)
        for stage in ("LIGHT", "DEEP", "REM", "AWAKE"):
            if kind == stage + "_START":
                starts[stage.lower()].append(ts)
            elif kind == stage + "_END":
                ends[stage.lower()].append(ts)
        try:
            fval = float(val) if val else None
        except ValueError:
            fval = None
        if fval is None:
            continue
        if kind == "HR" and HR_RANGE[0] <= fval <= HR_RANGE[1]:
            vitals[ts]["hr_bpm"] = fval
        elif kind == "SPO2" and 50 <= fval <= 100:
            vitals[ts]["spo2_pct"] = fval
        elif kind == "SDNN" and fval > 0:
            vitals[ts]["sdnn_ms"] = fval

    intervals = {k: pair_intervals(starts[k], ends[k]) for k in LEVELS}
    has_hypnogram = bool(intervals["light"] or intervals["deep"])
    segs = flatten_stages(intervals, start_ms, end_ms) if has_hypnogram else []

    manual = m["Cycles"] == "-1"
    kind = "nap" if start_local.hour in NAP_START_HOURS and in_bed_s < NAP_MAX_IN_BED_S else "night"

    fields: Dict[str, Any] = {
        "start": start_ms // 1000,
        "in_bed_s": in_bed_s,
        "manual": manual,
        "tz": m["Tz"],
        "tz_offset_min": int(start_local.utcoffset().total_seconds() // 60),
        "bedtime_min": signed_minutes(start_local),
        "wake_min": end_local.hour * 60 + end_local.minute,
    }
    # The app's own sleep length: Hours shortened by LenAdjust (negative minutes
    # for sleep latency, pauses and detected awake time).
    len_adjust = int(float(m["LenAdjust"] or 0))
    fields["asleep_s"] = max(0, in_bed_s + min(len_adjust, 0) * 60)

    if segs:
        totals: collections.Counter = collections.Counter()
        for stage, a, b in segs:
            totals[stage] += (b - a) // 1000
        for stage in LEVELS:
            fields[stage + "_s"] = totals[stage]

    deep = float(m["DeepSleep"])
    if deep >= 0:  # -1 / -2: no hypnogram
        fields["deep_pct"] = round(deep * 100, 2)
    cycles = int(m["Cycles"])
    if cycles >= 0 and not manual:
        fields["cycles"] = cycles
    rating = float(m["Rating"])
    if rating > 0:  # 0.0 = not rated
        fields["rating"] = rating
    snore = float(m["Snore"])
    if snore >= 0:  # -1 = snore detection off, 0 = on and nothing heard
        fields["snore_s"] = snore
    noise = float(m["Noise"])
    if noise >= 0:
        fields["noise_level"] = noise

    hrs = [v["hr_bpm"] for v in vitals.values() if "hr_bpm" in v]
    if hrs:
        fields["avg_hr"] = round(statistics.fmean(hrs), 1)
        fields["min_hr"] = min(hrs)
        fields["max_hr"] = max(hrs)
    spo2 = [v["spo2_pct"] for v in vitals.values() if "spo2_pct" in v]
    if spo2:
        fields["avg_spo2_pct"] = round(statistics.fmean(spo2), 1)
    sdnn = [v["sdnn_ms"] for v in vitals.values() if "sdnn_ms" in v]
    if sdnn:
        fields["sdnn_ms"] = round(statistics.fmean(sdnn), 1)

    comment = " ".join(m["Comment"].split())
    tags_ = [w for w in comment.split() if w.startswith("#")]
    note = " ".join(w for w in comment.split() if not w.startswith("#"))

    return Record(kind, end_ms, fields, segs, {ts: v for ts, v in vitals.items()}, tags_, note)


class SleepAsAndroidSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # Write each session's comment as `note` (free text: off by default; the
    # tags of the comment are always written).
    notes: bool = False


class SleepAsAndroidConnector(Connector):
    key = SOURCE
    label = "Sleep as Android"
    description = ("The sessions, hypnogram and overnight vitals of a Sleep as Android backup "
                   "imported in the UI (Settings → Backup → Export data).")
    icon = "bi-moon-stars"
    category = "Sleep"
    settings_class = SleepAsAndroidSettings
    measurements = (SLEEP_SESSION, SLEEP_STAGE, SLEEP_VITALS)
    file_types = (".zip", ".csv")
    # The backup is the app's whole history: importing it drops the connector's
    # own series and rewrites them from the file, as the script's --replace did.
    rebuild_own_source = True

    @property
    def watch_dir(self) -> Optional[Path]:
        # Upload only: a backup is the whole history, so a folder of them adds
        # nothing a watched folder's rebuild would assume.
        return None

    def import_fingerprint(self) -> Dict[str, Any]:
        """Writing the notes or not decides what an import writes: a change
        invalidates a checked (dry-run) upload rather than let it apply a diff
        the user never saw."""
        return {"notes": self.settings.notes}

    def import_file(self, path: Path) -> FileImport:
        records: List[Record] = []
        for header, values in parse_records(_read_text(path)):
            built = build_record(header, values)
            if built is not None:
                records.append(built)
        if not records:
            raise ConnectorError("This backup holds no session to import: nothing to write back, "
                                 "so the series is left as it is.")

        notes = self.settings.notes
        points: List[Point] = []
        nights = naps = segments = samples = 0
        for rec in records:
            fields = dict(rec.fields)
            # The comment's tags are always written; its free text only when asked.
            if notes and rec.note:
                fields["note"] = rec.note
            if rec.tags:
                fields["tags"] = " ".join(rec.tags)
            points.append(Point(SLEEP_SESSION, _at(rec.end_ms), {"kind": rec.kind}, fields))
            nights += rec.kind == "night"
            naps += rec.kind == "nap"
            for stage, a, b in rec.segments:
                points.append(Point(SLEEP_STAGE, _at(a), {},
                                    {"level": LEVELS[stage], "stage": stage, "duration_s": (b - a) // 1000}))
                segments += 1
            for ts, vals in sorted(rec.vitals.items()):
                points.append(Point(SLEEP_VITALS, _at(ts), {}, dict(vals)))
                samples += 1

        summary = [f"{len(records)} session(s) ({nights} night(s), {naps} nap(s))",
                   f"{segments} stage segment(s)", f"{samples} vitals sample(s)"]
        return FileImport(points, summary=summary)
