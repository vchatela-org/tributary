#!/usr/bin/env python3
"""Writes grafana/dashboards/sleep-planner.json: WHOOP's sleep planner and its
scores rebuilt from each device, as the WHOOP models connector writes them
(SleepStats: whoop_model, whoop_model_tonight), next to the WHOOP app's own
values when a strap writes them (sleep_session, daily_activity).

Every query is written here: edit this file, run
`python3 scripts/build-sleep-planner-dashboard.py`, commit both files
(tests/test_dashboards.py checks they agree).

Tonight: the bedtime for the goal in $goal (Peak 1, Perform 0.85, Get by 0.7)
and the wake-up in $wake (seconds after midnight), as an instant:
`wake_day_s + $wake − goal × need × 100 / efficiency − latency`, in ms, which
Grafana writes as a clock time (`time:HH:mm`, in the dashboard's time zone);
once per device ($input, a repeated panel), at the strain so far and at 8, 12,
16: today's strain is not known before the day ends. The other way round, for
the bedtime in $bed (seconds after the midnight before the night), the share
of the need slept: `($wake + 86400 − $bed − latency) × efficiency / need`.
Then how fresh the night under it is (whoop_model_tonight's `run_s`,
`night_*`, `synced_s`, `partial`, `cut_short`), with a "Refresh now" link to
Tributary's refresh page (app/refresh.py) at the address in $tributary: a
hidden textbox, empty here, that a deployment sets to the Tributary its
readers reach (docs/dashboards.md).

The rebuilt scores: one series per device (`input`), solid, in its source
colour, and the app's own value per source dashed in grey ("whoop app"). Every
night is cut noon to noon, so a WHOOP cycle (stamped at sleep onset) and the
night it starts with share a bucket.
"""
import json
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "grafana" / "dashboards" / "sleep-planner.json"

SLEEP = {"type": "influxdb", "uid": "${sleep}"}
BODY = {"type": "influxdb", "uid": "${body}"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
USER = "\"user\" = '$user'"
INPUT = '"input" =~ /^${input:regex}$/'
NIGHT = "GROUP BY time(1d, -12h), \"{tag}\" fill(none) tz('$tz')"
LATEST = "time > now() - 7d"
# docs/dashboards-design.md, rule 2: a device keeps its source's colour.
COLOURS = {"whoop": "#9085e9", "fitbit": "#d55181", "garmin": "#d95926"}
APP = "#8c8b84"  # the app's own value, a neutral
WAKE = [f"{h:02d}:{m:02d}" for h in range(4, 11) for m in (0, 15, 30, 45)]
# Bedtimes from 20:00 to 02:00; one after midnight counts from the midnight before
# (00:30 is 86400 + 1800), so wake-up + 86400 − bedtime is the time in bed.
BED = [f"{h % 24:02d}:{m:02d}" for h in range(20, 27) for m in (0, 15, 30, 45)][:-3]
GOALS = [("Peak (100 %)", "1"), ("Perform (85 %)", "0.85"), ("Get by (70 %)", "0.7")]


def clock_s(t, after_midnight_is_tomorrow=False):
    h, m = int(t[:2]), int(t[3:])
    return h * 3600 + m * 60 + (86400 if after_midnight_is_tomorrow and h < 12 else 0)

_ids = count(1)


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def target(query, datasource=SLEEP, alias=None, ref=None):
    t = {"refId": ref, "datasource": datasource, "rawQuery": True, "resultFormat": "time_series", "query": query}
    if alias:
        t["alias"] = alias
    return t


def refs(targets):
    for i, t in enumerate(targets):
        t["refId"] = t["refId"] or chr(ord("A") + i)
    return targets


def row(title, y):
    return {"type": "row", "title": title, "id": next(_ids), "collapsed": False, "gridPos": pos(0, y, 24, 1),
            "panels": []}


def text(content, p):
    return {"type": "text", "title": "", "id": next(_ids), "gridPos": p,
            "options": {"mode": "markdown", "content": content}}


def stat(title, description, targets, unit, p, decimals=None, repeat=None, mappings=(), datasource=SLEEP):
    defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": "text"}, "mappings": list(mappings),
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    panel = {"type": "stat", "title": title, "id": next(_ids), "description": description, "datasource": datasource,
             "gridPos": p, "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto",
                         "textMode": "value_and_name", "wideLayout": True, "showPercentChange": False,
                         "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
             "targets": refs(targets)}
    if repeat:
        panel.update({"repeat": repeat, "repeatDirection": "h", "maxPerRow": 3})
    return panel


# docs/dashboards-design.md, rule 7: how much of the need a bedtime gives, on
# Healthspan's diverging scale (short of the goal in orange, met in green).
O2, O1, G1, G2 = "#f8bc5e", "#b8803f", "#22a07b", "#5ff2c0"
SCENARIOS = [("strain so far", "need_s_so_far")] + [(f"strain {s}", f"need_s_strain{s}") for s in (8, 12, 16)]
PLAN = f'FROM "whoop_model_tonight" WHERE {USER} AND "input" = \'$input\' AND {LATEST}'
# The bedtime as an instant (epoch ms): the coming wake-up's day, plus the
# wake-up, less the time in bed the goal needs. Written as a clock time.
CLOCK = "time:HH:mm"
NEED_COLOUR = [{"color": O2, "value": None}, {"color": O1, "value": 70}, {"color": G1, "value": 85},
               {"color": G2, "value": 100}]


def bedtime(field):
    return (f'(last("wake_day_s") + $wake - $goal * last("{field}") * 100 / last("efficiency") '
            f'- last("latency_s")) * 1000')


# Asleep for an in-bed time from $bed to $wake: (in bed − time to fall asleep) × efficiency.
IN_BED = '($wake + 86400 - $bed - last("latency_s"))'


def share_of_need(field):
    return f'{IN_BED} * last("efficiency") / last("{field}")'


def bed_by(p):
    q = f'SELECT {bedtime("need_s_so_far")} AS "in bed by" {PLAN}'
    panel = stat("$input · in bed by", "When to be in bed for the goal and the wake-up picked above, if the day ends "
                 "at the strain so far: wake-up − goal × sleep need ÷ the 14-night median efficiency − time to fall "
                 "asleep. The need is the baseline, what today's strain adds and the debt carried from last night.",
                 [target(q, alias="$col")], CLOCK, p, repeat="input")
    panel["options"].update({"textMode": "value", "justifyMode": "center"})
    return panel


def if_the_day_ends(p):
    select = ", ".join(f'{bedtime(field)} AS "{label}"' for label, field in SCENARIOS[1:])
    panel = stat("$input · if the day ends at strain", "Today's strain adds to tonight's need and is not known "
                 "before the day ends: the same bedtime if it ends at strain 8, 12 or 16.",
                 [target(f"SELECT {select} {PLAN}", alias="$col")], CLOCK, p, repeat="input")
    panel["options"].update({"textMode": "value_and_name", "justifyMode": "center", "orientation": "vertical"})
    return panel


def bed_at(p):
    select = ", ".join(f'{share_of_need(field)} AS "{label}"' for label, field in SCENARIOS)
    defaults = {"unit": "percent", "decimals": 0, "min": 0, "max": 120, "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": NEED_COLOUR}}
    return {"type": "bargauge", "title": "$input · share of the need slept", "id": next(_ids),
            "description": "The other way round: in bed at the time picked above, up at the wake-up, how much of "
                           "tonight's need you would sleep: (in bed − time to fall asleep) × efficiency ÷ need, at the "
                           "strain so far and at 8, 12, 16. Orange below 70 % (Get by) and 85 % (Perform), green from "
                           "85 %, bright green from 100 % (Peak).",
            "datasource": SLEEP, "gridPos": p, "repeat": "input", "repeatDirection": "h", "maxPerRow": 3,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"displayMode": "basic", "orientation": "horizontal", "valueMode": "text",
                        "showUnfilled": True, "namePlacement": "left", "sizing": "auto", "minVizHeight": 16,
                        "maxVizHeight": 28, "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "targets": refs([target(f"SELECT {select} {PLAN}", alias="$col")])}


def sleep_you_get(p):
    q = (f'SELECT {IN_BED} * last("efficiency") / 100 / 3600 AS "asleep", '
         f'last("need_s_so_far") / 3600 AS "need" {PLAN}')
    panel = stat("$input · in bed at ${bed:text}", "The sleep you would get in bed from the time picked to the "
                 "wake-up, against tonight's need at the strain so far.", [target(q, alias="$col")], "suffix: h", p,
                 decimals=1, repeat="input")
    panel["options"].update({"justifyMode": "center"})
    return panel


STATUS = [{"type": "value", "options": {
    "0": {"text": "complete", "color": "transparent", "index": 0},
    "1": {"text": "still syncing", "color": O2, "index": 1},
    "2": {"text": "looks cut short", "color": O2, "index": 2},
    "3": {"text": "cut short, still syncing", "color": O2, "index": 3}}}]


def freshness(p):
    q = (f'SELECT last("run_s") * 1000 AS "model ran", last("night_start_s") * 1000 AS "fell asleep", '
         f'last("night_end_s") * 1000 AS "woke up", last("night_asleep_s") / 3600 AS "slept", '
         f'last("usual_asleep_s") / 3600 AS "usually", last("partial") + 2 * last("cut_short") AS "last night", '
         f'last("synced_s") * 1000 AS "device last synced" FROM "whoop_model_tonight" WHERE {USER} AND {INPUT} '
         f'AND {LATEST} GROUP BY "input"')
    t = target(q)
    t["resultFormat"] = "table"

    def override(name, *props):
        return {"matcher": {"id": "byName", "options": name},
                "properties": [{"id": k, "value": v} for k, v in props]}
    overrides = [
        override("model ran", ("unit", "dateTimeFromNow")),
        override("device last synced", ("unit", "dateTimeFromNow")),
        override("fell asleep", ("unit", "time:ddd HH:mm")),
        override("woke up", ("unit", "time:ddd HH:mm")),
        override("slept", ("unit", "suffix: h"), ("decimals", 1), ("custom.width", 80)),
        override("usually", ("unit", "suffix: h"), ("decimals", 1), ("custom.width", 80)),
        override("last night", ("mappings", STATUS),
                 ("custom.cellOptions", {"type": "color-background", "mode": "basic"})),
        override("input", ("displayName", "device"), ("custom.width", 90)),
    ]
    return {"type": "table", "title": "Is the data fresh?", "id": next(_ids),
            "description": "What the bedtimes above stand on, per device: when the WHOOP models connector last ran "
                           "(*Refresh now*, above, after a late workout or a sync), the last night it read, its "
                           "sleep against the usual (the median of the 14 nights before), and when the device's heart "
                           "rate was last seen. *Still syncing*: the heart rate stops before the night's end. *Looks "
                           "cut short*: under 60 % of the usual sleep, or ending 3 h or more before the usual wake-up: "
                           "the source had not finished the night (a Fitbit night is finished by Google Health hours "
                           "after the wake-up). Either way the debt is too high and the bedtime too early, until the "
                           "source rewrites the night and the connector runs again.",
            "datasource": SLEEP, "gridPos": p,
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "minWidth": 70},
                                         "color": {"mode": "fixed", "fixedColor": "text"}, "mappings": [],
                                         "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                            "overrides": overrides},
            "options": {"showHeader": True, "cellHeight": "md", "footer": {"show": False}},
            "transformations": [{"id": "merge", "options": {}},
                                {"id": "organize", "options": {"excludeByName": {"Time": True}, "indexByName": {
                                    "input": 0, "last night": 1, "fell asleep": 2, "woke up": 3, "slept": 4,
                                    "usually": 5, "model ran": 6, "device last synced": 7}}}],
            "targets": refs([t])}


# Tributary's refresh page for the WHOOP models connector (app/refresh.py), at
# the address the deployment puts in $tributary.
REFRESH_URL = "${tributary}/connectors/whoop_model/refresh"
REFRESH = f"""**[↻ Refresh now]({REFRESH_URL})**: fetch the latest data (the tools with a refresh hook are asked to
fetch, the Whoop connector runs), then rerun the WHOOP models. Open the device's phone app first, so it has synced.
The link needs the dashboard's `tributary` variable: the address of Tributary."""


def per_input(title, description, select, unit, p, decimals=1):
    """A tile per device: the latest planner value."""
    q = f'SELECT {select} FROM "whoop_model_tonight" WHERE {USER} AND {INPUT} AND {LATEST} GROUP BY "input"'
    return stat(title, description, [target(q, alias="$tag_input")], unit, p, decimals=decimals)


def timeseries(title, description, targets, unit, p, decimals=0, datasource=MIXED):
    defaults = {"unit": unit, "decimals": decimals, "color": {"mode": "palette-classic-by-name"},
                "custom": {"drawStyle": "line", "lineWidth": 2, "pointSize": 4, "showPoints": "always",
                           "spanNulls": False, "fillOpacity": 0, "axisBorderShow": False,
                           "stacking": {"mode": "none", "group": "A"}}}
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hex_}}]}
                 for name, hex_ in COLOURS.items()]
    overrides.append({"matcher": {"id": "byRegexp", "options": "/ app$/"},
                      "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": APP}},
                                     {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}}]})
    return {"type": "timeseries", "title": title, "id": next(_ids), "description": description,
            "datasource": datasource, "gridPos": p, "fieldConfig": {"defaults": defaults, "overrides": overrides},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "none"}},
            "targets": refs(targets)}


def model(field, scale=""):
    return target(f'SELECT mean("{field}"){scale} FROM "whoop_model" WHERE {USER} AND {INPUT} AND $timeFilter '
                  + NIGHT.format(tag="input"), alias="$tag_input")


def app(select, measurement="sleep_session", datasource=SLEEP, night=True):
    kind = " AND \"kind\" = 'night'" if night else ""
    return target(f'SELECT {select} FROM "{measurement}" WHERE {USER}{kind} AND $timeFilter '
                  + NIGHT.format(tag="source"), datasource=datasource, alias="$tag_source app")


ABOUT = """**WHOOP's sleep planner and scores, rebuilt from any device.** The *WHOOP models* connector reads each
device's nights and heart rate (a WHOOP strap through the Whoop connector, a Fitbit through fitbit-grafana, a Garmin
through garmin-grafana) and recomputes what the WHOOP app shows: the **sleep need** (a baseline, plus what the day's
strain adds, plus half of last night's shortfall), the **sleep performance**, the **consistency**, the **recovery**
(the night's HRV against its own 30-night mean) and the **day strain** (from the heart rate, for a device that has no
strain of its own). One line per device; a dashed grey line is the app's own value, where a strap writes it.
How the models were found and how close they come: docs/connectors/whoop_model.md."""


def build():
    global _ids
    _ids = count(1)
    panels = [text(ABOUT, pos(0, 0, 24, 4))]
    y = 4
    panels.append(row("Tonight: when to go to bed for ${goal:text}, up at ${wake:text}", y))
    y += 1
    panels.append(bed_by(pos(0, y, 8, 5)))
    y += 5
    panels.append(if_the_day_ends(pos(0, y, 8, 4)))
    y += 4
    panels.append(row("If I go to bed at ${bed:text}", y))
    y += 1
    panels.append(bed_at(pos(0, y, 8, 5)))
    y += 5
    panels.append(sleep_you_get(pos(0, y, 8, 3)))
    y += 3
    panels.append(row("Is the data fresh?", y))
    y += 1
    panels.append(text(REFRESH, pos(0, y, 24, 2)))
    y += 2
    panels.append(freshness(pos(0, y, 24, 6)))
    y += 6
    panels.append(row("Tonight's numbers", y))
    y += 1
    tiles = [
        ("Sleep need tonight", "Baseline + what the strain so far adds + the debt carried from last night.",
         'last("need_s_so_far") / 3600', "suffix: h", 1),
        ("Debt carried from last night", "Half of last night's shortfall (need − sleep); 9/14 after a night scored "
         "above 87.", 'last("debt_s") / 60', "suffix: min", 0),
        ("Strain so far", "Today's cycle, from last night's sleep onset: WHOOP's own for a strap, from the heart rate "
         "otherwise.", 'last("strain_so_far")', "none", 1),
        ("Sleep need at strain 12", "Baseline + what a day at strain 12 adds + the debt.",
         'last("need_s_strain12") / 3600', "suffix: h", 1),
        ("Efficiency", "Time asleep ÷ time in bed, the median of the last 14 nights.", 'last("efficiency")',
         "percent", 0),
        ("Time to fall asleep", "The median of the last 14 nights.", 'last("latency_s") / 60', "suffix: min", 0),
    ]
    for i, (title, description, select, unit, decimals) in enumerate(tiles):
        panels.append(per_input(title, description, select, unit, pos(8 * (i % 3), y + 4 * (i // 3), 8, 4),
                                decimals=decimals))
    y += 8
    panels.append(row("WHOOP's scores, rebuilt from each device", y))
    y += 1
    charts = [
        ("Recovery", "The app's recovery and the model's: 100·σ(h·(ln HRV − its 30-night mean) + p·performance + c).",
         [app('mean("recovery_score")'), model("recovery")], "percent", 0),
        ("Day strain", "The cycle that starts with the night (sleep onset to the next one): WHOOP's own for a strap; "
         "from the heart rate otherwise, a·ln(1 + Banister's TRIMP / b).",
         [app('mean("strain")', "daily_activity", BODY, night=False), model("day_strain")], "none", 1),
        ("Sleep need", "Baseline + strain + debt − naps: the app's own sum, and the model's.",
         [app('(mean("need_baseline_s") + mean("need_strain_s") + mean("need_debt_s") - mean("need_nap_s")) / 3600'),
          model("need_s", " / 3600")], "suffix: h", 2),
        ("Sleep debt carried into the night", "The share of the nights before still owed.",
         [app('mean("need_debt_s") / 60'), model("need_debt_s", " / 60")], "suffix: min", 0),
        ("Sleep performance", "The app's sleep performance, and the model's: sleep against need, efficiency and "
         "consistency.", [app('mean("score")'), model("sleep_score")], "percent", 0),
        ("Sleep consistency", "How much the night's sleep window overlaps the four before it.",
         [app('mean("consistency_pct")'), model("consistency")], "percent", 0),
    ]
    for i, (title, description, targets, unit, decimals) in enumerate(charts):
        panels.append(timeseries(title, description, targets, unit, pos(12 * (i % 2), y + 8 * (i // 2), 12, 8),
                                 decimals=decimals))
    return {
        "uid": "tributary-sleep-planner",
        "title": "Sleep planner & WHOOP models",
        "description": "WHOOP's sleep planner (when to go to bed) and its scores (sleep need and debt, performance, "
                       "consistency, recovery, strain), rebuilt from a WHOOP strap's, a Fitbit's or a Garmin's own "
                       "data by the WHOOP models connector.",
        "tags": ["tributary", "sleep"],
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 41,
        "time": {"from": "now-30d", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "refresh": "",
        "links": [
            {"type": "link", "title": title, "url": url, "icon": "dashboard", "asDropdown": False,
             "includeVars": False, "keepTime": True, "targetBlank": False, "tooltip": "", "tags": []}
            for title, url in (("Sleep", "/d/tributary-sleep"), ("Healthspan", "/d/tributary-healthspan"))
        ] + [
            {"type": "link", "title": "Refresh now", "url": REFRESH_URL, "icon": "external link",
             "asDropdown": False, "includeVars": False, "keepTime": False, "targetBlank": False,
             "tooltip": "Fetch the latest data, then rerun the WHOOP models (Tributary)", "tags": []},
        ],
        "annotations": {"list": []},
        "templating": {"list": variables()},
        "panels": panels,
    }


def variables():
    return [
        {"type": "custom", "name": "goal", "label": "Goal",
         "description": "WHOOP's sleep planner goals: the share of tonight's need to sleep.",
         "query": ", ".join(f"{text} : {value}" for text, value in GOALS),
         "hide": 0, "current": {"text": GOALS[0][0], "value": GOALS[0][1]}, "options": [], "multi": False,
         "includeAll": False},
        {"type": "custom", "name": "wake", "label": "Wake-up at",
         "description": "The wake-up the bedtimes are for (seconds after midnight).",
         "query": ", ".join(f"{t} : {clock_s(t)}" for t in WAKE),
         "hide": 0, "current": {"text": "07:00", "value": "25200"}, "options": [], "multi": False,
         "includeAll": False},
        {"type": "custom", "name": "bed", "label": "In bed at",
         "description": "A bedtime to try: the share of the need it gives (seconds after the midnight before the "
                        "night: 00:30 is 88200).",
         "query": ", ".join(f"{t} : {clock_s(t, True)}" for t in BED),
         "hide": 0, "current": {"text": "23:00", "value": "82800"}, "options": [], "multi": False,
         "includeAll": False},
        {"type": "datasource", "name": "sleep", "label": "InfluxDB SleepStats", "query": "influxdb",
         "regex": "/SleepStats/i", "hide": 0, "refresh": 1, "current": {}, "options": [], "includeAll": False,
         "multi": False},
        {"type": "datasource", "name": "body", "label": "InfluxDB BodyStats", "query": "influxdb",
         "regex": "/BodyStats/i", "hide": 0, "refresh": 1, "current": {}, "options": [], "includeAll": False,
         "multi": False},
        {"type": "textbox", "name": "tz", "label": "Time zone",
         "description": "IANA time zone the nights are cut in (TRIBUTARY_TZ).", "query": "Europe/Paris",
         "current": {"text": "Europe/Paris", "value": "Europe/Paris"},
         "options": [{"selected": True, "text": "Europe/Paris", "value": "Europe/Paris"}], "hide": 0},
        {"type": "textbox", "name": "tributary", "label": "Tributary",
         "description": "The address of Tributary (https://tributary.example.com), for the Refresh now link. Empty "
                        "until a deployment sets it.", "query": "", "current": {}, "hide": 2},
        {"type": "query", "name": "user", "label": "User", "description": "The person.", "datasource": SLEEP,
         "definition": 'SHOW TAG VALUES FROM "whoop_model" WITH KEY = "user"',
         "query": 'SHOW TAG VALUES FROM "whoop_model" WITH KEY = "user"', "refresh": 1, "sort": 1, "hide": 0,
         "current": {}, "options": [], "multi": False, "includeAll": False},
        {"type": "query", "name": "input", "label": "Devices",
         "description": "The devices WHOOP's scores are rebuilt from.", "datasource": SLEEP,
         "definition": f'SHOW TAG VALUES FROM "whoop_model" WITH KEY = "input" WHERE {USER}',
         "query": f'SHOW TAG VALUES FROM "whoop_model" WITH KEY = "input" WHERE {USER}',
         "refresh": 1, "sort": 1, "hide": 0, "current": {"text": "All", "value": "$__all"}, "options": [],
         "multi": True, "includeAll": True},
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
