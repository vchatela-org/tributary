"""WHOOP models: the reversed formulas on known values, and the connector on a
synthetic InfluxDB (invented nights and heart rates, nobody's)."""
import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import connectors
from app.config import settings
from app.connectors import whoop_model as wm
from app.connectors.base import ConnectorError
from app.points import encode

PARIS = ZoneInfo("Europe/Paris")
C = wm.constants()


def night(day: int, asleep_h=7.0, in_bed_h=7.5, hrv=None, start_hour=23, month_start=date(2026, 1, 1)):
    """Night whose onset is at start_hour on the (day - 1)th day counted from month_start (day 2: its first)."""
    first = month_start + timedelta(days=day - 2)
    onset = datetime(first.year, first.month, first.day, start_hour, 0, tzinfo=PARIS)
    end = onset + timedelta(hours=in_bed_h)
    return wm.Night(end.date(), int(onset.timestamp()), int(end.timestamp()), asleep_h * 3600, in_bed_h * 3600,
                    0.0, hrv, None)


# -- the formulas -----------------------------------------------------------------------

def test_need_is_baseline_plus_strain_plus_debt():
    rows = wm.apply([night(2), night(3)], {date(2026, 1, 1): 10.0, date(2026, 1, 2): 14.0}, C, 27000)
    for r in rows:
        assert r.need == pytest.approx(r.need_base + r.need_strain + r.need_debt)
    a, b, k = C["need_strain"]
    assert rows[0].need_strain == pytest.approx(a * (10 + b) ** k)
    assert rows[1].need_strain == pytest.approx(a * (14 + b) ** k)
    # More strain, more need: about 12 min at 10, 30 at 14.
    assert 600 < rows[0].need_strain < 900 < 1500 < rows[1].need_strain < 2100


def test_debt_is_half_the_shortfall_or_nine_fourteenths_after_a_good_night():
    rows = wm.apply([night(2, asleep_h=5.0, in_bed_h=8.0), night(3)], {}, C, 27000)
    first = rows[0]
    assert first.score <= C["debt_score_cut"]
    assert rows[1].need_debt == pytest.approx(0.5 * (first.need - first.night.asleep))
    assert wm.debt_s(C, 30000, 27000, 90.0) == pytest.approx(9 / 14 * 3000)
    assert wm.debt_s(C, 30000, 31000, 50.0) == 0.0  # slept more than needed: no debt


def test_a_missing_night_carries_no_debt():
    rows = wm.apply([night(2, asleep_h=4.0), night(4)], {}, C, 27000)
    assert rows[1].need_debt == 0.0


def test_consistency_is_the_overlap_with_the_four_nights_before():
    same = [night(d) for d in range(2, 8)]
    assert wm.consistency_raw(same, 3) is None
    assert wm.consistency_raw(same, 4) == pytest.approx(100.0)
    shifted = same[:4] + [night(6, start_hour=22)]   # an hour earlier, same length: 6.5/8.5 of each window
    assert wm.consistency_raw(shifted, 4) == pytest.approx(100 * 6.5 / 8.5)


def test_recovery_is_hrv_against_its_own_baseline():
    nights = [night(d, hrv=50.0) for d in range(2, 12)]
    rows = wm.apply(nights, {}, C, 27000)
    assert [r.recovery is None for r in rows][:7] == [True] * 7
    h, p, k = C["recovery"]
    r = rows[8]
    assert r.recovery == pytest.approx(100 / (1 + math.exp(-(p * r.score + k))))
    higher = wm.apply(nights[:9] + [night(11, hrv=60.0)], {}, C, 27000)[-1]
    assert higher.recovery > rows[-1].recovery


def test_strain_from_a_heart_rate():
    t0 = 1_800_000_000
    resting = [(t0 + 60 * i, 55.0) for i in range(600)]
    assert wm.banister_load(resting, 55, 190) == (0.0, 600 * 60.0)
    hard = [(t0 + 60 * i, 190.0) for i in range(60)]
    load, covered = wm.banister_load(hard, 55, 190)
    assert load == pytest.approx(60 * 0.64 * math.exp(1.92)) and covered == 3600
    # A sample stands for the time to the next one, at most 5 minutes: a gap is not worn.
    _, gappy = wm.banister_load([(t0, 100.0), (t0 + 3600, 100.0)], 55, 190, until=t0 + 3660)
    assert gappy == 300 + 60
    assert wm.hr_strain(C, 0) == 0 and wm.hr_strain(C, 1e9) == 21.0


def test_tonight_needs_the_last_nights_debt_and_the_usual_efficiency():
    rows = wm.apply([night(d, asleep_h=6.0, in_bed_h=8.0) for d in range(2, 6)], {}, C, 27000)
    plan = wm.tonight(rows, C, 7.5)
    assert plan.efficiency == pytest.approx(75.0)
    assert plan.debt == pytest.approx(wm.debt_s(C, rows[-1].need, 6 * 3600, rows[-1].score))


def test_a_night_with_too_little_sleep_or_an_early_end_looks_cut_short():
    usual = [night(d, asleep_h=7.5, in_bed_h=8.0) for d in range(2, 9)]   # 23:00 to 07:00
    assert not wm.cut_short(night(9, asleep_h=7.0, in_bed_h=8.0), usual, PARIS)
    # Google Health's Fitbit night ending at 01:42: little sleep, a wake-up hours early.
    assert wm.cut_short(night(9, asleep_h=2.5, in_bed_h=2.7), usual, PARIS)
    # Enough sleep but ending more than 3 h before the usual wake-up (an early night is fine).
    assert wm.cut_short(night(9, asleep_h=4.6, in_bed_h=4.8, start_hour=22), usual, PARIS)
    assert not wm.cut_short(night(9, asleep_h=7.0, in_bed_h=7.5, start_hour=21), usual, PARIS)
    # Fewer than 3 nights before: nothing to compare with.
    assert not wm.cut_short(night(9, asleep_h=1.0), usual[:2], PARIS)
    assert wm.usual_asleep(usual) == 7.5 * 3600 and wm.usual_asleep(usual[:2]) is None


def test_the_wake_up_day_is_the_one_after_the_last_night_or_after_today():
    def at(y, m, d, h, mi=0):
        return int(datetime(y, m, d, h, mi, tzinfo=PARIS).timestamp())
    midnight_21 = at(2026, 11, 21, 0)
    # Last night synced (it ended this morning): tonight's wake-up is tomorrow.
    assert wm.wake_day_s(date(2026, 11, 20), at(2026, 11, 20, 7, 50), PARIS) == midnight_21
    # Not synced yet (the last night is the one before): still tomorrow.
    assert wm.wake_day_s(date(2026, 11, 19), at(2026, 11, 20, 11, 50), PARIS) == midnight_21
    # Past midnight, before sleeping: the wake-up is this morning's.
    assert wm.wake_day_s(date(2026, 11, 20), at(2026, 11, 21, 0, 30), PARIS) == midnight_21


def test_constants_override_only_known_keys():
    assert wm.constants({"debt_r": 0.4})["debt_r"] == 0.4
    with pytest.raises(ConnectorError):
        wm.constants({"nope": 1})


# -- the connector on a synthetic InfluxDB ----------------------------------------------

class FakeStore:
    """Answers the connector's SELECTs from invented series, honouring the time window."""

    def __init__(self, days=12, hr=True):
        self.statements = []
        self.whoop, self.strain, self.fitbit, self.daily, self.hr = [], [], [], [], []
        # The last `days` nights, the last one this morning.
        first = datetime.now(PARIS).date() - timedelta(days=days)
        for d in range(2, 2 + days):
            n = night(d, asleep_h=6.5 + (d % 3) * 0.5, in_bed_h=8.0, hrv=45.0 + d, month_start=first)
            self.whoop.append({"time": n.end, "start": n.start, "asleep_s": int(n.asleep), "in_bed_s": int(n.in_bed),
                               "hrv_rmssd_ms": n.hrv, "resting_hr": 52.0})
            self.strain.append({"time": n.start, "strain": 6.0 + d % 5})
            self.fitbit.append({"time": n.start, "minutesAsleep": int(n.asleep / 60) + 20,
                                "minutesInBed": int(n.in_bed / 60), "minutesToFallAsleep": 5})
            midnight = int(datetime.combine(n.key, datetime.min.time(), PARIS).timestamp())
            self.daily.append({"time": midnight, "value": 58.0, "dailyRmssd": 40.0 + d})
            if hr:
                self.hr += [{"time": n.start + 60 * i, "mean": 60.0 + (30 if 600 < i < 660 else 0)}
                            for i in range(0, 24 * 60) if n.start + 60 * i < datetime.now().timestamp()]

    def __call__(self, database, statement):
        self.statements.append((database, statement))
        lo, hi = (re.findall(r"time >= '([^']+)'", statement) or [None])[0], \
            (re.findall(r"time < '([^']+)'", statement) or [None])[0]
        lo = lo and datetime.fromisoformat(lo.replace("Z", "+00:00")).timestamp()
        hi = hi and datetime.fromisoformat(hi.replace("Z", "+00:00")).timestamp()

        def window(rows):
            return [r for r in rows if (lo is None or r["time"] >= lo) and (hi is None or r["time"] < hi)]
        if "last(\"need_baseline_s\")" in statement:
            return [{"time": 0, "last": 27000}]
        if "percentile(\"max_hr\"" in statement:
            return [{"time": 0, "percentile": 190.0}]
        if statement.startswith("SELECT first("):
            source = {"SleepStats": self.whoop, "FitbitHealthStats": self.fitbit}.get(database, [])
            return [{"time": source[0]["time"], "first": 1}] if source else []
        if "FROM \"sleep_session\"" in statement:
            return window(self.whoop)
        if "FROM \"daily_activity\"" in statement:
            return window(self.strain)
        if "FROM \"Sleep Summary\"" in statement:
            return window(self.fitbit)
        if "FROM \"RestingHR\"" in statement or "FROM \"HRV\"" in statement:
            return window(self.daily)
        if "FROM \"HeartRate_Intraday\"" in statement:
            return window(self.hr)
        return []


@pytest.fixture()
def model(monkeypatch):
    connector = connectors.get("whoop_model")
    monkeypatch.setattr(connector.settings, "enabled", True)
    monkeypatch.setattr(connector.settings, "inputs", "whoop,fitbit")
    monkeypatch.setattr(settings, "influxdb_url", "http://127.0.0.1:9")
    store = FakeStore()
    monkeypatch.setattr(connector, "_select", store)
    return connector, store


def test_it_is_off_until_enabled():
    assert not connectors.get("whoop_model").api_configured()


def test_a_run_writes_a_night_per_input_and_one_plan(model):
    connector, _store = model
    result = connector.sync(None)
    nights = [p for p in result.points if p.measurement is wm.MODEL]
    plans = [p for p in result.points if p.measurement is wm.TONIGHT]
    assert {p.tags["input"] for p in nights} == {"whoop", "fitbit"}
    assert len([p for p in nights if p.tags["input"] == "whoop"]) == 12
    assert sorted(p.tags["input"] for p in plans) == ["fitbit", "whoop"]
    for p in nights:
        f = p.fields
        assert f["need_s"] == pytest.approx(f["need_base_s"] + f["need_strain_s"] + f["need_debt_s"])
        assert f["need_base_s"] == 27000
    # Fitbit's cycles get a strain from the heart rate; the last one is the strain so far.
    fitbit = sorted((p for p in nights if p.tags["input"] == "fitbit"), key=lambda p: p.time)
    assert all("day_strain" in p.fields for p in fitbit)
    assert fitbit[-1].fields["day_complete"] == 0 and fitbit[0].fields["day_complete"] == 1
    # The planner's point says how fresh it is.
    for plan in plans:
        f = plan.fields
        assert f["night_end_s"] == int(plan.time.timestamp()) and f["night_start_s"] < f["night_end_s"]
        assert f["run_s"] >= f["night_end_s"] and f["cut_short"] == 0
        assert f["wake_day_s"] > f["night_end_s"] and f["usual_asleep_s"] > 0
        assert ("synced_s" in f) == (plan.tags["input"] == "fitbit")
    # The schema accepts every point.
    lines = encode(result.points, "whoop_model", "someone")["SleepStats"]
    assert any(line.startswith("whoop_model,input=fitbit,source=whoop_model,user=someone ") for line in lines)
    assert result.cursor == {"whoop": max(r["time"] for r in _store.whoop),
                             "fitbit": max(r["time"] for r in _store.fitbit) + 8 * 3600}


def test_a_device_bias_is_taken_off_its_nights(model, monkeypatch):
    connector, _store = model
    raw = {p.time: p.fields["asleep_s"] for p in connector.sync(None).points
           if p.measurement is wm.MODEL and p.tags["input"] == "fitbit"}
    monkeypatch.setattr(connector.settings, "fitbit_asleep_bias_min", 20.0)
    fixed = {p.time: p.fields["asleep_s"] for p in connector.sync(None).points
             if p.measurement is wm.MODEL and p.tags["input"] == "fitbit"}
    assert raw and all(fixed[t] == raw[t] - 1200 for t in raw)


def test_a_later_run_starts_from_the_cursor_minus_the_lookback(model):
    connector, store = model
    cursor = connector.sync(None).cursor
    store.statements.clear()
    result = connector.sync(cursor)
    written = [p for p in result.points if p.measurement is wm.MODEL and p.tags["input"] == "whoop"]
    assert len(written) == 8  # the last 7 days and the night on the cursor
    assert not any(s.startswith("SELECT first(") for _db, s in store.statements)


def test_an_unknown_input_is_an_error(model, monkeypatch):
    connector, _store = model
    monkeypatch.setattr(connector.settings, "inputs", "whoop,oura")
    with pytest.raises(ConnectorError, match="oura"):
        connector.sync(None)


def test_an_input_with_no_night_is_skipped(model, monkeypatch):
    connector, store = model
    store.fitbit.clear()
    result = connector.sync(None)
    assert {p.tags["input"] for p in result.points} == {"whoop"}
