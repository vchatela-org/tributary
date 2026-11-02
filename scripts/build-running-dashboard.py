#!/usr/bin/env python3
"""Writes grafana/dashboards/running.json: a Garmin watch's runs, as garmin-grafana
writes them (ActivitySummary, ActivityLap, ActivityGPS, VO2_Max), with the
recovery that goes with them (resting heart rate and HRV, from their pickers).

Every query is written here: edit this file, run
`python3 scripts/build-running-dashboard.py`, commit both files
(tests/test_dashboards.py checks they agree).

A run is an activity whose type contains "run" (running, trail_running,
treadmill_running, indoor_running...) and that covers at least 1 km: shorter
ones are a warm-up or a watch started by mistake.

The best 10 km of a run is its fastest 10 consecutive 1 km laps: a moving
window over the laps of each activity (ActivityID is a tag, so each activity is
its own series), kept when the window covers 10 km to within 50 m. It needs the
watch's 1 km auto-lap; a run with manual laps, or imported without its laps,
lists its totals without a best 10 km.
"""

import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "running.json"

GARMIN = {"type": "influxdb", "uid": "${garmin}"}
SLEEP = {"type": "influxdb", "uid": "${sleep}"}
FITBIT = {"type": "influxdb", "uid": "${fitbit}"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}

# Everything on this dashboard but the recovery row is the watch's: garmin's
# colour (docs/dashboards-design.md, rule 2). Trends are the same hue, drawn as
# a line over the runs' points.
ORANGE = "#d95926"
# Two series that are not sources (aerobic and anaerobic training effect).
BLUE, AQUA = "#3987e5", "#199e70"
# Easy, moderate, hard: one hue, light to dark (an ordinal ramp, validated for
# light and dark surfaces).
EASY, MODERATE, HARD = "#86b6ef", "#2a78d6", "#184f95"
SOURCE_COLOURS = {"garmin": "#d95926", "whoop": "#9085e9", "sleep_as_android": "#199e70", "fitbit": "#d55181"}

RUN = '"activityType" =~ /run/ AND "distance" >= 1000'
RUNS = f'FROM "ActivitySummary" WHERE {RUN}'
LAPS = 'FROM "ActivityLap" WHERE "Sport" = \'running\''
GPS = 'FROM "ActivityGPS" WHERE "ActivityName" =~ /run/'
WEEK = "GROUP BY time(1w, -3d) fill(0) tz('$tz')"
DAY = "GROUP BY time(1d) fill(none) tz('$tz')"
TEN_K = 10
GARMIN_NIGHT = '"sleepTimeSeconds" > 0 AND "remSleepSeconds" > 0'


def best(km, extra_window_fields=True):
    """The fastest `km` consecutive 1 km laps of each run, with what the laps
    measured over the same window: one row per activity, at its window's last lap."""
    n = km
    fields = [
        f'moving_average("Elapsed_Time", {n}) / moving_average("Distance", {n}) * {km * 1000} AS "t"',
        f'moving_average("Distance", {n}) * {n} AS "d"',
    ]
    if extra_window_fields:
        fields += [
            f'moving_average("Avg_HR", {n}) AS "hr"',
            f'moving_average("Avg_Step_Length", {n}) / 1000 AS "step"',
            f'moving_average("Avg_Stance_Time", {n}) AS "gct"',
            f'moving_average("Avg_Vertical_Oscillation", {n}) / 10 AS "vo"',
            f'moving_average("Avg_Vertical_Ratio", {n}) AS "vr"',
            f'moving_average("Ascent", {n}) * {n} AS "ascent"',
            f'moving_average("Avg_Temperature", {n}) AS "temp"',
        ]
    inner = f'SELECT {", ".join(fields)} {LAPS} AND $timeFilter GROUP BY "ActivityID", "ActivitySelector"'
    window = f'"d" >= {km * 1000 - 50} AND "d" <= {km * 1000 + 50}'
    return inner, window


def best_rows(select):
    """The per-run best 10 km: `select` reads the min() row's fields."""
    inner, window = best(TEN_K)
    return f'SELECT {select} FROM ({inner}) WHERE {window} GROUP BY "ActivityID"'


BEST_SELECT = 'min("t") AS "best", "hr", "step", "gct", "vo", "vr", "ascent", "temp"'


_ids = iter(range(1, 1000))


# An activity whose type was changed in Garmin Connect keeps its old copy, under
# its old ActivitySelector tag: two ActivitySummary rows, laps and seconds per
# activity. Each summary query reads one row per activity (the max() selector,
# with the fields of its row), and the laps are windowed per copy.
SUMMARY_FIELDS = ('max("distance") AS "distance", "elapsedDuration", "movingDuration", "averageHR", "maxHR", '
                  '"aerobicTrainingEffect", "anaerobicTrainingEffect", "activityTrainingLoad", "calories", '
                  '"elevationGain", "activityName", "activityType"')
_RUNS_AT = re.compile(re.escape(RUNS) + r"((?: AND (?:(?! GROUP BY).)*?)?)(?= GROUP BY|$)")
_TIME = re.compile(r"\$timeFilter|time >= \S+ - \w+ AND time <= \$\{__to\}ms")


def once_per_activity(query):
    def sub(m):
        conditions = m.group(1)
        times = _TIME.findall(conditions)
        outer = f" WHERE {times[0]}" if times else ""
        return (f'FROM (SELECT {SUMMARY_FIELDS} FROM "ActivitySummary" WHERE {RUN}{conditions} '
                f'GROUP BY "ActivityID"){outer}')
    return _RUNS_AT.sub(sub, query)


def target(query, alias=None, ds=GARMIN, fmt="time_series", ref=None, hide=False):
    query = once_per_activity(query)
    t = {"refId": ref, "datasource": ds, "rawQuery": True, "resultFormat": fmt, "query": query}
    if alias:
        t["alias"] = alias
    if hide:
        t["hide"] = True
    return t


def refs(targets):
    for i, t in enumerate(targets):
        t["refId"] = t["refId"] or chr(ord("A") + i)
    return targets


def colour(name, hex_, regex=False):
    return {"matcher": {"id": "byRegexp" if regex else "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hex_}}]}


def text_tile():
    return {"mode": "fixed", "fixedColor": "text"}


def row(title, y, description=""):
    return {"type": "row", "title": title, "id": next(_ids), "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": [], **({"description": description} if description else {})}


def stat(title, description, query, unit, pos, decimals=None, ds=GARMIN, fields="", calc="lastNotNull", targets=None,
         transformations=None):
    defaults = {"unit": unit, "color": text_tile(), "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    p = {"type": "stat", "title": title, "id": next(_ids), "description": description, "datasource": ds,
         "gridPos": pos, "fieldConfig": {"defaults": defaults, "overrides": []},
         "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto",
                     "textMode": "value", "wideLayout": True, "showPercentChange": False,
                     "reduceOptions": {"calcs": [calc], "fields": fields, "values": False}},
         "targets": refs(targets or [target(query, ds=ds)])}
    if transformations:
        p["transformations"] = transformations
    return p


def timeseries(title, description, targets, unit, pos, overrides=(), decimals=None, draw="points", stack=None,
               ds=GARMIN, min_=None, color=None, legend=True, thresholds=None, extra=None):
    custom = {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 80 if draw == "bars" else 0,
              "showPoints": "always" if draw == "points" else "never", "pointSize": 6,
              "lineInterpolation": "linear", "spanNulls": True, "axisBorderShow": False,
              "barAlignment": -1 if draw == "bars" else 0,
              "stacking": {"mode": stack or "none", "group": "A"},
              "thresholdsStyle": {"mode": "dashed" if thresholds else "off"}}
    defaults = {"unit": unit, "custom": custom, "color": color or {"mode": "fixed", "fixedColor": ORANGE}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if min_ is not None:
        defaults["min"] = min_
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None}]
                                  + [{"color": "text", "value": v} for v in thresholds]}
    p = {"type": "timeseries", "title": title, "id": next(_ids), "description": description, "datasource": ds,
         "gridPos": pos, "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
         "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": legend},
                     "tooltip": {"mode": "multi", "sort": "none"}},
         "targets": refs(targets)}
    if extra:
        p.update(extra)
    return p


def as_line(name):
    """A trend drawn as a line among a panel's points."""
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "custom.drawStyle", "value": "line"},
                           {"id": "custom.showPoints", "value": "never"},
                           {"id": "custom.fillOpacity", "value": 0}]}


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def per_run_with_trend(title, description, value, alias, unit, p, decimals=None, runs=10, where=""):
    """One point per run, and the moving average of the last `runs` runs."""
    sub = f'SELECT {value} AS "v" {RUNS}{where}'
    return timeseries(title, description, [
        target(f'{sub} AND $timeFilter', alias),
        target(f'SELECT moving_average("v", {runs}) AS "trend" FROM ({sub} AND time >= ${{__from}}ms - 365d '
               f'AND time <= ${{__to}}ms)', f"{runs}-run average"),
    ], unit, p, overrides=[as_line(f"{runs}-run average")], decimals=decimals)


# Pace in seconds per km, read as m:ss.
PACE = "clocks"  # with 1 decimal: 04m:38s
# An elapsed time, read as hh:mm:ss.
DURATION = "dthms"
MOVING_PACE = '"movingDuration" / "distance" * 1000'
BEATS_PER_KM = '"averageHR" * "movingDuration" / "distance" * 1000 / 60'
CADENCE = 'mean("Cadence") * 2 + mean("Fractional_Cadence") * 2'
# The two intensity boundaries, from the watch's own heart-rate zones.
EASY_HR = '"HeartRate" > 0 AND "HeartRate" < $z3'
MODERATE_HR = '"HeartRate" >= $z3 AND "HeartRate" < $z4'
HARD_HR = '"HeartRate" >= $z4'


def build():
    global _ids
    _ids = iter(range(1, 1000))
    panels = []
    y = 0

    # ---------------------------------------------------------------- overview
    panels.append(row("Overview", y)); y += 1
    tiles = [
        ("Runs", "Runs in the time range: activities whose type contains \"run\", 1 km or longer.",
         f'SELECT count("distance") {RUNS} AND $timeFilter', "none", 0),
        ("Distance", "Total distance run.", f'SELECT sum("distance") / 1000 {RUNS} AND $timeFilter', "lengthkm", 0),
        ("Time running", "Total time, pauses included (the activities' elapsed time).",
         f'SELECT sum("elapsedDuration") / 3600 {RUNS} AND $timeFilter', "suffix: h", 0),
        ("Average run", "Mean distance of a run.", f'SELECT mean("distance") / 1000 {RUNS} AND $timeFilter', "lengthkm", 1),
        ("Average pace", "Total moving time over total distance (stops left out), per km.",
         f'SELECT sum("movingDuration") / sum("distance") * 1000 {RUNS} AND $timeFilter', PACE, 1),
        ("Runs of 10 km+", "Runs of 10 km or more (to within the 50 m a watch loses).",
         f'SELECT count("distance") {RUNS} AND "distance" >= 9950 AND $timeFilter', "none", 0),
        ("Best 10 km", "The fastest 10 consecutive 1 km laps of any run in the range (the 10 km row lists them).",
         f'SELECT min("best") FROM ({best_rows(BEST_SELECT)})', DURATION, 0),
        ("VO₂ max", "The watch's latest running VO₂ max in the range.",
         'SELECT last("VO2_max_value") FROM "VO2_Max" WHERE "VO2_max_value" > 0 AND $timeFilter', "none", 0),
        ("Longest run", "The longest run's distance.", f'SELECT max("distance") / 1000 {RUNS} AND $timeFilter', "lengthkm", 1),
        ("Longest run, time", "The longest run's elapsed time.",
         f'SELECT max("elapsedDuration") {RUNS} AND $timeFilter', DURATION, 0),
        ("Shortest run", "The shortest run's distance (runs under 1 km are not counted).",
         f'SELECT min("distance") / 1000 {RUNS} AND $timeFilter', "lengthkm", 1),
        ("Shortest run, time", "The shortest run's elapsed time.",
         f'SELECT min("elapsedDuration") {RUNS} AND $timeFilter', DURATION, 0),
        ("Runs / week", "Runs per week, averaged over the time range.",
         f'SELECT count("distance") * 604800000 / (${{__to}} - ${{__from}}) {RUNS} AND $timeFilter', "none", 1),
        ("Last run", "When the latest run in the range started.", None, "dateTimeFromNow", None),
        ("Climbing", "Total elevation gain of the runs that recorded one (the watch's own figure; the older imports "
         "have none).", f'SELECT sum("elevationGain") {RUNS} AND $timeFilter', "suffix: m", 0),
        ("Average heart rate", "Mean of the runs' average heart rates.",
         f'SELECT mean("averageHR") {RUNS} AND "averageHR" > 0 AND $timeFilter', "suffix: bpm", 0),
    ]
    for i, (title, desc, query, unit, dec) in enumerate(tiles):
        p = pos((i % 8) * 3, y + (i // 8) * 4, 3, 4)
        if title == "Last run":
            panels.append(stat(title, desc, f'SELECT last("distance") {RUNS} AND $timeFilter', unit, p,
                               fields="/^Time$/"))
        else:
            panels.append(stat(title, desc, query, unit, p, decimals=dec))
    y += 8

    # ------------------------------------------------------- volume & regularity
    panels.append(row("Volume and regularity", y)); y += 1
    panels.append(timeseries(
        "Distance per week",
        "Kilometres run each week (bars, weeks start on Monday) and the average of the last 4 weeks (line). "
        "A week far above its line is a jump in load: the usual advice is to add no more than about 10% a week.",
        [target(f'SELECT sum("distance") / 1000 {RUNS} AND $timeFilter {WEEK}', "km"),
         target(f'SELECT moving_average(sum("distance"), 4) / 1000 {RUNS} AND time >= ${{__from}}ms - 28d '
                f'AND time <= ${{__to}}ms {WEEK}', "4-week average")],
        "lengthkm", pos(0, y, 12, 8), draw="bars", min_=0, decimals=1,
        overrides=[as_line("4-week average"), colour("4-week average", "text")]))
    panels.append(timeseries(
        "Runs per week", "Number of runs each week.",
        [target(f'SELECT count("distance") {RUNS} AND $timeFilter {WEEK}', "runs")],
        "none", pos(12, y, 6, 8), draw="bars", min_=0, decimals=0, legend=False))
    panels.append(timeseries(
        "Days since the previous run",
        "For each run, the days since the one before. Long gaps followed by a hard run are when an injury "
        "is most likely.",
        [target(f'SELECT elapsed("distance", 1d) {RUNS} AND $timeFilter', "days")],
        "none", pos(18, y, 6, 8), min_=0, decimals=0, legend=False))
    y += 8
    panels.append(timeseries(
        "Distance of each run", "One point per run; the dashed line is 10 km.",
        [target(f'SELECT "distance" / 1000 AS "km" {RUNS} AND $timeFilter', "km")],
        "lengthkm", pos(0, y, 12, 8), min_=0, decimals=1, legend=False, thresholds=[10]))
    panels.append({
        "type": "histogram", "title": "How long the runs are", "id": next(_ids),
        "description": "Number of runs per 1 km of distance.",
        "datasource": GARMIN, "gridPos": pos(12, y, 12, 8),
        "fieldConfig": {"defaults": {"unit": "lengthkm", "color": {"mode": "fixed", "fixedColor": ORANGE},
                                     "custom": {"fillOpacity": 80, "lineWidth": 1}}, "overrides": []},
        "options": {"bucketSize": 1, "bucketOffset": 0, "combine": False,
                    "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"}},
        "targets": refs([target(f'SELECT "distance" / 1000 AS "km" {RUNS} AND $timeFilter', "km")])})
    y += 8

    # --------------------------------------------------------- intensity & load
    panels.append(row("Intensity and training effect", y)); y += 1
    zone_desc = ("Each second of running with a heart rate, by intensity, from the watch's own heart-rate zones "
                 "(the latest ones it reported, so every year is cut the same way): easy below zone 3, moderate in "
                 "zone 3, hard in zones 4 and 5. Most training plans keep about 80% of the running easy.")
    shares = [
        ("Easy running", EASY_HR), ("Hard running", HARD_HR),
    ]
    for i, (title, cond) in enumerate(shares):
        panels.append(stat(
            title, zone_desc + " This tile: its share of the time range's running seconds.", None, "percentunit",
            pos(i * 4, y, 4, 8), decimals=0,
            targets=[target(f'SELECT count("HeartRate") AS "part" {GPS} AND {cond} AND $timeFilter', "part", ref="A"),
                     target(f'SELECT count("HeartRate") AS "all" {GPS} AND "HeartRate" > 0 AND $timeFilter', "all", ref="B")],
            transformations=[
                {"id": "joinByField", "options": {"byField": "Time", "mode": "outer"}},
                {"id": "calculateField", "options": {"mode": "binary", "alias": "share", "replaceFields": True,
                                                     "binary": {"left": {"matcher": {"id": "byName", "options": "part"}},
                                                                "operator": "/",
                                                                "right": {"matcher": {"id": "byName", "options": "all"}}}}}]))
    panels.append(timeseries(
        "Easy, moderate and hard, share of each week", zone_desc,
        [target(f'SELECT count("HeartRate") {GPS} AND {EASY_HR} AND $timeFilter {WEEK}', "easy"),
         target(f'SELECT count("HeartRate") {GPS} AND {MODERATE_HR} AND $timeFilter {WEEK}', "moderate"),
         target(f'SELECT count("HeartRate") {GPS} AND {HARD_HR} AND $timeFilter {WEEK}', "hard")],
        "percentunit", pos(8, y, 16, 8), draw="bars", stack="percent", min_=0,
        overrides=[colour("easy", EASY), colour("moderate", MODERATE), colour("hard", HARD)]))
    y += 8
    panels.append(timeseries(
        "Training effect of each run",
        "Garmin's aerobic and anaerobic training effect, 0 to 5, per run (watches from about 2016; older imports "
        "have none). 3 to 4 improves, above 4 is highly improving or overreaching: many runs above 4 in a row is "
        "a lot of hard running.",
        [target(f'SELECT "aerobicTrainingEffect" AS "aerobic" {RUNS} AND $timeFilter', "aerobic"),
         target(f'SELECT "anaerobicTrainingEffect" AS "anaerobic" {RUNS} AND $timeFilter', "anaerobic")],
        "none", pos(0, y, 12, 8), min_=0, decimals=1,
        overrides=[colour("aerobic", BLUE), colour("anaerobic", AQUA)]))
    panels.append(timeseries(
        "Training load of each run",
        "Garmin's training load (EPOC) per run, and the average of the last 5 runs.",
        [target(f'SELECT "activityTrainingLoad" AS "load" {RUNS} AND "activityTrainingLoad" > 0 AND $timeFilter', "load"),
         target(f'SELECT moving_average("activityTrainingLoad", 5) AS "trend" {RUNS} AND "activityTrainingLoad" > 0 '
                f'AND time >= ${{__from}}ms - 365d AND time <= ${{__to}}ms', "5-run average")],
        "none", pos(12, y, 12, 8), min_=0, decimals=0, overrides=[as_line("5-run average")]))
    y += 8

    # ------------------------------------------------------------------ 10 km
    panels.append(row("10 km", y)); y += 1
    ten_k_desc = ("Every run of 10 km or more, compared on its best 10 km: its fastest 10 consecutive 1 km laps "
                  "(the watch's auto-lap), and what the laps measured over the same 10 km. A run without 1 km laps "
                  "(manual laps, an import without its laps) shows its totals only.")
    table_targets = [
        target(f'SELECT "distance" / 1000 AS "Distance", "elapsedDuration" AS "Duration", "activityName" AS "Name", '
               f'"aerobicTrainingEffect" AS "Aerobic TE", "activityTrainingLoad" AS "Load" '
               f'{RUNS} AND "distance" >= 9950 AND $timeFilter GROUP BY "ActivityID"', fmt="table", ref="A"),
        target(best_rows('min("t") AS "Best 10 km", "t" / 10 AS "Pace", "hr" AS "Heart rate", '
                         '"hr" * "t" / 600 AS "Beats / km", "step" AS "Step length", "gct" AS "Ground contact", '
                         '"vo" AS "Vertical oscillation", "vr" AS "Vertical ratio", "ascent" AS "Ascent", '
                         '"temp" AS "Temperature"'), fmt="table", ref="B"),
        target(f'SELECT {CADENCE} AS "Cadence" {GPS} AND "Cadence" > 0 AND $timeFilter GROUP BY "ActivityID"',
               fmt="table", ref="C"),
    ]
    units = {"Distance": ("lengthkm", 2), "Duration": (DURATION, 0), "Best 10 km": (DURATION, 0), "Pace": (PACE, 1),
             "Heart rate": ("suffix: bpm", 0), "Beats / km": ("none", 0), "Step length": ("suffix: m", 2),
             "Ground contact": ("ms", 0), "Vertical oscillation": ("lengthcm", 1), "Vertical ratio": ("percent", 1),
             "Ascent": ("lengthm", 0), "Temperature": ("celsius", 0), "Cadence": ("suffix: spm", 0),
             "Aerobic TE": ("none", 1), "Load": ("none", 0)}
    order = ["Time ActivitySummary", "Name", "Distance", "Duration", "Best 10 km", "Pace", "Heart rate", "Beats / km", "Cadence",
             "Step length", "Ground contact", "Vertical oscillation", "Vertical ratio", "Ascent", "Temperature",
             "Aerobic TE", "Load"]
    panels.append({
        "type": "table", "title": "Every 10 km, on its best 10 km", "id": next(_ids), "description": ten_k_desc +
        " Columns from Best 10 km to Temperature are the best 10 km's; Distance, Time, Cadence, training effect "
        "and load are the whole run's. Click a header to sort.",
        "datasource": GARMIN, "gridPos": pos(0, y, 24, 12),
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": k},
                                       "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": d}]}
                                      for k, (u, d) in units.items()]
                        + [{"matcher": {"id": "byName", "options": "Date"},
                            "properties": [{"id": "unit", "value": "time:YYYY-MM-DD"}]}]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Date", "desc": True}],
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "targets": refs(table_targets),
        "transformations": [
            {"id": "joinByField", "options": {"byField": "ActivityID", "mode": "outerTabular"}},
            {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [
                {"fieldName": "Distance", "config": {"id": "isNull", "options": {}}}]}},
            {"id": "organize", "options": {
                "excludeByName": {"ActivityID": True, "Time ActivityLap": True, "Time ActivityGPS": True},
                "indexByName": {k: i for i, k in enumerate(order)},
                "renameByName": {"Time ActivitySummary": "Date"}}},
        ]})
    y += 12
    trend_runs = 5

    def ten_k_panel(title, desc, expr, unit, p, decimals=None, where=""):
        series = f'SELECT {expr} AS "v" FROM ({best_rows(BEST_SELECT)}){where}'
        return timeseries(title, desc, [
            target(series, "best 10 km"),
            target(f'SELECT moving_average("v", {trend_runs}) AS "trend" FROM ({series})', f"{trend_runs}-run average"),
        ], unit, p, decimals=decimals, overrides=[as_line(f"{trend_runs}-run average")])

    panels.append(ten_k_panel("Best 10 km time", "Each run's best 10 km, and the average of the last 5. Lower is "
                              "faster.", '"best"', DURATION, pos(0, y, 8, 8), 0))
    panels.append(ten_k_panel("Heart rate on the best 10 km", "Mean of the 10 laps' average heart rates.",
                              '"hr"', "suffix: bpm", pos(8, y, 8, 8), 0))
    panels.append(ten_k_panel("Heartbeats per km on the best 10 km",
                              "Average heart rate times pace: the beats one km costs. Lower is more economical: "
                              "running as fast for fewer beats, or faster for the same.",
                              '"hr" * "best" / 600', "none", pos(16, y, 8, 8), 0))
    y += 8
    dyn = " Needs a running dynamics sensor (a chest strap or pod); older runs have none."
    panels.append(ten_k_panel("Step length on the best 10 km", "Mean step length over the 10 laps." + dyn,
                              '"step"', "suffix: m", pos(0, y, 8, 8), 2))
    panels.append(ten_k_panel("Cadence on the best 10 km", "Steps per minute over the 10 laps: their speed over "
                              "their step length." + dyn, '600000 / ("best" * "step")', "suffix: spm",
                              pos(8, y, 8, 8), 0))
    panels.append(ten_k_panel("Ground contact on the best 10 km", "Mean ground contact time over the 10 laps. "
                              "Shorter is usually more efficient." + dyn, '"gct"', "ms", pos(16, y, 8, 8), 0))
    y += 8
    panels.append(ten_k_panel("Vertical oscillation on the best 10 km", "How far the body bounces at each step." + dyn,
                              '"vo"', "lengthcm", pos(0, y, 8, 8), 1))
    panels.append(ten_k_panel("Vertical ratio on the best 10 km", "Vertical oscillation over step length: lower "
                              "means less of each step goes up rather than forward." + dyn, '"vr"', "percent",
                              pos(8, y, 8, 8), 1))
    panels.append(ten_k_panel("Temperature on the best 10 km", "The watch's temperature over the 10 laps (warmed "
                              "by the wrist): heat alone slows a run and raises its heart rate.", '"temp"', "celsius",
                              pos(16, y, 8, 8), 0))
    y += 8

    # ------------------------------------------------- pace, heart & efficiency
    panels.append(row("Every run: pace, heart and fitness", y)); y += 1
    panels.append(per_run_with_trend("Pace of each run", "Moving pace (stops left out) of each run, and the average "
                                     "of the last 10 runs. Lower is faster.", MOVING_PACE, "pace", PACE,
                                     pos(0, y, 12, 8), 1))
    panels.append(per_run_with_trend("Heart rate of each run", "Average heart rate of each run, and of the last "
                                     "10 runs.", '"averageHR"', "bpm", "suffix: bpm", pos(12, y, 12, 8), 0,
                                     where=' AND "averageHR" > 0'))
    y += 8
    panels.append(per_run_with_trend("Heartbeats per km", "Average heart rate times moving pace: the beats a km "
                                     "costs, run by run. Falling over months at the same kind of run is aerobic "
                                     "fitness improving; rising for a few runs can be fatigue, heat or illness.",
                                     BEATS_PER_KM, "beats", "none", pos(0, y, 12, 8), 0,
                                     where=' AND "averageHR" > 0'))
    panels.append(timeseries(
        "VO₂ max", "The watch's running VO₂ max estimate, each time it changes. Higher is fitter.",
        [target('SELECT max("VO2_max_value") FROM "VO2_Max" WHERE "VO2_max_value" > 0 AND $timeFilter '
                "GROUP BY time(1d) fill(none) tz('$tz')", "VO₂ max")],
        "none", pos(12, y, 12, 8), draw="line", decimals=0, legend=False))
    y += 8

    # ------------------------------------------------------------ running form
    panels.append(row("Running form, every run", y)); y += 1
    panels.append(timeseries(
        "Cadence", "Steps per minute, each run's mean over the seconds it was moving (the watch's own cadence, every "
        "run that has its per-second data).",
        [target(f'SELECT {CADENCE} AS "cadence" {GPS} AND "Cadence" > 0 AND $timeFilter {DAY}', "cadence")],
        "suffix: spm", pos(0, y, 12, 8), decimals=0, legend=False))
    panels.append(timeseries(
        "Step length", "Each run's mean speed over its mean cadence: the distance of one step, available for every "
        "run with per-second data (a running dynamics sensor measures it directly; this needs none).",
        [target(f'SELECT mean("Speed") * 30 / (mean("Cadence") + mean("Fractional_Cadence")) AS "step" {GPS} '
                f'AND "Cadence" > 0 AND "Speed" > 0 AND $timeFilter {DAY}', "step")],
        "suffix: m", pos(12, y, 12, 8), decimals=2, legend=False))
    y += 8
    for i, (title, field, unit, scale, dec, desc) in enumerate([
        ("Ground contact time", "Stance_Time", "ms", "", 0, "How long each foot stays on the ground."),
        ("Vertical oscillation", "Vertical_Oscillation", "lengthcm", " / 10", 1, "How far the body bounces at each step."),
        ("Vertical ratio", "Vertical_Ratio", "percent", "", 1, "Vertical oscillation over step length."),
    ]):
        panels.append(timeseries(
            title, f"{desc} Each run's mean.{dyn}",
            [target(f'SELECT mean("{field}"){scale} AS "v" {GPS} AND "{field}" > 0 AND $timeFilter {DAY}', title.lower())],
            unit, pos(i * 8, y, 8, 8), decimals=dec, legend=False))
    y += 8

    # ---------------------------------------------------------------- recovery
    panels.append(row("Recovery around the running", y)); y += 1
    recovery = ("Weekly averages, under the running of the same weeks: a resting heart rate climbing, or an HRV "
                "falling, for more than a few days after a heavy week is the body asking for rest. Read from the "
                "source picked in {picker}.")
    panels.append(timeseries(
        "Distance per week", "Kilometres run each week, to read the two graphs below against.",
        [target(f'SELECT sum("distance") / 1000 {RUNS} AND $timeFilter {WEEK}', "km")],
        "lengthkm", pos(0, y, 24, 6), draw="bars", min_=0, decimals=1, legend=False))
    y += 6
    by_name = {"mode": "palette-classic-by-name"}
    source_overrides = [colour(f"/.*{s}$/", c, regex=True) for s, c in SOURCE_COLOURS.items()]
    panels.append(timeseries(
        "Resting heart rate per week", recovery.format(picker="*Resting HR from*"),
        [target(f'SELECT mean("restingHeartRate") FROM "SleepSummary" WHERE {GARMIN_NIGHT} AND \'garmin\' = '
                f"'${{rhr_from}}' AND $timeFilter GROUP BY time(1w, -3d) fill(none) tz('$tz')", "garmin"),
         target('SELECT mean("resting_hr") FROM "sleep_session" WHERE "source" = \'${rhr_from}\' AND "user" = '
                "'$user' AND \"kind\" != 'nap' AND $timeFilter GROUP BY time(1w, -3d), \"source\" fill(none) tz('$tz')",
                "$tag_source", ds=SLEEP),
         target('SELECT mean("value") FROM "RestingHR" WHERE \'fitbit\' = \'${rhr_from}\' AND $timeFilter '
                "GROUP BY time(1w, -3d) fill(none) tz('$tz')", "fitbit", ds=FITBIT)],
        "suffix: bpm", pos(0, y, 24, 7), draw="line", decimals=0, ds=MIXED, color=by_name,
        overrides=source_overrides))
    y += 7
    panels.append(timeseries(
        "HRV per week", recovery.format(picker="*HRV from*"),
        [target(f'SELECT mean("avgOvernightHrv") FROM "SleepSummary" WHERE {GARMIN_NIGHT} AND \'garmin\' = '
                f"'${{hrv_from}}' AND $timeFilter GROUP BY time(1w, -3d) fill(none) tz('$tz')", "garmin"),
         target('SELECT mean("hrv_rmssd_ms") FROM "sleep_session" WHERE "source" = \'${hrv_from}\' AND "user" = '
                "'$user' AND \"kind\" != 'nap' AND $timeFilter GROUP BY time(1w, -3d), \"source\" fill(none) tz('$tz')",
                "$tag_source", ds=SLEEP),
         target('SELECT mean("dailyRmssd") FROM "HRV" WHERE \'fitbit\' = \'${hrv_from}\' AND $timeFilter '
                "GROUP BY time(1w, -3d) fill(none) tz('$tz')", "fitbit", ds=FITBIT)],
        "ms", pos(0, y, 24, 7), draw="line", decimals=0, ds=MIXED, color=by_name, overrides=source_overrides))
    y += 7

    # -------------------------------------------------------------------- log
    panels.append(row("Every run", y)); y += 1
    log_units = {"Distance": ("lengthkm", 2), "Duration": (DURATION, 0), "Pace": (PACE, 1), "Heart rate": ("suffix: bpm", 0),
                 "Max heart rate": ("suffix: bpm", 0), "Beats / km": ("none", 0), "Cadence": ("suffix: spm", 0),
                 "Ascent": ("lengthm", 0), "Aerobic TE": ("none", 1), "Anaerobic TE": ("none", 1),
                 "Load": ("none", 0), "Calories": ("suffix: kcal", 0)}
    log_order = ["Time ActivitySummary", "Name", "Type", "Distance", "Duration", "Pace", "Heart rate", "Max heart rate", "Beats / km",
                 "Cadence", "Ascent", "Aerobic TE", "Anaerobic TE", "Load", "Calories"]
    panels.append({
        "type": "table", "title": "Runs", "id": next(_ids),
        "description": "Every run in the time range, latest first. Pace is the moving pace; Ascent is the watch's elevation gain "
                       "(the older imports have none); "
                       "Cadence is the mean over the seconds moving. Click a header to sort.",
        "datasource": GARMIN, "gridPos": pos(0, y, 24, 12),
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": k},
                                       "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": d}]}
                                      for k, (u, d) in log_units.items()]
                        + [{"matcher": {"id": "byName", "options": "Date"},
                            "properties": [{"id": "unit", "value": "time:YYYY-MM-DD HH:mm"}]}]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Date", "desc": True}],
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "targets": refs([
            target(f'SELECT "activityName" AS "Name", "activityType" AS "Type", "distance" / 1000 AS "Distance", '
                   f'"elapsedDuration" AS "Duration", {MOVING_PACE} AS "Pace", "averageHR" AS "Heart rate", '
                   f'"maxHR" AS "Max heart rate", {BEATS_PER_KM} AS "Beats / km", '
                   f'"aerobicTrainingEffect" AS "Aerobic TE", "anaerobicTrainingEffect" AS "Anaerobic TE", '
                   f'"activityTrainingLoad" AS "Load", "calories" AS "Calories", "elevationGain" AS "Ascent" {RUNS} AND $timeFilter '
                   f'GROUP BY "ActivityID"', fmt="table", ref="A"),
            target(f'SELECT {CADENCE} AS "Cadence" {GPS} AND "Cadence" > 0 AND $timeFilter GROUP BY "ActivityID"',
                   fmt="table", ref="B"),
        ]),
        "transformations": [
            {"id": "joinByField", "options": {"byField": "ActivityID", "mode": "outerTabular"}},
            {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [
                {"fieldName": "Distance", "config": {"id": "isNull", "options": {}}}]}},
            {"id": "organize", "options": {
                "excludeByName": {"ActivityID": True, "Time ActivityGPS": True},
                "indexByName": {k: i for i, k in enumerate(log_order)},
                "renameByName": {"Time ActivitySummary": "Date"}}},
        ]})

    return {
        "uid": "tributary-running",
        "title": "Running",
        "description": "A Garmin watch's runs, as garmin-grafana writes them (ActivitySummary, ActivityLap, "
                       "ActivityGPS, VO2_Max): volume and regularity, intensity, every 10 km compared on its best "
                       "10 km with the running dynamics, pace, heart rate and fitness run by run, running form, "
                       "and the resting heart rate and HRV of the same weeks.",
        "tags": ["tributary", "running"],
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 41,
        "time": {"from": "now-2y", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "refresh": "",
        "links": [
            {"type": "link", "title": "Activity & fitness", "url": "/d/tributary-activity", "icon": "dashboard",
             "asDropdown": False, "includeVars": False, "keepTime": True, "targetBlank": False, "tooltip": "",
             "tags": []},
            {"type": "link", "title": "Healthspan", "url": "/d/tributary-healthspan", "icon": "dashboard",
             "asDropdown": False, "includeVars": False, "keepTime": True, "targetBlank": False, "tooltip": "",
             "tags": []},
        ],
        "annotations": {"list": []},
        "templating": {"list": variables()},
        "panels": panels,
    }


def datasource(name, database):
    return {"type": "datasource", "name": name, "label": f"InfluxDB {database}", "query": "influxdb",
            "regex": f"/{database}/i", "hide": 0, "refresh": 1, "current": {}, "options": [],
            "includeAll": False, "multi": False}


def query_var(name, label, description, ds, query, hide=0, multi=False):
    v = {"type": "query", "name": name, "label": label, "description": description, "datasource": ds,
         "definition": query, "query": query, "refresh": 1, "sort": 1, "hide": hide, "current": {}, "options": [],
         "multi": multi, "includeAll": multi}
    if multi:
        v["current"] = {"text": "All", "value": "$__all"}
    return v


def variables():
    zone = ('SELECT last("hrZoneLowBoundary_{n}") FROM "ActivitySummary" WHERE "activityType" =~ /run/ '
            'AND "hrZoneLowBoundary_{n}" > 0')
    return [
        datasource("garmin", "GarminStats"),
        datasource("sleep", "SleepStats"),
        datasource("fitbit", "FitbitHealthStats"),
        {"type": "textbox", "name": "tz", "label": "Time zone",
         "description": "IANA time zone the days and weeks are cut in (TRIBUTARY_TZ).", "query": "Europe/Paris",
         "current": {"text": "Europe/Paris", "value": "Europe/Paris"},
         "options": [{"selected": True, "text": "Europe/Paris", "value": "Europe/Paris"}], "hide": 0},
        query_var("user", "User", "The person, in SleepStats (GarminStats holds one person and has no user tag).",
                  SLEEP, 'SHOW TAG VALUES FROM "sleep_session" WITH KEY = "user"'),
        query_var("nights", "Night sources", "Every source of nights in SleepStats, for the heart pickers.", SLEEP,
                  'SHOW TAG VALUES FROM "sleep_session" WITH KEY = "source" WHERE "user" = \'$user\'', hide=2,
                  multi=True),
        {"type": "custom", "name": "rhr_from", "label": "Resting HR from",
         "description": "The source of the resting heart rate.", "query": "${nights:csv},garmin,fitbit", "hide": 0,
         "current": {}, "options": [], "multi": False, "includeAll": False},
        {"type": "custom", "name": "hrv_from", "label": "HRV from",
         "description": "The source of the overnight HRV. Devices measure it differently.",
         "query": "${nights:csv},garmin,fitbit", "hide": 0, "current": {}, "options": [], "multi": False,
         "includeAll": False},
        query_var("z3", "Zone 3 from", "The watch's lower bound of heart-rate zone 3: below it, a second of running "
                  "is easy.", GARMIN, zone.format(n=3), hide=2),
        query_var("z4", "Zone 4 from", "The watch's lower bound of heart-rate zone 4: from it, a second of running "
                  "is hard.", GARMIN, zone.format(n=4), hide=2),
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
