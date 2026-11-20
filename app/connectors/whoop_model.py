"""WHOOP's models, rebuilt from any device: sleep need (and the debt it
carries), sleep performance, sleep consistency, recovery, day strain, and the
sleep planner's bedtime -- recomputed night by night from WHOOP's own inputs,
from a Fitbit's and from a Garmin's, into SleepStats.

A derived source: it calls no API and reads no file. It reads what the other
writers already put in InfluxDB -- Tributary's Whoop connector (`sleep_session`,
`daily_activity`, source `whoop`), fitbit-fetch (`FitbitHealthStats`) and
garmin-grafana (`GarminStats`) -- and writes what WHOOP's app would show from
each. Reading other tools' *values* is new for Tributary (it otherwise reads
another tool's timestamps only, app/freshness.py); this connector is the one
place that does, it is off until TRIBUTARY_WHOOP_MODEL_ENABLED, and it never
writes into another source's series (docs/connectors/whoop_model.md).

The models (reverse-engineered on a WHOOP history of 600 nights; how, and how
close they come, in docs/connectors/whoop_model.md):
  sleep need   = baseline + a·(S + b)^c + debt, S = the previous cycle's day
                 strain (an exact identity in WHOOP's own fields; the strain
                 term reproduced to a minute).
  debt         = r × max(0, last night's need − last night's sleep), on
                 consecutive nights; r = 9/14 after a night scored above 87,
                 else ½. Part of the need, so it halves night after night.
  performance  = s1·min(100, sleep/need %) + s2·efficiency % + s3·consistency %
                 + s0, within 0..100.
  consistency  = k1·(mean overlap of the night's sleep window with each of the
                 4 nights before, moved onto the same day, %) + k0.
  recovery     = 100·σ(h·(ln HRV − mean ln HRV of the 30 nights before)
                 + p·performance + c), from the 8th night with an HRV.
  day strain   = WHOOP's own for `whoop`; from a heart rate otherwise:
                 a·ln(1 + L/b), L = Banister's TRIMP over the cycle (sleep onset
                 to the next one), Σ x·0.64·e^(1.92x) per minute,
                 x = (HR − resting HR)/(max HR − resting HR); at most 21.
  bedtime      = wake-up − target × need ÷ efficiency − time to fall asleep
                 (target 100 % "peak", 85 % "perform"): the dashboard computes
                 it from `whoop_model_tonight`, for the wake-up the reader picks.

Mapping (SleepStats, tag `input` = the device the night comes from):
  whoop_model          one point per night and input, at its end (the wake-up,
                       as `sleep_session`): the need and its parts, the sleep
                       and time in bed used, the scores, the strain of the
                       cycle before (`strain_prev`) and of the cycle that
                       starts with this night (`day_strain`, `day_complete` 0
                       while it is still running).
  whoop_model_tonight  one point per input, at the end of its last night: what
                       the planner needs for the coming night -- the debt
                       carried, the 14-night median efficiency and time to fall
                       asleep, the need at strain 8, 12, 16 and at the strain so
                       far; and how fresh that is: when the model ran, the last
                       night's bounds and sleep against the usual, the device's
                       last heart-rate sample, the local midnight of the coming
                       wake-up's day, `partial` 1 when the device had not synced
                       the whole night yet and `cut_short` 1 when the night looks
                       cut short (a source that had not finished processing it).

Cursor: {input: the end of the last night written, epoch seconds}. A run
recomputes from there minus TRIBUTARY_WHOOP_MODEL_LOOKBACK_DAYS (late syncs,
WHOOP's rescoring), reading WARMUP_DAYS more before it (the debt and the HRV
baseline need the nights before), and rewrites those nights: the same keys, the
same points. A first or full run starts at TRIBUTARY_WHOOP_MODEL_START_DATE, or
at each input's first night, in windows of WINDOW_DAYS.
"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic_settings import SettingsConfigDict

from app import influxdb
from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Measurement, Point

PREFIX = "TRIBUTARY_WHOOP_MODEL_"
INPUTS = ("whoop", "fitbit", "garmin")
DAY = 86400
# Nights read before the first one written: 30 for the HRV baseline, and the
# debt, halved every night, has forgotten where it started by then.
WARMUP_DAYS = 40
# A first run walks the history in windows of this many days (minute heart
# rate: ~30,000 rows a week per device, read a week at a time).
WINDOW_DAYS = 120
HR_CHUNK_DAYS = 7
# A heart-rate sample stands for the minutes until the next one, up to this
# many (a watch sampling every 2 minutes; a gap longer than that is not worn).
HR_GAP_MAX_S = 300
# A cycle counts when its heart rate covers this share of it.
HR_COVERAGE_MIN = 0.7
# A night longer apart than this from the one before is not consecutive.
CONSECUTIVE_MAX_S = 36 * 3600

# The reversed constants (docs/connectors/whoop_model.md). WHOOP's algorithm,
# not a person's: a deployment may refit them (TRIBUTARY_WHOOP_MODEL_CONSTANTS,
# a JSON object overriding any of these keys).
DEFAULTS: Dict[str, Any] = {
    "need_strain": [1.289, 0.860, 2.680],      # a·(S + b)^c seconds
    "debt_r": 0.5,
    "debt_r_good": 9 / 14,
    "debt_score_cut": 87.0,
    "score": [0.4347, 1.0329, 0.3587, -73.578],  # s1 (sleep/need %), s2 (efficiency %), s3 (consistency %), s0
    "consistency": [0.938, 1.326],             # k1, k0
    "recovery": [6.628, 0.01628, -0.500],      # h, p, c
    "hr_strain": [36.93, 696.4],               # a, b of a·ln(1 + TRIMP/b)
    "typical_strain": 10.0,                    # when the cycle before has no strain
    "typical_consistency": 80.0,               # before 4 nights are known
}
# A baseline need when neither the settings nor WHOOP's own nights give one.
BASELINE_NEED_H = 7.5
# Max heart rate when neither the settings nor WHOOP's days give one.
HR_MAX = 190.0
PLAN_STRAINS = (8, 12, 16)
# A last night looks cut short (its source had not finished it: Google Health
# has held a Fitbit night ending at 01:42 until noon) when it has less than this
# share of the usual sleep, or ends this many minutes before the usual wake-up;
# "usual" = the median of the 14 nights before, once there are 3.
CUT_SHORT_SLEEP = 0.6
CUT_SHORT_WAKE_MIN = 180
USUAL_MIN_NIGHTS = 3

MODEL = Measurement(
    database="SleepStats", name="whoop_model", tags=("input",),
    fields={"need_s": float, "need_base_s": float, "need_strain_s": float, "need_debt_s": float,
            "asleep_s": float, "in_bed_s": float, "efficiency": float, "latency_s": float,
            "sleep_score": float, "consistency": float, "recovery": float,
            "strain_prev": float, "day_strain": float, "day_complete": int},
    last_wins=True,
)
TONIGHT = Measurement(
    database="SleepStats", name="whoop_model_tonight", tags=("input",),
    fields={"debt_s": float, "base_s": float, "efficiency": float, "latency_s": float,
            "need_s_strain8": float, "need_s_strain12": float, "need_s_strain16": float,
            "strain_so_far": float, "need_s_so_far": float, "partial": int,
            "run_s": int, "night_start_s": int, "night_end_s": int, "synced_s": int, "wake_day_s": int,
            "night_asleep_s": float, "usual_asleep_s": float, "cut_short": int},
    last_wins=True,
)


class WhoopModelSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # Off by default: it reads other tools' values (docs/connectors/whoop_model.md).
    enabled: bool = False
    # The devices to rebuild WHOOP's scores from, among whoop, fitbit, garmin;
    # one with no night in InfluxDB is skipped.
    inputs: str = ",".join(INPUTS)
    # Nights recomputed before the last one written (late syncs, rescoring).
    lookback_days: int = 7
    # Where a first or full run starts (YYYY-MM-DD); each input's first night when unset.
    start_date: Optional[date] = None
    # The baseline sleep need, in hours. Unset: WHOOP's own (its latest
    # need_baseline_s), else 7.5 h.
    baseline_need_h: Optional[float] = None
    # Max heart rate for the strain from a heart rate. Unset: the 99.5th
    # percentile of WHOOP's daily max HR, else 190.
    hr_max: Optional[float] = None
    # A device's median offset on time asleep and in bed against WHOOP, in
    # minutes (positive: it counts more), measured on nights both recorded. It
    # is taken off that device's nights, so its scores land on WHOOP's scale.
    fitbit_asleep_bias_min: float = 0.0
    fitbit_in_bed_bias_min: float = 0.0
    garmin_asleep_bias_min: float = 0.0
    garmin_in_bed_bias_min: float = 0.0
    # Refitted constants, a JSON object over DEFAULTS' keys.
    constants: Dict[str, Any] = {}


# ────────────────────────────────────────────────
# The models (pure functions: no InfluxDB)
# ────────────────────────────────────────────────

@dataclass
class Night:
    key: date            # the local date it ends on
    start: int           # epoch seconds
    end: int
    asleep: float        # seconds
    in_bed: float
    latency: float = 0.0
    hrv: Optional[float] = None
    rhr: Optional[float] = None


@dataclass
class Row:
    """What the model makes of one night."""
    night: Night
    need: float
    need_base: float
    need_strain: float
    need_debt: float
    efficiency: Optional[float]
    consistency: Optional[float]
    score: Optional[float]
    recovery: Optional[float]
    strain_prev: Optional[float]


def constants(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ConnectorError(f"{PREFIX}CONSTANTS: unknown key {key!r} (known: {', '.join(sorted(DEFAULTS))})")
        out[key] = value
    return out


def need_strain_s(c: Dict[str, Any], strain: Optional[float]) -> float:
    """The need a day's strain adds to the night after it, in seconds."""
    a, b, k = c["need_strain"]
    s = c["typical_strain"] if strain is None else strain
    return a * max(s + b, 0.0) ** k


def debt_s(c: Dict[str, Any], need: float, asleep: float, score: Optional[float]) -> float:
    """What last night's shortfall carries into tonight's need."""
    r = c["debt_r_good"] if (score or 0) > c["debt_score_cut"] else c["debt_r"]
    return r * max(0.0, need - asleep)


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Intersection over union of two intervals."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else 0.0


def consistency_raw(nights: List[Night], i: int) -> Optional[float]:
    """Mean overlap (%) of night i's window with each of the 4 nights before,
    moved by whole days onto night i."""
    if i < 4:
        return None
    n = nights[i]
    total = 0.0
    for j in range(i - 4, i):
        shift = round((n.start - nights[j].start) / DAY) * DAY
        total += overlap(n.start, n.end, nights[j].start + shift, nights[j].end + shift)
    return 100 * total / 4


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


def recovery(c: Dict[str, Any], ln_hrv: float, baseline_ln_hrv: List[float], score: float) -> float:
    h, p, k = c["recovery"]
    return 100 * sigmoid(h * (ln_hrv - sum(baseline_ln_hrv) / len(baseline_ln_hrv)) + p * score + k)


def apply(nights: List[Night], strain: Dict[date, Optional[float]], c: Dict[str, Any], base_s: float) -> List[Row]:
    """Night by night, in order: the debt of each night depends on the need
    and the score of the one before."""
    rows: List[Row] = []
    ln_hrv: List[float] = []
    a1, a2, a3, a0 = c["score"]
    k1, k0 = c["consistency"]
    for i, n in enumerate(nights):
        prev = rows[-1] if rows and n.start - rows[-1].night.end < CONSECUTIVE_MAX_S \
            and (n.key - rows[-1].night.key).days == 1 else None
        s_prev = strain.get(n.key - timedelta(days=1))
        ns = need_strain_s(c, s_prev)
        debt = debt_s(c, prev.need, prev.night.asleep, prev.score) if prev else 0.0
        need = base_s + ns + debt
        raw = consistency_raw(nights, i)
        cons = None if raw is None else min(100.0, k1 * raw + k0)
        eff = 100 * n.asleep / n.in_bed if n.in_bed else None
        score = None
        if eff is not None and need > 0:
            score = a1 * min(100.0, 100 * n.asleep / need) + a2 * eff \
                + a3 * (c["typical_consistency"] if cons is None else cons) + a0
            score = max(0.0, min(100.0, score))
        rec = None
        if n.hrv and n.hrv > 0:
            ln = math.log(n.hrv)
            if len(ln_hrv) >= 7 and score is not None:
                rec = recovery(c, ln, ln_hrv[-30:], score)
            ln_hrv.append(ln)
        rows.append(Row(n, need, base_s, ns, debt, eff, cons, score, rec, s_prev))
    return rows


def banister_load(samples: List[Tuple[int, float]], rhr: float, hr_max: float, until: Optional[int] = None
                  ) -> Tuple[float, float]:
    """(TRIMP, seconds covered) of heart-rate samples sorted by time, each
    standing for the time to the next one (at most HR_GAP_MAX_S)."""
    load = covered = 0.0
    span = max(1.0, hr_max - rhr)
    for k, (t, hr) in enumerate(samples):
        nxt = samples[k + 1][0] if k + 1 < len(samples) else (until if until is not None else t + 60)
        weight = max(0, min(nxt - t, HR_GAP_MAX_S))
        x = min(1.0, max(0.0, (hr - rhr) / span))
        load += x * 0.64 * math.exp(1.92 * x) * weight / 60
        covered += weight
    return load, covered


def hr_strain(c: Dict[str, Any], load: float) -> float:
    a, b = c["hr_strain"]
    return min(21.0, a * math.log1p(load / b))


@dataclass
class Tonight:
    debt: float
    efficiency: float
    latency: float
    strain_so_far: Optional[float]


def tonight(rows: List[Row], c: Dict[str, Any], strain_so_far: Optional[float]) -> Optional[Tonight]:
    """What the planner needs for the night after the last one."""
    if not rows:
        return None
    last = rows[-1]
    recent = rows[-14:]
    effs = [r.efficiency for r in recent if r.efficiency]
    return Tonight(debt=debt_s(c, last.need, last.night.asleep, last.score),
                   efficiency=median(effs) if effs else 90.0,
                   latency=median(r.night.latency for r in recent),
                   strain_so_far=strain_so_far)


def _wake_minute(n: Night, tz: ZoneInfo) -> int:
    """The night's end as minutes after the noon before it (no wrap at midnight)."""
    local = datetime.fromtimestamp(n.end, tz)
    return (local.hour * 60 + local.minute - 720) % 1440


def usual_asleep(before: List[Night]) -> Optional[float]:
    return median(n.asleep for n in before) if len(before) >= USUAL_MIN_NIGHTS else None


def cut_short(last: Night, before: List[Night], tz: ZoneInfo) -> bool:
    """Whether the last night looks cut short against the nights before it."""
    usual = usual_asleep(before)
    if usual is None:
        return False
    if last.asleep < CUT_SHORT_SLEEP * usual:
        return True
    return median(_wake_minute(n, tz) for n in before) - _wake_minute(last, tz) > CUT_SHORT_WAKE_MIN


def wake_day_s(key: date, now: int, tz: ZoneInfo) -> int:
    """Local midnight of the coming wake-up's day: the day after the last night,
    or after today once the morning is past (the last night not synced yet); a
    run between midnight and 6 am plans the night it is in."""
    run_day = datetime.fromtimestamp(now - 6 * 3600, tz).date()
    day = max(key, run_day) + timedelta(days=1)
    return int(datetime.combine(day, datetime.min.time(), tz).timestamp())


# ────────────────────────────────────────────────
# Reading InfluxDB
# ────────────────────────────────────────────────

def _rows(payload: Dict) -> List[Dict[str, Any]]:
    out = []
    for result in payload.get("results") or []:
        for series in result.get("series") or []:
            for values in series.get("values") or []:
                row = dict(zip(series.get("columns") or [], values))
                row.update(series.get("tags") or {})
                out.append(row)
    return out


def _lit(moment: int) -> str:
    return influxdb.time_literal(datetime.fromtimestamp(moment, timezone.utc))


@dataclass
class Device:
    """One input's nights, its day strain by cycle (the wake date of the night
    the cycle starts with), and when its heart rate was last seen."""
    nights: List[Night] = field(default_factory=list)
    strain: Dict[date, Optional[float]] = field(default_factory=dict)
    complete: Dict[date, bool] = field(default_factory=dict)
    synced: Optional[int] = None


class WhoopModelConnector(Connector):
    key = "whoop_model"
    label = "WHOOP models"
    description = ("WHOOP's sleep need, debt, sleep performance, consistency, recovery, strain and "
                   "bedtime, rebuilt from WHOOP's, a Fitbit's or a Garmin's own data in InfluxDB.")
    icon = "bi-calculator"
    category = "Sleep"
    # After the Whoop connector (10:35) and through the day, for the strain so far.
    default_schedule = "50 7,11,15,19 * * *"
    settings_class = WhoopModelSettings
    measurements = (MODEL, TONIGHT)
    required_env = (f"{PREFIX}ENABLED",)

    # Tests replace it: (database, statement) -> rows.
    def _select(self, database: str, statement: str) -> List[Dict[str, Any]]:
        return _rows(influxdb.query(statement, database, epoch="s"))

    def api_configured(self) -> bool:
        return bool(self.settings.enabled)

    # -- settings ----------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(app_settings.tz)

    def inputs(self) -> List[str]:
        names = [n.strip().lower() for n in self.settings.inputs.split(",") if n.strip()]
        unknown = [n for n in names if n not in INPUTS]
        if unknown:
            raise ConnectorError(f"{PREFIX}INPUTS: unknown input(s) {', '.join(unknown)} (known: {', '.join(INPUTS)})")
        return names

    def _user(self) -> str:
        user = app_settings.influxdb_user
        return f' AND "user" = {influxdb.quote(user)}' if user else ""

    def _local(self, epoch: float) -> date:
        return datetime.fromtimestamp(epoch, self.tz).date()

    # -- what WHOOP itself says ---------------------------------------------------

    def baseline_s(self) -> float:
        if self.settings.baseline_need_h:
            return self.settings.baseline_need_h * 3600
        try:
            rows = self._select("SleepStats", "SELECT last(\"need_baseline_s\") FROM \"sleep_session\" "
                                              f"WHERE \"source\" = 'whoop'{self._user()}")
        except influxdb.InfluxError:
            rows = []
        value = rows[0].get("last") if rows else None
        return float(value) if value else BASELINE_NEED_H * 3600

    def hr_max(self) -> float:
        if self.settings.hr_max:
            return float(self.settings.hr_max)
        try:
            rows = self._select("BodyStats", "SELECT percentile(\"max_hr\", 99.5) FROM \"daily_activity\" "
                                             f"WHERE \"source\" = 'whoop'{self._user()}")
        except influxdb.InfluxError:
            rows = []
        value = rows[0].get("percentile") if rows else None
        return float(value) if value else HR_MAX

    # -- one reader per input -----------------------------------------------------

    def first_night(self, name: str) -> Optional[int]:
        statement = {
            "whoop": "SELECT first(\"asleep_s\") FROM \"sleep_session\" WHERE \"source\" = 'whoop' "
                     f"AND \"kind\" = 'night'{self._user()}",
            "fitbit": "SELECT first(\"minutesAsleep\") FROM \"Sleep Summary\" WHERE \"isMainSleep\" =~ /^(True|true)$/",
            "garmin": "SELECT first(\"sleepTimeSeconds\") FROM \"SleepSummary\" "
                      "WHERE \"sleepTimeSeconds\" > 0 AND \"remSleepSeconds\" > 0",
        }[name]
        database = {"whoop": "SleepStats", "fitbit": "FitbitHealthStats", "garmin": "GarminStats"}[name]
        try:
            rows = self._select(database, statement)
        except influxdb.InfluxError:
            return None  # no such database: nothing to rebuild from
        return int(rows[0]["time"]) if rows else None

    def read(self, name: str, start: int, end: int, now: int, hr_max: float) -> Device:
        """The nights ending in [start, end), each cycle's strain (the last one
        so far, when `end` reaches now) and the calibration applied."""
        reader = {"whoop": self._read_whoop, "fitbit": self._read_fitbit, "garmin": self._read_garmin}[name]
        device = reader(start, end)
        if name != "whoop":
            self._hr_strain(name, device, min(end, now), hr_max, live=end > now)
            bias = (getattr(self.settings, f"{name}_asleep_bias_min") * 60,
                    getattr(self.settings, f"{name}_in_bed_bias_min") * 60)
            for n in device.nights:
                n.asleep = max(0.0, n.asleep - bias[0])
                n.in_bed = max(n.asleep, n.in_bed - bias[1])
        device.nights.sort(key=lambda n: n.end)
        return device

    def _keep_longest(self, nights: Iterable[Night]) -> List[Night]:
        by_key: Dict[date, Night] = {}
        for n in nights:
            if n.asleep > 0 and (n.key not in by_key or n.asleep > by_key[n.key].asleep):
                by_key[n.key] = n
        return list(by_key.values())

    def _read_whoop(self, start: int, end: int) -> Device:
        window = f"time >= {_lit(start)} AND time < {_lit(end)}"
        nights = []
        for r in self._select("SleepStats", "SELECT \"start\", \"asleep_s\", \"in_bed_s\", \"hrv_rmssd_ms\", "
                                            "\"resting_hr\" FROM \"sleep_session\" WHERE \"source\" = 'whoop' "
                                            f"AND \"kind\" = 'night'{self._user()} AND {window}"):
            if r.get("asleep_s") and r.get("start"):
                end_s = int(r["time"])
                nights.append(Night(self._local(end_s), int(r["start"]), end_s, float(r["asleep_s"]),
                                    float(r.get("in_bed_s") or 0), 0.0, r.get("hrv_rmssd_ms"), r.get("resting_hr")))
        device = Device(nights=self._keep_longest(nights))
        # A WHOOP cycle starts at sleep onset (the evening before): its day is
        # that night's wake date.
        for r in self._select("BodyStats", "SELECT \"strain\" FROM \"daily_activity\" WHERE \"source\" = 'whoop'"
                                           f"{self._user()} AND time >= {_lit(start - DAY)} AND time < {_lit(end)}"):
            if r.get("strain") is not None:
                key = self._local(int(r["time"]) + 12 * 3600)
                device.strain[key] = float(r["strain"])
                device.complete[key] = True
        return device

    def _read_fitbit(self, start: int, end: int) -> Device:
        # A Fitbit night is stamped at its start: read the day before too.
        window = f"time >= {_lit(start - DAY)} AND time < {_lit(end)}"
        daily: Dict[date, Dict[str, float]] = {}
        for measurement, column, name in (("RestingHR", "value", "rhr"), ("HRV", "dailyRmssd", "hrv")):
            for r in self._select("FitbitHealthStats", f"SELECT \"{column}\" FROM \"{measurement}\" WHERE {window}"):
                if r.get(column):
                    daily.setdefault(self._local(int(r["time"])), {})[name] = float(r[column])
        nights = []
        for r in self._select("FitbitHealthStats", "SELECT \"minutesAsleep\", \"minutesInBed\", \"minutesToFallAsleep\" "
                                                   "FROM \"Sleep Summary\" WHERE \"isMainSleep\" =~ /^(True|true)$/ "
                                                   f"AND {window}"):
            begin = int(r["time"])
            end_s = begin + 60 * int(r.get("minutesInBed") or 0)
            if not r.get("minutesAsleep") or not (start <= end_s < end):
                continue
            key = self._local(end_s)
            # Daily values are stamped at local midnight of the wake-up date.
            nights.append(Night(key, begin, end_s, 60.0 * r["minutesAsleep"], 60.0 * (r.get("minutesInBed") or 0),
                                60.0 * (r.get("minutesToFallAsleep") or 0),
                                daily.get(key, {}).get("hrv"), daily.get(key, {}).get("rhr")))
        return Device(nights=self._keep_longest(nights))

    def _read_garmin(self, start: int, end: int) -> Device:
        window = f"time >= {_lit(start)} AND time < {_lit(end)}"
        nights = []
        for r in self._select("GarminStats", "SELECT \"sleepTimeSeconds\", \"awakeSleepSeconds\", \"avgOvernightHrv\", "
                                             "\"restingHeartRate\" FROM \"SleepSummary\" WHERE \"sleepTimeSeconds\" > 0 "
                                             f"AND \"remSleepSeconds\" > 0 AND {window}"):
            end_s = int(r["time"])
            asleep = float(r["sleepTimeSeconds"])
            in_bed = asleep + float(r.get("awakeSleepSeconds") or 0)
            nights.append(Night(self._local(end_s), int(end_s - in_bed), end_s, asleep, in_bed, 0.0,
                                r.get("avgOvernightHrv"), r.get("restingHeartRate")))
        return Device(nights=self._keep_longest(nights))

    def _heart_rate(self, name: str, start: int, end: int) -> List[Tuple[int, float]]:
        database, measurement, column = {"fitbit": ("FitbitHealthStats", "HeartRate_Intraday", "value"),
                                         "garmin": ("GarminStats", "HeartRateIntraday", "HeartRate")}[name]
        samples: List[Tuple[int, float]] = []
        t = start
        while t < end:
            u = min(end, t + HR_CHUNK_DAYS * DAY)
            for r in self._select(database, f"SELECT mean(\"{column}\") FROM \"{measurement}\" WHERE "
                                            f"time >= {_lit(t)} AND time < {_lit(u)} GROUP BY time(1m) fill(none)"):
                if r.get("mean") is not None:
                    samples.append((int(r["time"]), float(r["mean"])))
            t = u
        return samples

    def _hr_strain(self, name: str, device: Device, until: int, hr_max: float, live: bool) -> None:
        """Each cycle's strain from the device's heart rate: from a night's
        onset to the next night's, or, `live`, to now for the last one (the
        strain so far)."""
        nights = sorted(device.nights, key=lambda n: n.end)
        if not nights:
            return
        now = until
        samples = self._heart_rate(name, nights[0].start, until)
        device.synced = samples[-1][0] if samples else None
        c = constants(self.settings.constants)
        rhrs: List[float] = []
        k = 0
        for i, n in enumerate(nights):
            if n.rhr:
                rhrs.append(float(n.rhr))
            closed = i + 1 < len(nights)
            if not closed and not live:
                continue  # its next night is past the window read
            t1 = nights[i + 1].start if closed else now
            if t1 - n.start > 40 * 3600:
                continue  # a night missing: not one 2-day cycle
            while k < len(samples) and samples[k][0] < n.start:
                k += 1
            j = k
            while j < len(samples) and samples[j][0] < t1:
                j += 1
            rhr = sum(rhrs[-30:]) / len(rhrs[-30:]) if rhrs else 60.0
            load, covered = banister_load(samples[k:j], rhr, hr_max, until=t1)
            if closed and covered < HR_COVERAGE_MIN * (t1 - n.start):
                continue
            device.strain[n.key] = hr_strain(c, load)
            device.complete[n.key] = closed

    # -- the run ------------------------------------------------------------------

    def points_for(self, name: str, device: Device, c: Dict[str, Any], base_s: float, write_from: int,
                   write_until: int, plan: bool, now: Optional[int] = None) -> List[Point]:
        """The points of the nights ending in [write_from, write_until), and,
        with `plan`, the planner's point of the last night."""
        rows = apply(device.nights, device.strain, c, base_s)
        rows = [r for r in rows if r.night.end < write_until]
        points = []
        for r in rows:
            if r.night.end < write_from:
                continue
            fields = {"need_s": r.need, "need_base_s": r.need_base, "need_strain_s": r.need_strain,
                      "need_debt_s": r.need_debt, "asleep_s": r.night.asleep, "in_bed_s": r.night.in_bed,
                      "efficiency": r.efficiency, "latency_s": r.night.latency, "sleep_score": r.score,
                      "consistency": r.consistency, "recovery": r.recovery, "strain_prev": r.strain_prev}
            if device.strain.get(r.night.key) is not None:
                fields["day_strain"] = device.strain[r.night.key]
                fields["day_complete"] = 1 if device.complete.get(r.night.key) else 0
            points.append(Point(MODEL, datetime.fromtimestamp(r.night.end, timezone.utc), {"input": name},
                                {k: v for k, v in fields.items() if v is not None}))
        plan = tonight(rows, c, device.strain.get(rows[-1].night.key) if rows else None) if plan else None
        if plan:
            last = rows[-1].night
            fields = {"debt_s": plan.debt, "base_s": base_s, "efficiency": plan.efficiency, "latency_s": plan.latency,
                      "partial": 1 if device.synced is not None and device.synced < last.end + 1800 else 0}
            for s in PLAN_STRAINS:
                fields[f"need_s_strain{s}"] = base_s + need_strain_s(c, s) + plan.debt
            if plan.strain_so_far is not None:
                fields["strain_so_far"] = plan.strain_so_far
                fields["need_s_so_far"] = base_s + need_strain_s(c, plan.strain_so_far) + plan.debt
            # How fresh it is: the dashboard shows these beside the bedtime.
            now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
            before = [r.night for r in rows[-1 - 14:-1]]
            fields.update({"run_s": now, "night_start_s": last.start, "night_end_s": last.end,
                           "wake_day_s": wake_day_s(last.key, now, self.tz), "night_asleep_s": last.asleep,
                           "cut_short": 1 if cut_short(last, before, self.tz) else 0})
            if device.synced is not None:
                fields["synced_s"] = device.synced
            usual = usual_asleep(before)
            if usual is not None:
                fields["usual_asleep_s"] = usual
            points.append(Point(TONIGHT, datetime.fromtimestamp(last.end, timezone.utc), {"input": name}, fields))
        return points

    def sync(self, cursor: Any) -> SyncResult:
        if not influxdb.is_configured():
            raise ConnectorError("It reads InfluxDB: set TRIBUTARY_INFLUXDB_URL.")
        names = self.inputs()
        c = constants(self.settings.constants)
        now = int(datetime.now(timezone.utc).timestamp())
        base_s, hr_max = self.baseline_s(), self.hr_max()
        state: Dict[str, int] = dict(cursor or {})
        lookback = max(0, self.settings.lookback_days) * DAY
        points: List[Point] = []
        summary: List[str] = []
        for name in names:
            if name in state:
                write_from = int(state[name]) - lookback
            elif self.settings.start_date:
                write_from = int(datetime.combine(self.settings.start_date, datetime.min.time(), self.tz).timestamp())
            else:
                first = self.first_night(name)
                if first is None:
                    continue
                write_from = first
            count, last_end = 0, None
            # Window by window, each read with its warm-up before it and two
            # days after it (the next night closes its last cycle); the last
            # window reaches now and carries the planner's point.
            t = write_from
            while t <= now:
                u = min(now + 1, t + WINDOW_DAYS * DAY)
                device = self.read(name, t - WARMUP_DAYS * DAY, u + 2 * DAY, now, hr_max)
                window = self.points_for(name, device, c, base_s, t, u, plan=u > now, now=now)
                points.extend(window)
                count += sum(1 for p in window if p.measurement is MODEL)
                ends = [n.end for n in device.nights if n.end < u]
                if ends:
                    last_end = max(last_end or 0, max(ends))
                t = u
            if last_end:
                state[name] = last_end
            summary.append(f"{name}: {count} nights")
        return SyncResult(points=points, cursor=state, summary=summary or ["no input has a night yet"])
