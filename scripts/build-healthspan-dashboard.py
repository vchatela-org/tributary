#!/usr/bin/env python3
"""Writes grafana/dashboards/healthspan.json: WHOOP's Healthspan screen, re-drawn
from the series Tributary and garmin-grafana keep, for any source.

WHOOP Age is the chronological age plus nine age impacts. WHOOP's 2025 white
paper gives each indicator a guideline worth 0 years and an impact of
10 * ln(hazard ratio). Each impact is modelled here as a straight line through
that guideline:

    years = K * (value - TARGET)

with K fitted on screenshots of the app (one person, October 2026: its own
6-month values against the years it showed). WHOOP's slopes depend on age and
sex, so the years are an estimate; the bars use the app's scales, and its
colours in five bands (orange adds years, grey is about 0, green takes them
off), read off the same screenshots.

Every value reads the source picked for it (docs/dashboards-design.md, rule 3).
The panels of the last row, "Other markers, every source", are kept from the
JSON as they are: edit them there. Everything else is written here: edit a
constant, run `python3 scripts/build-healthspan-dashboard.py`, commit both
files (tests/test_dashboards.py checks they agree).
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "healthspan.json"
KEPT_ROW = "Other markers, every source"

# The app's two averages: the ▼ and ▲ markers of each bar.
WINDOWS = [(182, "6 months"), (30, "30 days")]
YEAR_DAYS = 365.25

DS = {db: {"type": "influxdb", "uid": "${" + var + "}"}
      for db, var in (("SleepStats", "sleep"), ("BodyStats", "body"), ("GarminStats", "garmin"))}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}

# The diverging scale: two steps per arm around a grey midpoint. The dataviz
# validator keeps the arms apart for protan and deutan vision (worst adjacent
# pair ΔE 14, 18 for normal vision). The bright steps are faint on a light
# panel, so no value is written in a colour: a bar prints its value in the
# text colour, an age impact sits on a tile filled with its colour. Grafana
# writes on such a tile in dark ink above a brightness of 180, in white below:
# the bright steps sit above it (contrast 9.4 and 11.3), the dim ones and the
# grey below (3.1 to 5.8, for a large value).
O2, O1, N, G1, G2 = "#f8bc5e", "#b8803f", "#5d6268", "#22a07b", "#5ff2c0"

GARMIN_NIGHT = '"sleepTimeSeconds" > 0 AND "remSleepSeconds" > 0'


def night(picker):
    return (f'FROM "sleep_session" WHERE "source" = \'${{{picker}}}\' AND "user" = \'$user\' '
            f'AND "kind" != \'nap\'')


def garmin_night(picker):
    return f'FROM "SleepSummary" WHERE {GARMIN_NIGHT} AND \'garmin\' = \'${{{picker}}}\''


def workouts(where=""):
    return (f'FROM "exercise" WHERE "source" = \'${{workouts_from}}\' AND "user" = \'$user\' '
            f'AND "category" != \'adjustment\'{where}')


def zones(db, fields):
    """A workout's minutes in some zones, summed in a subquery: one value per workout."""
    total = " + ".join(f'"{f}"' for f in fields)
    if db == "BodyStats":
        return f'FROM (SELECT {total} AS "z" {workouts()} AND {{time}}) WHERE true'
    return (f'FROM (SELECT {total} AS "z" FROM "ActivitySummary" '
            f'WHERE \'garmin\' = \'${{workouts_from}}\' AND {{time}}) WHERE true')


def garmin_days():
    """garmin-grafana's DailyStats, one value a day (a day can be written twice)."""
    return ('FROM (SELECT max("totalSteps") AS "s" FROM "DailyStats" '
            'WHERE \'garmin\' = \'${activity_from}\' AND {time} GROUP BY time(1d) fill(none) tz(\'$tz\')) '
            'WHERE true')


def strength(db):
    """Strength workouts, plus every workout of the source counted as 0 minutes: a
    window with workouts but no strength reads 0, not nothing."""
    if db == "BodyStats":
        return (f'FROM (SELECT "duration_s" AS "d" {workouts(" AND \"category\" = 'strength'")} AND {{time}}), '
                f'(SELECT "duration_s" * 0 AS "d" {workouts()} AND {{time}}) WHERE true')
    garmin = 'FROM "ActivitySummary" WHERE \'garmin\' = \'${workouts_from}\''
    return (f'FROM (SELECT "elapsedDuration" AS "d" {garmin} AND "activityType" =~ /strength/ AND {{time}}), '
            f'(SELECT "elapsedDuration" * 0 AS "d" {garmin} AND {{time}}) WHERE true')


def per_week(expr):
    return f"{expr} / 60 / ({{days}} / 7)"


# Each indicator: section, title, unit, decimals, the app's scale and bands
# (low end first: the value where a band starts, its colour), K (years per
# unit) and TARGET (0 years), and per source the database, the value over one
# window ("single") and its daily rolling average ("rolling", over {n} days).
INDICATORS = [
    dict(key="consistency", section="Sleep", title="Sleep consistency", unit="percent", decimals=0,
         scale=(40, 100), bands=[(None, O2), (64, O1), (70, G1), (82, G2)], k=-0.172, target=70,
         sources=[("SleepStats", 'mean("consistency_pct") ' + night("sleep_from"),
                   'moving_average(mean("consistency_pct"), {n}) ' + night("sleep_from"), "previous")],
         note="How regular the bedtimes and wake-ups are, as the source scores it (consistency_pct, "
              "WHOOP's sleep consistency; garmin-grafana's summary has none). 0 years at 70 %, "
              "-0.17 year per point above."),
    dict(key="sleep", section="Sleep", title="Hours of sleep", unit="suffix: h", decimals=2,
         scale=(5, 8), bands=[(None, O2), (5.6, O1), (6.8, N), (7.1, G1), (7.4, G2)], k=-0.283, target=7,
         sources=[("SleepStats", 'mean("asleep_s") / 3600 ' + night("sleep_from"),
                   'moving_average(mean("asleep_s"), {n}) / 3600 ' + night("sleep_from"), "previous"),
                  ("GarminStats", 'mean("sleepTimeSeconds") / 3600 ' + garmin_night("sleep_from"),
                   'moving_average(mean("sleepTimeSeconds"), {n}) / 3600 ' + garmin_night("sleep_from"),
                   "previous")],
         note="Time asleep per night, naps left out. 0 years at 7 h, the least the American Academy "
              "of Sleep Medicine recommends for an adult; -0.28 year per hour above."),
    dict(key="zones13", section="Strain", title="Time in HR zones 1-3, per week", unit="suffix: min",
         decimals=0, scale=(0, 180), bands=[(None, O2), (36, O1), (108, G1), (144, G2)],
         k=-0.408 / 60, target=100,
         sources=[(db, per_week('sum("z")') + " " + zones(db, fields),
                   'moving_average(sum("z"), {n}) * 7 / 60 ' + zones(db, fields), "0")
                  for db, fields in (("BodyStats", ["zone1_s", "zone2_s", "zone3_s"]),
                                     ("GarminStats", ["hrTimeInZone_1", "hrTimeInZone_2", "hrTimeInZone_3"]))],
         note="Minutes in heart-rate zones 1 to 3 during workouts, per week. "
              "0 years at 100 min, -0.41 year per hour above."),
    dict(key="zones45", section="Strain", title="Time in HR zones 4-5, per week", unit="suffix: min",
         decimals=0, scale=(0, 60), bands=[(None, O2), (12, N), (24, G1), (48, G2)],
         k=-0.200 / 60, target=10,
         sources=[(db, per_week('sum("z")') + " " + zones(db, fields),
                   'moving_average(sum("z"), {n}) * 7 / 60 ' + zones(db, fields), "0")
                  for db, fields in (("BodyStats", ["zone4_s", "zone5_s"]),
                                     ("GarminStats", ["hrTimeInZone_4", "hrTimeInZone_5"]))],
         note="Minutes in heart-rate zones 4 and 5 during workouts, per week. 0 years at 10 min; "
              "the app shows about 0 years over the values fitted, so the slope (-0.2 year per "
              "hour) is coarse."),
    dict(key="strength", section="Strain", title="Strength activity time, per week", unit="suffix: min",
         decimals=0, scale=(0, 120), bands=[(None, O2), (12, O1), (36, N), (48, G1), (84, G2)],
         k=-1.0 / 60, target=40,
         sources=[(db, per_week('sum("d")') + " " + strength(db),
                   'moving_average(sum("d"), {n}) * 7 / 60 ' + strength(db), "0")
                  for db in ("BodyStats", "GarminStats")],
         note="Minutes of strength workouts per week: category strength (weightlifting, "
              "powerlifting, functional fitness), Garmin's strength_training. 0 years at 40 min, "
              "-1 year per hour above."),
    dict(key="steps", section="Strain", title="Steps", unit="locale", decimals=0,
         scale=(0, 16000), bands=[(None, O2), (3200, O1), (8000, G1), (11200, G2)], k=-0.000123, target=8000,
         sources=[("BodyStats", 'mean("steps") FROM "daily_activity" WHERE "source" = \'${activity_from}\' '
                   'AND "user" = \'$user\'',
                   'moving_average(mean("steps"), {n}) FROM "daily_activity" '
                   'WHERE "source" = \'${activity_from}\' AND "user" = \'$user\'', "previous"),
                  ("GarminStats", 'mean("s") ' + garmin_days(),
                   'moving_average(max("totalSteps"), {n}) FROM "DailyStats" '
                   'WHERE \'garmin\' = \'${activity_from}\'', "previous")],
         note="Steps per day. 0 years at 8,000, by which studies of mortality see most of the "
              "benefit; -0.6 year per 5,000 above."),
    dict(key="vo2", section="Fitness", title="VO₂ max", unit="none", decimals=1,
         scale=(15, 70), bands=[(None, O2), (26, O1), (42.5, G1), (59, G2)], k=-0.295, target=44,
         sources=[("GarminStats", 'mean("VO2_max_value") FROM "VO2_Max" WHERE true',
                   'moving_average(mean("VO2_max_value"), {n}) FROM "VO2_Max" WHERE true', "previous")],
         note="The watch's running VO₂ max in ml/kg/min (garmin-grafana): WHOOP's own estimate is "
              "in no export, so a strap alone leaves this one out of WHOOP Age. 0 years at 44, "
              "-0.30 year per point above."),
    dict(key="rhr", section="Fitness", title="Resting heart rate", unit="suffix: bpm", decimals=0,
         scale=(40, 80), bands=[(None, G2), (52, G1), (60, O1), (72, O2)], k=0.114, target=60,
         sources=[("SleepStats", 'mean("resting_hr") ' + night("rhr_from"),
                   'moving_average(mean("resting_hr"), {n}) ' + night("rhr_from"), "previous"),
                  ("GarminStats", 'mean("restingHeartRate") ' + garmin_night("rhr_from"),
                   'moving_average(mean("restingHeartRate"), {n}) ' + garmin_night("rhr_from"), "previous")],
         note="The night's resting heart rate. 0 years at 60 bpm, +0.11 year per bpm: lower is "
              "younger."),
    dict(key="lbm", section="Fitness", title="Lean body mass", unit="percent", decimals=1,
         scale=(60, 90), bands=[(None, O2), (63, O1), (78, N)], k=-0.030, target=80,
         sources=[("BodyStats", '100 - mean("body_fat_pct") FROM "body_composition" '
                   'WHERE "source" = \'${weight_from}\' AND "user" = \'$user\'',
                   '100 - moving_average(mean("body_fat_pct"), {n}) FROM "body_composition" '
                   'WHERE "source" = \'${weight_from}\' AND "user" = \'$user\'', "previous"),
                  ("GarminStats", '100 - mean("bodyFat") FROM "BodyComposition" '
                   'WHERE \'garmin\' = \'${weight_from}\'',
                   '100 - moving_average(mean("bodyFat"), {n}) FROM "BodyComposition" '
                   'WHERE \'garmin\' = \'${weight_from}\'', "previous")],
         note="Lean body mass: 100 % less the scale's body fat. About 0 years above 78 %."),
]

# On a filled tile, "no data" takes the base step's colour: keep it clear, the
# first band starts below any age impact.
YEARS_BANDS = [(None, "transparent"), (-1000, G2), (-1, G1), (-0.05, N), (0.05, O1), (1, O2)]
YOUNGER_BANDS = [(None, "transparent"), (-1000, O2), (-1, O1), (-0.05, N), (0.05, G1), (1, G2)]

MODEL = ("Each age impact is a straight line through WHOOP's guideline for its indicator, with a "
         "slope fitted on screenshots of the app; WHOOP's own slopes depend on age and sex, so "
         "read the years as an estimate.")


def window(days):
    return f"time > ${{__to}}ms - {days}d AND time <= ${{__to}}ms"


def lookback(days):
    # Twice the window: a sparse series (weigh-ins, VO₂ max) has its first full
    # window by the start of the range.
    return f"time >= ${{__from}}ms - {2 * days}d AND time <= ${{__to}}ms"


def impact(agg, ind):
    return f"({agg} - {ind['target']:g}) * {ind['k']:.6g}"


def single_sql(ind, source, days, years=False):
    """One value: the mean over the window that ends at the end of the range."""
    agg, rest = source[1].split(" FROM ", 1)
    agg = agg.replace("{days}", str(days))
    if years:
        agg = impact(agg, ind)
    sql = f"SELECT {agg} FROM {rest.replace('{time}', window(days))} AND {window(days)}"
    if years:
        # Wrapped once more, every impact and the age land on time 0, where the
        # panel joins them.
        sql = f'SELECT sum("y") FROM ({sql.replace(f"SELECT {agg} ", f"SELECT {agg} AS \"y\" ", 1)})'
    return sql


def rolling_sql(ind, source, days, years=False):
    """One point a day: the mean of the `days` days before it."""
    agg, rest = source[2].split(" FROM ", 1)
    agg = agg.replace("{n}", str(days))
    if years:
        agg = impact(agg, ind)
    return (f"SELECT {agg} FROM {rest.replace('{time}', lookback(days))} AND {lookback(days)} "
            f"GROUP BY time(1d) fill({source[3]}) tz('$tz')")


def age_sql():
    # InfluxQL reads no date from a variable but as a time bound: count the days
    # from the birth date to the end of the range, one bucket a day, any night's
    # measurement as the calendar.
    return ('SELECT count("d") / 365.25 FROM (SELECT max("asleep_s") AS "d" FROM "sleep_session" '
            "WHERE time >= '${birth}' AND time <= ${__to}ms GROUP BY time(1d) fill(0) tz('$tz'))")


def target(ref, db, sql, alias):
    return {"refId": ref, "datasource": DS[db], "query": sql, "rawQuery": True,
            "resultFormat": "time_series", "alias": alias}


def refs():
    n = 0
    while True:
        yield "".join(chr(ord("A") + d) for d in ([n // 26 - 1] if n >= 26 else []) + [n % 26])
        n += 1


def thresholds(bands):
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in bands]}


def total(names, alias, times=None):
    """Add field from calculation: the sum of `names` in each row (a missing one is
    skipped: the source not picked), times a number."""
    steps = [{"id": "calculateField", "options": {
        "mode": "reduceRow", "reduce": {"reducer": "sum", "include": names},
        "alias": alias if times is None else f"{alias} (sum)", "replaceFields": False}}]
    if times is not None:
        steps.append(binary(f"{alias} (sum)", "*", times, alias))
    return steps


def binary(left, operator, right, alias):
    """Add field from calculation: `left` (a field) and `right` (a field or a number).
    A missing field adds nothing, so the panel shows no data rather than a part."""
    operand = ({"fixed": f"{right:g}"} if isinstance(right, (int, float))
               else {"matcher": {"id": "byName", "options": right}})
    return {"id": "calculateField", "options": {
        "mode": "binary", "binary": {"left": {"matcher": {"id": "byName", "options": left}},
                                     "operator": operator, "right": operand},
        "alias": alias, "replaceFields": False}}


JOIN = {"id": "joinByField", "options": {"byField": "Time", "mode": "outer"}}


def keep(*names):
    return {"id": "filterFieldsByName", "options": {"include": {"names": list(names)}}}


def stat(title, description, targets, transformations, unit, decimals, bands, color_mode="value",
         text_mode="auto"):
    return {
        "type": "stat", "title": title, "description": description,
        "datasource": MIXED if len({t["datasource"]["uid"] for t in targets}) > 1 else targets[0]["datasource"],
        "targets": targets, "transformations": transformations,
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "color": {"mode": "thresholds"},
                                     "thresholds": thresholds(bands)},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "none", "textMode": text_mode,
                    "justifyMode": "center", "orientation": "auto", "wideLayout": True,
                    "showPercentChange": False},
    }


def build(existing):
    panels = []
    ids = iter(range(1, 1000))

    def add(panel, x, y, w, h):
        panel["id"] = next(ids)
        panel["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
        panels.append(panel)

    def row(title, y):
        add({"type": "row", "title": title, "collapsed": False, "panels": []}, 0, y, 24, 1)

    def impacts(days, prefix, rolling=False):
        """One query per indicator and source, each named after its indicator."""
        out, names = [], []
        ref = refs()
        for ind in INDICATORS:
            name = f"{prefix}{ind['key']}"
            names.append(name)
            for source in ind["sources"]:
                sql = (rolling_sql if rolling else single_sql)(ind, source, days, years=True)
                out.append(target(next(ref), source[0], sql, name))
        return out, names, ref

    six, thirty = WINDOWS[0][0], WINDOWS[1][0]
    y = 0
    row("WHOOP Age, estimated", y)
    y += 1
    t6, n6, ref = impacts(six, "")
    add(stat("WHOOP Age",
             "The age the body's markers suggest, as WHOOP computes it: the chronological age plus the "
             "nine age impacts of the last 6 months. Needs the Birth date variable. " + MODEL,
             t6 + [target(next(ref), "SleepStats", age_sql(), "age")],
             [JOIN] + total(n6, "impacts") + [binary("age", "+", "impacts", "WHOOP Age"), keep("WHOOP Age")],
             "none", 1, [(None, "text")]), 0, y, 5, 4)
    t6, n6, _ = impacts(six, "")
    add(stat("Years younger",
             "Minus the sum of the nine age impacts of the last 6 months: how far WHOOP Age sits below "
             "the chronological age. An indicator with no source picked (VO₂ max without a watch) "
             "counts as 0. " + MODEL,
             t6, [JOIN] + total(n6, "Years younger", times=-1) + [keep("Years younger")],
             "none", 1, YOUNGER_BANDS, color_mode="background"), 5, y, 5, 4)
    add(stat("Chronological age",
             "From the Birth date variable (YYYY-MM-DD) to the end of the time range. Empty, this tile "
             "and WHOOP Age show an error instead.",
             [target("A", "SleepStats", age_sql(), "age")], [], "none", 1, [(None, "text")]), 0, y + 4, 5, 4)
    t30, n30, ref = impacts(thirty, "")
    add(stat("WHOOP Age, 30 days",
             "The same sum with the 30-day averages, the app's ▲ markers. Below the 6-month WHOOP Age, "
             "the last month is pulling it down; above, up. That is the idea of the app's pace of "
             "aging, which this does not reproduce.",
             t30 + [target(next(ref), "SleepStats", age_sql(), "age")],
             [JOIN] + total(n30, "impacts") + [binary("age", "+", "impacts", "WHOOP Age, 30 days"),
                                              keep("WHOOP Age, 30 days")],
             "none", 1, [(None, "text")]), 5, y + 4, 5, 4)

    r6, rn6, _ = impacts(six, "6 months ", rolling=True)
    r30, rn30, _ = impacts(thirty, "30 days ", rolling=True)
    ref = refs()
    for t in r6 + r30:
        t["refId"] = next(ref)
    add({
        "type": "timeseries", "title": "Years younger, over time",
        "description": "Minus the sum of the nine age impacts, each day from the 6 months (solid) and the "
                       "30 days (dashed) before it. Up is younger. " + MODEL,
        "datasource": MIXED, "targets": r6 + r30,
        "transformations": [JOIN] + total(rn6, "6 months", times=-1) + total(rn30, "30 days", times=-1)
                           + [keep("Time", "6 months", "30 days")],
        "fieldConfig": {
            "defaults": {"unit": "suffix: years", "decimals": 1, "color": {"mode": "fixed", "fixedColor": "text"},
                         "thresholds": thresholds([(None, O2), (-1, O1), (0, G1), (1, G2)]),
                         "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                                    "spanNulls": True, "thresholdsStyle": {"mode": "area"}}},
            "overrides": [{"matcher": {"id": "byName", "options": "30 days"},
                           "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}},
                                          {"id": "custom.lineWidth", "value": 1}]}]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}},
    }, 10, y, 14, 8)
    y += 8

    section = None
    for ind in INDICATORS:
        if ind["section"] != section:
            section = ind["section"]
            row(section, y)
            y += 1
        lo, hi = ind["scale"]
        per_window = [(days, name, source) for days, name in WINDOWS for source in ind["sources"]]
        dbs = {source[0] for source in ind["sources"]}
        ds = MIXED if len(dbs) > 1 else DS[next(iter(dbs))]
        picked = "" if ind["key"] == "vo2" else " From the source picked for it."
        ref = refs()
        add({
            "type": "bargauge", "title": ind["title"],
            "description": f"{ind['note']}{picked} The bar spans WHOOP's scale, {lo:g} to {hi:g}, in its "
                           "colours: orange adds years, grey is about 0, green takes them off.",
            "datasource": ds,
            "targets": [target(next(ref), s[0], single_sql(ind, s, days), name) for days, name, s in per_window],
            "fieldConfig": {"defaults": {"unit": ind["unit"], "decimals": ind["decimals"], "min": lo, "max": hi,
                                         "color": {"mode": "thresholds"}, "thresholds": thresholds(ind["bands"])},
                            "overrides": []},
            "options": {"displayMode": "lcd", "orientation": "horizontal", "showUnfilled": True,
                        "valueMode": "text", "namePlacement": "top", "sizing": "manual",
                        "minVizWidth": 8, "minVizHeight": 46, "maxVizHeight": 46,
                        "text": {"titleSize": 12, "valueSize": 18},
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"}},
        }, 0, y, 11, 5)
        ref = refs()
        add(stat("Age impact",
                 f"years = {ind['k']:.4g} × (value − {ind['target']:g}), from each average. Negative is younger.",
                 [target(next(ref), s[0], single_sql(ind, s, days, years=True), name) for days, name, s in per_window],
                 [], "suffix: years", 1, YEARS_BANDS, color_mode="background", text_mode="value_and_name"),
            11, y, 4, 5)
        panels[-1]["options"]["orientation"] = "vertical"
        ref = refs()
        add({
            "type": "timeseries", "title": f"{ind['title']}, over time",
            "description": "Each day, the average of the 6 months (solid) and of the 30 days (dashed) before "
                           "it, over WHOOP's colours.",
            "datasource": ds,
            "targets": [target(next(ref), s[0], rolling_sql(ind, s, days), name) for days, name, s in per_window],
            "fieldConfig": {
                "defaults": {"unit": ind["unit"], "decimals": ind["decimals"],
                             "color": {"mode": "fixed", "fixedColor": "text"},
                             "thresholds": thresholds(ind["bands"]),
                             "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                                        "spanNulls": True, "axisSoftMin": lo, "axisSoftMax": hi,
                                        "thresholdsStyle": {"mode": "area"}}},
                "overrides": [{"matcher": {"id": "byName", "options": WINDOWS[1][1]},
                               "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}},
                                              {"id": "custom.lineWidth", "value": 1}]}]},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                        "tooltip": {"mode": "multi", "sort": "none"}},
        }, 15, y, 9, 5)
        y += 5

    # The last row is kept from the JSON, collapsed: the tiles and every-source
    # trends of the markers outside WHOOP's nine.
    kept = next(p for p in existing["panels"] if p.get("type") == "row" and p["title"] == KEPT_ROW)
    kept_panels = kept["panels"]
    base = min((p["gridPos"]["y"] for p in kept_panels), default=0)
    for p in kept_panels:
        p["gridPos"]["y"] = p["gridPos"]["y"] - base + y + 1
    add({"type": "row", "title": KEPT_ROW, "collapsed": True, "panels": kept_panels}, 0, y, 24, 1)
    # Their own ids, after the generated ones: Grafana wants every id unique.
    for p in kept_panels:
        p["id"] = next(ids)

    variables = existing["templating"]["list"]
    if not any(v["name"] == "birth" for v in variables):
        at = next(i for i, v in enumerate(variables) if v["name"] == "tz") + 1
        variables.insert(at, {"name": "birth", "label": "Birth date", "type": "textbox", "query": "",
                              "current": {}, "hide": 0,
                              "description": "YYYY-MM-DD: the chronological age, for WHOOP Age. Nothing "
                                             "else needs it."})

    return {
        **{k: v for k, v in existing.items() if k not in ("panels", "templating")},
        "description": "WHOOP's Healthspan screen for any source: nine indicators of sleep, strain and "
                       "fitness on WHOOP's scales and colours, their age impacts and an estimated WHOOP Age. "
                       "Below, collapsed, the other long-run markers from every source. " + MODEL,
        "time": {"from": "now-1y", "to": "now"},
        "templating": {"list": variables},
        "panels": panels,
    }


def main():
    existing = json.loads(OUT.read_text(encoding="utf-8"))
    dashboard = build(existing)
    OUT.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(OUT.parents[2])}: {len(dashboard['panels'])} panels")


if __name__ == "__main__":
    main()
