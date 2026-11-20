#!/usr/bin/env python3
"""Writes grafana/dashboards/sleep-planner.json: WHOOP's sleep planner and its
scores rebuilt from each device, as the WHOOP models connector writes them
(SleepStats: whoop_model, whoop_model_tonight), next to the WHOOP app's own
values when a strap writes them (sleep_session, daily_activity).

Every query is written here: edit this file, run
`python3 scripts/build-sleep-planner-dashboard.py`, commit both files
(tests/test_dashboards.py checks they agree).

Tonight: the bedtime for the wake-up picked in $wake (seconds after midnight),
`(86400 + $wake − target × need × 100 / efficiency − latency) % 86400`,
written as a clock time by value mappings to 5 minutes (Grafana's `clocks`
unit writes 00:14 as "14m:44s"), once per device ($input, a repeated panel),
at strain 8, 12, 16 and the strain so far: today's strain is not known before
the day ends.

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


def clock_mappings():
    """Seconds after midnight as a clock time, to 5 minutes: Grafana's `clocks`
    unit writes a time between midnight and 1 am as minutes and seconds."""
    return [{"type": "range", "options": {"from": m * 60, "to": m * 60 + 299.999,
                                          "result": {"text": f"{m // 60:02d}:{m % 60:02d}", "index": i}}}
            for i, m in enumerate(range(0, 1440, 5))]


def bedtime(field, target_share):
    return (f"(86400 + $wake - {target_share} * last(\"{field}\") * 100 / last(\"efficiency\") "
            f"- last(\"latency_s\")) % 86400")


def planner(title, share, p):
    scenarios = [("strain so far", "need_s_so_far")] + [(f"strain {s}", f"need_s_strain{s}") for s in (8, 12, 16)]
    select = ", ".join(f'{bedtime(field, share)} AS "{label}"' for label, field in scenarios)
    q = f'SELECT {select} FROM "whoop_model_tonight" WHERE {USER} AND "input" = \'$input\' AND {LATEST}'
    return stat(f"{title}: $input", "When to go to bed for the wake-up picked above: wake-up − "
                 f"{'' if share == '1' else share + ' × '}sleep need ÷ the 14-night median efficiency − time to fall "
                 "asleep. Today's strain adds to the need and is not known before the day ends: the bedtime at the "
                 "strain so far, and at 8, 12 and 16.", [target(q, alias="$col")], "none", p, repeat="input",
                 mappings=clock_mappings())


def per_input(title, description, field, unit, p, scale=""):
    """A tile per device: the latest planner value."""
    q = (f'SELECT last("{field}"){scale} FROM "whoop_model_tonight" WHERE {USER} AND {INPUT} AND {LATEST} '
         f'GROUP BY "input"')
    return stat(title, description, [target(q, alias="$tag_input")], unit, p, decimals=1)


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
    panels.append(row("Tonight: when to go to bed", y))
    y += 1
    panels.append(planner("Peak (100 %)", "1", pos(0, y, 8, 6)))
    y += 6
    panels.append(planner("Perform (85 %)", "0.85", pos(0, y, 8, 6)))
    y += 6
    panels.append(per_input("Sleep need tonight at strain 12", "Baseline + what a day at strain 12 adds + the debt "
                            "carried from last night.", "need_s_strain12", "suffix: h", pos(0, y, 6, 5),
                            scale=" / 3600"))
    panels.append(per_input("Debt carried from last night", "Half of last night's shortfall (need − sleep); 9/14 "
                            "after a night scored above 87.", "debt_s", "suffix: min", pos(6, y, 6, 5), scale=" / 60"))
    panels.append(per_input("Strain so far", "Today's cycle, from last night's sleep onset: WHOOP's own for a strap, "
                            "from the heart rate otherwise.", "strain_so_far", "none", pos(12, y, 6, 5)))
    synced = {"type": "value", "options": {"0": {"text": "whole night", "index": 0},
                                           "1": {"text": "not synced yet", "index": 1}}}
    panels.append(stat("Last night", "Whether the device had synced the whole of last night when the model last "
                       "ran: a partial night has too little sleep, and its debt is too high until the next run.",
                       [target(f'SELECT last("partial") FROM "whoop_model_tonight" WHERE {USER} AND {INPUT} '
                               f'AND {LATEST} GROUP BY "input"', alias="$tag_input")], "none", pos(18, y, 6, 5),
                       mappings=[synced]))
    y += 5
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
        ],
        "annotations": {"list": []},
        "templating": {"list": variables()},
        "panels": panels,
    }


def variables():
    return [
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
        {"type": "custom", "name": "wake", "label": "Wake-up at",
         "description": "The wake-up the bedtimes are for (seconds after midnight).",
         "query": ", ".join(f"{t} : {int(t[:2]) * 3600 + int(t[3:]) * 60}" for t in WAKE),
         "hide": 0, "current": {"text": "07:00", "value": "25200"}, "options": [], "multi": False,
         "includeAll": False},
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
