#!/usr/bin/env python3
"""Writes grafana/dashboards/phone.json: a phone's screen time, as the App Usage
connector writes it (DigitalStats: screen_time, app_usage, app_session, unlock).

Every query is written here: edit this file, run
`python3 scripts/build-phone-dashboard.py`, commit both files
(tests/test_dashboards.py checks they agree).

Not every minute of screen is the same use. Each app carries a `category`, from
Tributary's vocabulary (app/connectors/app_usage_categories.py); the dashboard
draws them in seven GROUPS, the first four hands-on (the phone in hand: feeds,
messages, tools, the rest), the last three lean-back (a video or music playing,
a map while driving, the home screen between two apps). Seven, because a stack
of seventeen colours can't be read; the ranked bars and the table at the bottom
keep every category and every app.

A value "per day" is a total over the time range divided by the days it holds
data (screen_time has one point per day), with Grafana's expressions: a range
that starts before the first backup does not dilute it.
"""

import json
import sys
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "grafana" / "dashboards" / "phone.json"
sys.path.insert(0, str(ROOT))
from app.connectors.app_usage_categories import CATEGORIES, LEAN_BACK  # noqa: E402

DIGITAL = {"type": "influxdb", "uid": "${digital}"}
EXPR = {"type": "__expr__", "uid": "__expr__"}

# (name, categories, colour): the reference palette's first seven slots, dark
# steps, in its validated order (adjacent pairs clear the CVD and normal-vision
# floors in light and dark; the yellow sits at 2.99:1 on the light surface, so
# every stack has its legend and the table below names every app).
GROUPS = (
    ("Social, news & web", ("social", "news", "browsing"), "#3987e5"),
    ("Messages & calls", ("communication",), "#d95926"),
    ("Work & tools", ("productivity", "finance", "system"), "#199e70"),
    ("Home, health & other", ("smart_home", "health", "shopping", "photos", "games", "other"), "#c98500"),
    ("Watching & listening", ("watching", "listening"), "#d55181"),
    ("Maps", ("navigation",), "#008300"),
    ("Home screen", ("launcher",), "#9085e9"),
)
assert sorted(c for _n, cats, _c in GROUPS for c in cats) == sorted(CATEGORIES), "a category in no group, or two"
HANDS_ON = tuple(c for c in CATEGORIES if c not in LEAN_BACK)
WATCHING = ("watching", "listening")
# A single series that is none of the groups (screen on, unlocks, hands-on time
# by hour): a neutral, never a group's hue.
NEUTRAL = "#8c8b84"

USER = "\"user\" = '$user'"
PICKED = '"category" =~ /^${category:regex}$/'
DAY = "GROUP BY time(1d) fill(none) tz('$tz')"
NIGHT = "GROUP BY time(1d, -12h) fill(none) tz('$tz')"
AT_NIGHT = '("local_hour" >= 22 OR "local_hour" < 6)'
VALUE = "v"
# Hours that stay hours: Grafana's `h` turns 30 hours into "1.3 days".
HOURS = "suffix: h"

_ids = count(1)


def among(categories):
    return '"category" =~ /^(' + "|".join(categories) + ')$/'


def apps(*conditions):
    """The WHERE of app_usage / app_session: the user, the picked categories."""
    return " AND ".join((USER, PICKED) + conditions + ("$timeFilter",))


def target(query, alias=None, fmt="time_series", ref=None, hide=False):
    """A hidden target is an expression's input: all of them are named VALUE,
    or Grafana's expressions see labels that differ (app_usage.sum,
    screen_time.count), drop the pair from their union, and warn."""
    alias = VALUE if hide and alias is None else alias
    t = {"refId": ref, "datasource": DIGITAL, "rawQuery": True, "resultFormat": fmt, "query": query}
    if alias:
        t["alias"] = alias
    if hide:
        t["hide"] = True
    return t


def math(ref, expression):
    return {"refId": ref, "datasource": EXPR, "type": "math", "expression": expression, "hide": False}


def refs(targets):
    for i, t in enumerate(targets):
        t["refId"] = t["refId"] or chr(ord("A") + i)
    return targets


def colour(name, hex_):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hex_}}]}


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def row(title, y):
    return {"type": "row", "title": title, "id": next(_ids), "collapsed": False, "gridPos": pos(0, y, 24, 1),
            "panels": []}


def stat(title, description, targets, unit, p, decimals=None):
    defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": "text"}, "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "id": next(_ids), "description": description, "datasource": DIGITAL,
            "gridPos": p, "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"colorMode": "value", "graphMode": "none", "justifyMode": "auto", "orientation": "auto",
                        "textMode": "value", "wideLayout": True, "showPercentChange": False,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "targets": refs(targets)}


def per_day(select, where, measurement="app_usage"):
    """A total over the range divided by the days that have data: [A, DAYS, value]."""
    return [target(f'SELECT {select} FROM "{measurement}" WHERE {where}', ref="A", hide=True),
            target(f'SELECT count("screen_on_s") FROM "screen_time" WHERE {USER} AND $timeFilter', ref="DAYS",
                   hide=True),
            math("V", "$A / $DAYS")]


def bars(title, description, targets, unit, p, overrides=(), stack=True, decimals=None, legend=True):
    custom = {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 90, "showPoints": "never", "barAlignment": -1,
              "axisBorderShow": False, "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": "off"}}
    defaults = {"unit": unit, "min": 0, "custom": custom, "color": {"mode": "fixed", "fixedColor": NEUTRAL}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "timeseries", "title": title, "id": next(_ids), "description": description,
            "datasource": DIGITAL, "gridPos": p, "fieldConfig": {"defaults": defaults, "overrides": list(overrides)},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": legend},
                        "tooltip": {"mode": "multi", "sort": "none"}},
            "targets": refs(targets)}


def by_group(field, measurement, group_by, conditions=(), skip=()):
    """One target per group (InfluxQL groups by a tag, never by a list of its
    values): its hours, named after the group."""
    return [target(f'SELECT sum("{field}") / 3600 AS "{name}" FROM "{measurement}" '
                   f'WHERE {apps(among(cats), *conditions)} {group_by}', alias=name)
            for name, cats, _c in GROUPS if name not in skip]


def group_colours(skip=()):
    return [colour(name, hex_) for name, _cats, hex_ in GROUPS if name not in skip]


def by_hour(title, description, categories, hex_, p):
    """Minutes per day in each hour of the day: each hour's total over the range,
    divided by the days with data (expressions), one bar per hour. Each hour's
    sum also reads the user's sessions times 0 from the same measurement, which
    InfluxDB merges into the same series: an hour never used is a 0, not a
    missing bar (fill() only fills a series that exists)."""
    targets = [target(f'SELECT count("screen_on_s") FROM "screen_time" WHERE {USER} AND $timeFilter', ref="DAYS",
                      hide=True)]
    for h in range(24):
        used = among(categories) + f' AND "local_hour" = {h}'
        targets.append(target(f'SELECT sum("v") FROM (SELECT "duration_s" / 60 AS "v" FROM "app_session" '
                              f'WHERE {USER} AND {PICKED} AND {used}), (SELECT "count" * 0 AS "v" '
                              f'FROM "app_session" WHERE {USER}) WHERE $timeFilter', ref=f"H{h:02d}", hide=True))
    targets += [math(f"{h:02d}h", f"$H{h:02d} / $DAYS") for h in range(24)]
    return {"type": "barchart", "title": title, "id": next(_ids), "description": description,
            "datasource": DIGITAL, "gridPos": p,
            "fieldConfig": {"defaults": {"unit": "m", "min": 0, "decimals": 0,
                                         "color": {"mode": "fixed", "fixedColor": hex_},
                                         "custom": {"fillOpacity": 90, "lineWidth": 0, "axisBorderShow": False}},
                            "overrides": []},
            "options": {"orientation": "vertical", "xField": "Field", "showValue": "never", "barWidth": 0.8,
                        "groupWidth": 0.7, "legend": {"showLegend": False, "displayMode": "list",
                                                      "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}, "xTickLabelSpacing": 0,
                        "stacking": "none"},
            # An expression's output is named after its refId and its inputs' label
            # ("07h v"): the hour alone.
            "transformations": [{"id": "renameByRegex", "options": {"regex": "^(\\d\\dh).*$",
                                                                   "renamePattern": "$1"}},
                                {"id": "reduce", "options": {"mode": "seriesToRows", "reducers": ["lastNotNull"],
                                                             "includeTimeField": False}}],
            "targets": refs(targets)}


def build():
    global _ids
    _ids = count(1)
    hands_on = among(HANDS_ON)
    y = 0
    panels = [row("Per day, over the time range", y)]
    y += 1
    tiles = [
        stat("Screen on", "How long the screen was on, per day: the phone's own figure, lock screen and calls "
             "included.", [target(f'SELECT mean("screen_on_s") FROM "screen_time" WHERE {USER} AND $timeFilter')],
             "s", pos(0, y, 4, 4)),
        stat("Hands-on", "Time in apps of the hands-on groups (feeds, messages, tools, the rest), per day: not a "
             "video or music playing, a map while driving, or the home screen.",
             per_day('sum("foreground_s")', apps(hands_on)), "s", pos(4, y, 4, 4)),
        stat("Watching & listening", "Time in video and audio apps, per day.",
             per_day('sum("foreground_s")', apps(among(WATCHING))), "s", pos(8, y, 4, 4)),
        stat("Unlocks", "Times the phone was unlocked, per day (the screen woken and left locked is not one).",
             [target(f'SELECT mean("unlocks") FROM "screen_time" WHERE {USER} AND $timeFilter')], "none",
             pos(12, y, 4, 4), decimals=0),
        stat("Hands-on per unlock", "Hands-on time divided by the unlocks: how long the phone stays in hand once "
             "picked up.",
             [target(f'SELECT sum("foreground_s") FROM "app_usage" WHERE {apps(hands_on)}', ref="A", hide=True),
              target(f'SELECT sum("unlocks") FROM "screen_time" WHERE {USER} AND $timeFilter', ref="U",
                     hide=True),
              math("V", "$A / $U")], "s", pos(16, y, 4, 4)),
        stat("At night", "Time in apps between 22:00 and 06:00 (local time), per day, the home screen left out.",
             per_day('sum("duration_s")', apps(among(tuple(c for c in CATEGORIES if c != "launcher")), AT_NIGHT),
                     "app_session"), "s", pos(20, y, 4, 4)),
    ]
    panels += tiles
    y += 4

    panels.append(row("Day by day", y))
    y += 1
    panels.append(bars(
        "Time in apps per day, by group",
        "Hours in the foreground per day, stacked by group: the hands-on groups at the bottom, then a video or "
        "music playing, a map, and the home screen. The category filter above applies.",
        by_group("foreground_s", "app_usage", DAY), HOURS, pos(0, y, 24, 9), overrides=group_colours(), decimals=1))
    y += 9
    panels.append(bars(
        "Screen on per day", "How long the screen was on, per day (the phone's own figure).",
        [target(f'SELECT sum("screen_on_s") / 3600 AS "Screen on" FROM "screen_time" WHERE {USER} AND $timeFilter '
                f'{DAY}')], HOURS, pos(0, y, 12, 7), stack=False, decimals=1, legend=False))
    panels.append(bars(
        "Unlocks per day", "Times the phone was unlocked, per day.",
        [target(f'SELECT sum("unlocks") AS "Unlocks" FROM "screen_time" WHERE {USER} AND $timeFilter {DAY}')],
        "none", pos(12, y, 12, 7), stack=False, decimals=0, legend=False))
    y += 7

    panels.append(row("When in the day", y))
    y += 1
    panels.append(by_hour("Hands-on, by hour of the day",
                          "Minutes per day in each hour (local time) in the hands-on groups' apps, over the time "
                          "range: when the phone is in hand.", HANDS_ON, NEUTRAL, pos(0, y, 12, 8)))
    panels.append(by_hour("Watching & listening, by hour of the day",
                          "Minutes per day in each hour (local time) in video and audio apps, over the time range.",
                          WATCHING, GROUPS[4][2], pos(12, y, 12, 8)))
    y += 8
    panels.append(bars(
        "At night (22:00 to 06:00), per night",
        "Hours in apps between 22:00 and 06:00 (local time), each night from noon to noon, stacked by group; the "
        "home screen left out. Next to the Sleep dashboard: the evenings the phone stays in hand.",
        by_group("duration_s", "app_session", NIGHT, (AT_NIGHT,), skip=("Home screen",)), HOURS,
        pos(0, y, 24, 8), overrides=group_colours(skip=("Home screen",)), decimals=1))
    y += 8

    panels.append(row("Apps and categories", y))
    y += 1
    panels.append({
        "type": "barchart", "title": "Time by category",
        "id": next(_ids),
        "description": "Hours in the foreground over the time range, per category (Tributary's: "
                       "app/connectors/app_usage_categories.py; yours in TRIBUTARY_APP_USAGE_CATEGORIES).",
        "datasource": DIGITAL, "gridPos": pos(0, y, 8, 12),
        "fieldConfig": {"defaults": {"unit": HOURS, "min": 0, "decimals": 1,
                                     "color": {"mode": "fixed", "fixedColor": NEUTRAL},
                                     "custom": {"fillOpacity": 90, "lineWidth": 0, "axisBorderShow": False}},
                        "overrides": []},
        "options": {"orientation": "horizontal", "xField": "category", "showValue": "always", "barWidth": 0.8,
                    "groupWidth": 0.7, "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "single", "sort": "none"}, "stacking": "none"},
        "transformations": [{"id": "sortBy", "options": {"sort": [{"field": "hours", "desc": True}]}},
                            {"id": "organize", "options": {"excludeByName": {"Time": True}}}],
        "targets": refs([target(f'SELECT sum("foreground_s") / 3600 AS "hours" FROM "app_usage" WHERE {apps()} '
                                'GROUP BY "category"', fmt="table")]),
    })
    panels.append({
        "type": "table", "title": "Apps", "id": next(_ids),
        "description": "Every app used over the time range: its category, hours in the foreground, launches and "
                       "minutes per launch. Sort by any column.",
        "datasource": DIGITAL, "gridPos": pos(8, y, 16, 12),
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": "hours"},
                             "properties": [{"id": "unit", "value": HOURS}, {"id": "decimals", "value": 1}]},
                            {"matcher": {"id": "byName", "options": "minutes per launch"},
                             "properties": [{"id": "unit", "value": "m"}, {"id": "decimals", "value": 1}]}]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "hours", "desc": True}],
                    "footer": {"show": False, "reducer": ["sum"], "fields": "", "countRows": False}},
        "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
        "targets": refs([target(
            'SELECT sum("foreground_s") / 3600 AS "hours", sum("launches") AS "launches", '
            'sum("foreground_s") / sum("launches") / 60 AS "minutes per launch" '
            f'FROM "app_usage" WHERE {apps()} GROUP BY "app", "category"', fmt="table")]),
    })

    return {
        "uid": "tributary-phone",
        "title": "Phone",
        "description": "A phone's screen time, from the App Usage connector (DigitalStats): per day and per "
                       "group of apps, hands-on time against video, music, maps and the home screen, unlocks, "
                       "when in the day and at night, and every app.",
        "tags": ["tributary", "phone"],
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 41,
        "time": {"from": "now-30d", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "refresh": "",
        "links": [
            {"type": "link", "title": "Sleep", "url": "/d/tributary-sleep", "icon": "dashboard",
             "asDropdown": False, "includeVars": False, "keepTime": True, "targetBlank": False, "tooltip": "",
             "tags": []},
        ],
        "annotations": {"list": []},
        "templating": {"list": variables()},
        "panels": panels,
    }


def variables():
    return [
        {"type": "datasource", "name": "digital", "label": "InfluxDB DigitalStats", "query": "influxdb",
         "regex": "/DigitalStats/i", "hide": 0, "refresh": 1, "current": {}, "options": [], "includeAll": False,
         "multi": False},
        {"type": "textbox", "name": "tz", "label": "Time zone",
         "description": "IANA time zone the days are cut in (TRIBUTARY_TZ).", "query": "Europe/Paris",
         "current": {"text": "Europe/Paris", "value": "Europe/Paris"},
         "options": [{"selected": True, "text": "Europe/Paris", "value": "Europe/Paris"}], "hide": 0},
        {"type": "query", "name": "user", "label": "User", "description": "The person.", "datasource": DIGITAL,
         "definition": 'SHOW TAG VALUES FROM "screen_time" WITH KEY = "user"',
         "query": 'SHOW TAG VALUES FROM "screen_time" WITH KEY = "user"', "refresh": 1, "sort": 1, "hide": 0,
         "current": {}, "options": [], "multi": False, "includeAll": False},
        {"type": "query", "name": "category", "label": "Categories",
         "description": "The categories of apps every panel counts (all by default).", "datasource": DIGITAL,
         "definition": 'SHOW TAG VALUES FROM "app_usage" WITH KEY = "category" WHERE "user" = \'$user\'',
         "query": 'SHOW TAG VALUES FROM "app_usage" WITH KEY = "category" WHERE "user" = \'$user\'',
         "refresh": 2, "sort": 1, "hide": 0, "current": {"text": "All", "value": "$__all"}, "options": [],
         "multi": True, "includeAll": True},
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
