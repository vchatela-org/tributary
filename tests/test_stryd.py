"""Stryd: the Footpath of an export Stryd sends on request, imported in the UI."""
import json
import math
import zipfile

import pytest

from app import connectors, runner
from app.config import settings
from app.connectors.stryd import DISTANCE_GAP_S, LOOP_POINTS, Odometer, pace_band
from tests.fakes import epoch, parse

USER = "alice"
# 2026-03-01 08:15:00 UTC = 09:15 in Paris (CET).
START = epoch(2026, 3, 1, 8, 15)
POD = "AA:BB:CC:DD:EE:01"
ACCOUNT = "account-not-a-real-id-0042"


def path(lift=0.12, back=0.5, reach=0.4, width=0.06, n=108):
    """A plausible cycle from mid-stance to mid-stance: on the ground for its
    first and last fifth, back, up, forward, down; (0, 0, 0) at both ends."""
    out = []
    for i in range(n):
        f = i / (n - 1)
        swing = min(max((f - 0.2) / 0.6, 0.0), 1.0)  # 0 on the ground before, 1 after
        x = -back * math.sin(math.pi * swing) if swing < 0.5 else reach * math.sin(math.pi * swing)
        if f < 0.2:
            x = -back * f / 0.2 * 0.3
        elif f > 0.8:
            x = reach * 0.5 * (1 - (f - 0.8) / 0.2)
        z = lift * math.sin(math.pi * swing)
        y = -width * math.sin(math.pi * swing)
        out.append({"x": x, "y": y, "z": z})
    out[0] = out[-1] = {"x": 0, "y": 0, "z": 0}
    return out


def step(t, side, speed=3.4, lift=0.12, new_format=True):
    s = {"side": side, "timestamp": int(t), "speed": speed, "power": 240, "incline": -0.01, "sample_freq": 160,
         "sample_cnt": 70, "scale_x": 0.9, "scale_y": 0.9, "scale_z": 0.6,
         "positions": path(lift=lift), "velocities": [{"x": 0, "y": 0, "z": 0}] * 108}
    if new_format:
        s.update({"timestamp_frac": round(t - int(t), 3), "step_modulus": 2,
                  "angles": [{"x": 0, "y": 0, "z": 0}] * 108, "scale_velocity": {"x": 1, "y": 1, "z": 1}})
    return s


def footdata(steps, start=START):
    return {"user_id": ACCOUNT, "id": 0, "timestamp": start, "elapsed_time": 600, "pod_id": POD,
            "file_path": "phone_offline/someone/1700000000.footdata", "deleted": False, "foot_data_list": steps}


def a_run(start=START, steps=24, gap=7.0, **kw):
    """Both feet, a step each every `gap` seconds."""
    out = []
    for i in range(steps):
        out.append(step(start + i * gap + 0.25, 1, **kw))
        out.append(step(start + i * gap + 0.6, 2, **kw))
    return out


def write_export(tmp_path, files, name="stryd-export.zip"):
    target = tmp_path / name
    with zipfile.ZipFile(target, "w") as z:
        for member, doc in files.items():
            z.writestr(f"someone/{member}", json.dumps(doc))
    return target


@pytest.fixture()
def connector(monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return connectors.get("stryd")


def _import(db, connector, path, **options):
    return runner.run(db, connector, file=str(path), filename=path.name, trigger="upload", **options)


def _lines(influx, measurement):
    return [parse(line) for line in influx.written_lines() if line.startswith(measurement + ",")]


def test_pace_bands_are_15_seconds_wide_and_open_at_both_ends():
    assert pace_band(1000 / 270) == "4:30"   # 4:30 per km
    assert pace_band(1000 / 284.9) == "4:30"
    assert pace_band(1000 / 285) == "4:45"
    assert pace_band(10.0) == "2:30"         # 1:40 per km: the fastest band
    assert pace_band(2.0) == "8:00"          # 8:20 per km: the slowest


def test_an_export_writes_each_step_and_each_run_s_loop_per_foot(connector, influx, db, tmp_path):
    export = write_export(tmp_path, {"5000000000000001_footdata_1700000100.json": footdata(a_run())})
    run = _import(db, connector, export)
    assert run.status == "ok", run.message
    assert run.message == ("Imported stryd-export.zip, 1 Footpath file(s), 48 running step(s) (24 left, 24 right; "
                           "0 repeated in another copy), 1 run(s), 2 typical loop(s), 3 loop(s) km by km: "
                           f"{48 + 5 * LOOP_POINTS} point(s) written (BodyStats {48 + 5 * LOOP_POINTS}).")

    steps = _lines(influx, "footpath_step")
    assert len(steps) == 48
    measurement, tags, fields, ts = steps[0]
    assert tags == {"source": "stryd", "user": USER, "side": "left", "pace": "4:45"}  # 3.4 m/s = 4:54/km
    assert ts == START
    assert set(fields) == {"lift_cm", "back_cm", "reach_cm", "width_cm", "touchdown_cm", "speed_mps", "power_w",
                           "incline"}
    assert float(fields["lift_cm"]) == pytest.approx(12.0, abs=0.1)
    assert float(fields["back_cm"]) == pytest.approx(50.0, abs=0.5)
    assert float(fields["reach_cm"]) == pytest.approx(40.0, abs=0.5)
    assert float(fields["width_cm"]) == pytest.approx(6.0, abs=0.1)
    assert 0 < float(fields["touchdown_cm"]) <= 40
    assert fields["speed_mps"] == "3.4" and fields["power_w"] == "240.0" and fields["incline"] == "-0.01"

    loops = _lines(influx, "footpath_loop")
    assert len(loops) == 2 * LOOP_POINTS
    left = sorted((ts, f) for _m, t, f, ts in loops if t["side"] == "left")
    assert {t["run"] for _m, t, _f, _ts in loops} == {"2026-03-01 09:15"}  # its local start
    assert [ts for ts, _f in left] == [START + i for i in range(LOOP_POINTS)]
    assert [f["phase"] for _ts, f in left] == [f"{i}i" for i in range(LOOP_POINTS)]
    first, top = left[0][1], max((f for _ts, f in left), key=lambda f: float(f["z_cm"]))
    assert (first["x_cm"], first["y_cm"], first["z_cm"]) == ("0.0", "0.0", "0.0")
    assert float(top["z_cm"]) == pytest.approx(12.0, abs=0.2)
    assert first["steps"] == "24i" and first["speed_mps"] == "3.4"


def test_the_distance_is_the_speed_summed_over_time_a_long_gap_adding_none():
    odometer = Odometer([(0, 3.0), (10, 3.0), (20, 4.0), (20 + DISTANCE_GAP_S + 1, 4.0), (60, 2.0)])
    assert odometer.at(-5) == 0
    assert odometer.at(10) == 30
    assert odometer.at(15) == 30            # the last sample's distance until the next one
    assert odometer.at(20) == 65            # trapezoid: 10 s at 3.5 m/s
    assert odometer.at(20 + DISTANCE_GAP_S + 1) == 65  # the pod went quiet: no distance
    assert odometer.at(60) == pytest.approx(65 + 9 * 3.0)


def test_each_500_m_of_a_run_gets_its_own_loop_per_foot(connector, influx, db, tmp_path):
    """Both feet every 7 s at 3.4 m/s (23.8 m a step), lifting less after the
    30th step: 1.405 km, three segments; the third one lifts 8 cm, the first 12."""
    steps = []
    for i in range(60):
        lift = 0.12 if i < 30 else 0.08
        steps += [step(START + i * 7 + 0.25, 1, lift=lift), step(START + i * 7 + 0.6, 2, lift=lift)]
    run = _import(db, connector, write_export(tmp_path, {"a_footdata_0.json": footdata(steps)}))
    assert "1 run(s), 2 typical loop(s), 6 loop(s) km by km" in run.message, run.message

    rows = _lines(influx, "footpath_segment")
    assert len(rows) == 6 * LOOP_POINTS
    assert {t["run"] for _m, t, _f, _ts in rows} == {"2026-03-01 09:15"}
    left = sorted((ts, f) for _m, t, f, ts in rows if t["side"] == "left")
    # Second-apart points from the run's start, segment after segment.
    assert [ts for ts, _f in left] == [START + i for i in range(3 * LOOP_POINTS)]
    heads = [f for ts, f in left if f["phase"] == "0i"]
    assert [(f["km"], f["km_end"]) for f in heads] == [("0.0", "0.5"), ("0.5", "1.0"), ("1.0", "1.405")]
    assert [f["steps"] for f in heads] == ["22i", "21i", "17i"]
    assert [f["elapsed_s"] for f in heads] == ["0i", "147i", "294i"]
    assert float(heads[0]["lift_cm"]) == pytest.approx(12.0, abs=0.1)
    assert float(heads[2]["lift_cm"]) == pytest.approx(8.0, abs=0.1)
    assert {"back_cm", "reach_cm", "width_cm", "touchdown_cm", "speed_mps"} <= set(heads[0])
    # The form figures are on the segment's first point only; the loop on all.
    assert all("lift_cm" not in f for _ts, f in left if f["phase"] != "0i")
    last = [f for _ts, f in left[2 * LOOP_POINTS:]]
    assert max(float(f["z_cm"]) for f in last) == pytest.approx(8.0, abs=0.2)


def test_the_copies_of_a_run_merge_and_an_import_again_rewrites_the_same_points(connector, influx, db, tmp_path):
    """The export holds a run up to five times, whole or in part, under other
    names and in both formats: a step is written once, and importing the
    export again (or in parts) lands on the same keys."""
    whole = a_run()
    older_copy = [dict(s) for s in whole[10:]]  # a later, partial copy, synced from the phone
    for s in older_copy:
        s.pop("timestamp_frac")
    export = write_export(tmp_path, {"1700000000_footdata_2.json": footdata(whole),
                                     "5000000000000002_footdata_1700000200.json": footdata(older_copy)})
    run = _import(db, connector, export)
    assert "48 running step(s) (24 left, 24 right; 38 repeated in another copy)" in run.message
    first = sorted(influx.written_lines())

    influx.writes.clear()
    part = write_export(tmp_path, {"5000000000000002_footdata_1700000200.json": footdata(older_copy)}, "part.zip")
    _import(db, connector, part)
    again = influx.written_lines()
    # The loops are drawn from the part of the run the upload holds (a run's copies go in the same upload).
    assert set(again) - set(first) <= {line for line in again if line.startswith(("footpath_loop,",
                                                                                  "footpath_segment,"))}
    assert {line for line in again if line.startswith("footpath_step,")} <= set(first)


def test_walking_stops_and_broken_paths_are_left_out(connector, influx, db, tmp_path):
    steps = a_run(steps=12)
    steps.append(step(START + 200, 1, speed=1.4))       # walking
    steps.append(step(START + 201, 2, speed=0))         # a stop
    broken = step(START + 202, 1)
    broken["positions"] = broken["positions"][:5]        # too short to be a cycle
    steps.append(broken)
    steps.append({**step(START + 203, 3)})               # no such foot
    run = _import(db, connector, write_export(tmp_path, {"a_footdata_0.json": footdata(steps)}))
    assert "24 running step(s)" in run.message
    assert len(_lines(influx, "footpath_step")) == 24


def test_a_long_gap_starts_another_run_and_a_short_run_gets_no_loop(connector, influx, db, tmp_path):
    later = START + 3 * 3600
    steps = a_run(steps=12) + a_run(start=later, steps=5)  # the second run: 5 steps a foot, too few for a loop
    run = _import(db, connector, write_export(tmp_path, {"a_footdata_0.json": footdata(steps)}))
    assert "2 run(s), 2 typical loop(s)" in run.message
    assert {t["run"] for _m, t, _f, _ts in _lines(influx, "footpath_loop")} == {"2026-03-01 09:15"}


def test_a_single_json_file_is_taken_too(connector, influx, db, tmp_path):
    target = tmp_path / "1700000300_footdata_0.json"
    target.write_text(json.dumps(footdata(a_run(steps=12))))
    run = _import(db, connector, target)
    assert run.status == "ok" and "24 running step(s)" in run.message


def test_a_file_that_is_not_a_footpath_export_is_refused(connector, influx, db, tmp_path):
    export = write_export(tmp_path, {"notes.json": {"hello": "world"}})
    run = _import(db, connector, export)
    assert run.status == "error" and "no Footpath step" in run.message
    assert influx.written_lines() == []


def test_the_account_the_pods_and_the_phone_s_paths_never_leave(connector, influx, db, tmp_path, caplog):
    """The export names the account, the pods' Bluetooth addresses and the
    phone's file paths: none of them reaches InfluxDB, the run or the logs."""
    export = write_export(tmp_path, {"a_footdata_0.json": footdata(a_run(steps=12))})
    run = _import(db, connector, export, dry_run=True)
    run = _import(db, connector, write_export(tmp_path, {"a_footdata_0.json": footdata(a_run(steps=12))}))
    seen = "\n".join(influx.written_lines()) + run.message + (run.preview or "") + caplog.text
    for private in (ACCOUNT, POD, "phone_offline", "someone"):
        assert private not in seen, private
