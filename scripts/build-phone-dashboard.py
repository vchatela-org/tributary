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

The *Filters* variable (Grafana's ad hoc filters) leaves apps out of every
panel: a medication reminder that keeps the screen on, the home screen. Grafana
puts each filter beside a query's $timeFilter, so a query that reads apps
through a subquery keeps their tags there (GROUP BY *).

A value "per day" is a total over the time range divided by the days it holds
data (screen_time has one point per day; the hidden variable $days): a range
that starts before the first backup does not dilute it. Not with Grafana's
expressions: a query feeding one never gets the ad hoc filters.

The week heatmaps (weekday by hour) are tables with coloured cells: Grafana's
heatmap panel wants time along its x axis, and InfluxQL groups by a tag, never
by a field (local_hour and local_weekday are fields). So one query per weekday,
its 24 hours each a subquery of the same measurement (subqueries of two
measurements stay two series), divided by that weekday's days with data
(hidden variables $days_1..$days_7, Monday to Sunday).
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
# The days of the range that hold data, and the unlocks over it: hidden
# variables, read again at each change of the time range. A value "per day" is
# divided by them in its own query, not in a Grafana expression: Grafana
# leaves a query feeding an expression out of the ad hoc filters. `0$days`:
# with no data (an empty variable) the query divides by 0, which InfluxDB
# answers with 0, rather than failing on "/ " (041 is read as 41).
DAYS = "0$days"
UNLOCKS = "0$unlocks"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # local_weekday 1..7 (ISO)
# Session lengths (app_session: an app's time in the foreground, from opening it
# to leaving it): the quick look, the reply, the real use, the long scroll.
LENGTHS = (
    ("under 10 s", '"duration_s" < 10'),
    ("10 s to 1 min", '"duration_s" >= 10 AND "duration_s" < 60'),
    ("1 to 5 min", '"duration_s" >= 60 AND "duration_s" < 300'),
    ("5 to 15 min", '"duration_s" >= 300 AND "duration_s" < 900'),
    ("15 min and more", '"duration_s" >= 900'),
)
# The heatmap's cells: one hue, light to dark (the reference palette's
# sequential blue), absolute steps so a colour means the same on every week.
BLUES = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
# Hours that stay hours: Grafana's `h` turns 30 hours into "1.3 days".
HOURS = "suffix: h"

_ids = count(1)


def among(categories):
    return '"category" =~ /^(' + "|".join(categories) + ')$/'


def apps(*conditions):
    """The WHERE of app_usage / app_session: the user, the picked categories."""
    return " AND ".join((USER, PICKED) + conditions + ("$timeFilter",))


def target(query, alias=None, fmt="time_series", ref=None, hide=False):
    t = {"refId": ref, "datasource": DIGITAL, "rawQuery": True, "resultFormat": fmt, "query": query}
    if alias:
        t["alias"] = alias
    if hide:
        t["hide"] = True
    return t


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
    """A total over the range divided by the days that have data ($days)."""
    return [target(f'SELECT {select} / {DAYS} FROM "{measurement}" WHERE {where}')]


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


def binned(title, description, bins, value, unit, hex_, p, decimals=0):
    """One bar per bin (an hour of the day, a session length), its total over the
    range divided by the days with data. Each bin's sum also reads the user's
    sessions times 0 from the same measurement, which InfluxDB merges into the
    same series: an empty bin is a 0, not a missing bar (fill() only fills a
    series that exists). Both subqueries keep every tag (GROUP BY *): Grafana's
    ad hoc filters ($filters) land beside the outer $timeFilter, and must find
    `app` or `category` there."""
    targets = []
    for label, condition in bins:
        targets.append(target(f'SELECT sum("v") / {DAYS} AS "{label}" FROM (SELECT {value} AS "v" '
                              f'FROM "app_session" WHERE {USER} AND {PICKED} AND {condition} GROUP BY *), '
                              f'(SELECT "count" * 0 AS "v" FROM "app_session" WHERE {USER} GROUP BY *) '
                              f'WHERE $timeFilter', alias=label))
    return {"type": "barchart", "title": title, "id": next(_ids), "description": description,
            "datasource": DIGITAL, "gridPos": p,
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "decimals": decimals,
                                         "color": {"mode": "fixed", "fixedColor": hex_},
                                         "custom": {"fillOpacity": 90, "lineWidth": 0, "axisBorderShow": False}},
                            "overrides": []},
            "options": {"orientation": "vertical", "xField": "Field", "showValue": "never", "barWidth": 0.8,
                        "groupWidth": 0.7, "legend": {"showLegend": False, "displayMode": "list",
                                                      "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}, "xTickLabelSpacing": 0,
                        "stacking": "none"},
            "transformations": [{"id": "reduce", "options": {"mode": "seriesToRows", "reducers": ["lastNotNull"],
                                                             "includeTimeField": False}}],
            "targets": refs(targets)}


def by_hour(title, description, categories, hex_, p):
    """Minutes per day in each hour of the day (local time)."""
    return binned(title, description,
                  [(f"{h:02d}h", among(categories) + f' AND "local_hour" = {h}') for h in range(24)],
                  '"duration_s" / 60', "m", hex_, p)


def by_length(title, description, value, unit, p, decimals=0):
    """Hands-on sessions per day, binned by how long each lasted."""
    return binned(title, description, [(label, among(HANDS_ON) + " AND " + cond) for label, cond in LENGTHS],
                  value, unit, NEUTRAL, p, decimals=decimals)


def week_by_hour(title, description, measurement, value, conditions, steps, total, p):
    """A heatmap of the week: a row per weekday, a cell per hour (local time),
    each the hour's total over the range divided by that weekday's days with data
    ($days_N), then the row's total per day. The cells are coloured on absolute
    steps (`steps`: the lower bound of each blue); an hour never used stays
    empty. Every subquery reads `measurement`, keeping its tags (GROUP BY *) for
    the ad hoc filters. `value` must be a float: over integers (unlock's
    "count"), InfluxDB 1.11 keeps only the first of several subqueries naming
    different fields, and every hour stays empty."""
    where = " AND ".join((USER,) + tuple(conditions))
    targets = []
    for n, _day in enumerate(WEEKDAYS, start=1):
        div = f"0$days_{n}"
        subs = [f'(SELECT "local_weekday" AS "wd", {value} AS "all" FROM "{measurement}" WHERE {where} '
                f'AND "local_weekday" = {n} GROUP BY *)']
        select = ['max("wd") AS "day"']
        for h in range(24):
            subs.append(f'(SELECT {value} AS "h{h:02d}" FROM "{measurement}" WHERE {where} AND "local_weekday" = {n} '
                        f'AND "local_hour" = {h} GROUP BY *)')
            select.append(f'sum("h{h:02d}") / {div} AS "{h:02d}"')
        select.append(f'sum("all") / {div}{total[0]} AS "{total[1]}"')
        targets.append(target(f'SELECT {", ".join(select)} FROM {", ".join(subs)} WHERE $timeFilter', fmt="table"))
    cells = {"type": "color-background", "mode": "basic"}
    scale = [{"color": "transparent", "value": None}] + [
        {"color": hex_, "value": v} for hex_, v in zip(BLUES, steps)]
    return {
        "type": "table", "title": title, "id": next(_ids), "description": description, "datasource": DIGITAL,
        "gridPos": p,
        "fieldConfig": {
            # Under 0.5 a cell would read "0" in a coloured cell: "·", some use.
            "defaults": {"unit": "none", "decimals": 0, "noValue": "",
                         "mappings": [{"type": "range", "options": {"from": 0, "to": 0.5, "result": {"text": "·"}}}],
                         "color": {"mode": "thresholds"},
                         "thresholds": {"mode": "absolute", "steps": scale},
                         "custom": {"align": "center", "cellOptions": cells, "minWidth": 34,
                                    "inspect": False}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "day"},
                 "properties": [{"id": "displayName", "value": " "},
                                {"id": "custom.cellOptions", "value": {"type": "auto"}},
                                {"id": "custom.align", "value": "left"}, {"id": "custom.width", "value": 52},
                                {"id": "mappings", "value": [{"type": "value", "options": {
                                    str(n): {"text": day, "index": n - 1}
                                    for n, day in enumerate(WEEKDAYS, start=1)}}]}]},
                {"matcher": {"id": "byName", "options": total[1]},
                 "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}, {"id": "mappings", "value": []},
                                {"id": "unit", "value": total[2]}, {"id": "decimals", "value": total[3]},
                                {"id": "custom.align", "value": "right"}, {"id": "custom.width", "value": 80}]},
            ]},
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": "", "countRows": False}},
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "sortBy", "options": {"sort": [{"field": "day", "desc": False}]}},
            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
        ],
        "targets": refs(targets),
    }


def with_average(name, field, scale, hex_=GROUPS[0][2]):
    """A day's bar and, over it, the 7-day moving average as a line (days with
    data only: fill(none)), in the first group's blue."""
    return [target(f'SELECT sum("{field}"){scale} AS "{name}" FROM "screen_time" WHERE {USER} AND $timeFilter {DAY}',
                   alias=name),
            target(f'SELECT moving_average(sum("{field}"), 7){scale} AS "7-day average" FROM "screen_time" '
                   f'WHERE {USER} AND $timeFilter {DAY}', alias="7-day average")]


AVERAGE_LINE = {"matcher": {"id": "byName", "options": "7-day average"},
                "properties": [{"id": "custom.drawStyle", "value": "line"}, {"id": "custom.lineWidth", "value": 2},
                               {"id": "custom.fillOpacity", "value": 0},
                               {"id": "color", "value": {"mode": "fixed", "fixedColor": GROUPS[0][2]}}]}


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
             [target(f'SELECT sum("foreground_s") / {UNLOCKS} FROM "app_usage" WHERE {apps(hands_on)}')], "s",
             pos(16, y, 4, 4)),
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
        "Screen on per day", "How long the screen was on, per day (the phone's own figure), and its 7-day moving "
        "average: the trend under the day-to-day noise.",
        with_average("Screen on", "screen_on_s", " / 3600"), HOURS, pos(0, y, 12, 7), overrides=[AVERAGE_LINE],
        stack=False, decimals=1))
    panels.append(bars(
        "Unlocks per day", "Times the phone was unlocked, per day, and the 7-day moving average.",
        with_average("Unlocks", "unlocks", ""), "none", pos(12, y, 12, 7), overrides=[AVERAGE_LINE], stack=False,
        decimals=0))
    y += 7

    panels.append(row("When in the day", y))
    y += 1
    panels.append(week_by_hour(
        "Hands-on, by weekday and hour",
        "Minutes per day in the hands-on groups' apps, for each weekday (row) and hour (column, local time), over "
        "the time range: each cell is that hour's total divided by the number of such weekdays with data. The "
        "darker, the longer; an empty cell is an hour never used. On the right, the day's total. A session counts "
        "in the hour it started.",
        "app_session", '"duration_s" / 60', (PICKED, hands_on), (0.01, 2, 5, 10, 15, 20, 30),
        (" / 60", "per day", HOURS, 1), pos(0, y, 24, 9)))
    y += 9
    panels.append(week_by_hour(
        "Unlocks, by weekday and hour",
        "Unlocks per day for each weekday (row) and hour (column, local time), over the time range: when the "
        "phone gets picked up. On the right, the day's total.",
        "unlock", '"count" * 1.0', (), (0.01, 1, 2, 3, 4, 6, 8), ("", "per day", "none", 0), pos(0, y, 24, 9)))
    y += 9
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

    panels.append(row("How long each time", y))
    y += 1
    panels.append(by_length(
        "Hands-on sessions per day, by length",
        "Sessions per day in the hands-on groups' apps, by how long each lasted: a session is an app in the "
        "foreground, from opening it to leaving it. Many short ones: the phone checked, again and again.",
        '"count"', "none", pos(0, y, 12, 8)))
    panels.append(by_length(
        "Hands-on minutes per day, by session length",
        "Minutes per day in the hands-on groups' apps, by how long the session lasted: where the time goes, in "
        "quick looks or in long stretches.",
        '"duration_s" / 60', "suffix: min", pos(12, y, 12, 8)))
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
        {"type": "query", "name": "days", "label": "Days with data", "datasource": DIGITAL,
         "description": "The days of the time range that hold data: what a value per day is divided by.",
         "definition": f'SELECT count("screen_on_s") FROM "screen_time" WHERE {USER} AND $timeFilter',
         "query": f'SELECT count("screen_on_s") FROM "screen_time" WHERE {USER} AND $timeFilter',
         "refresh": 2, "sort": 0, "hide": 2, "current": {}, "options": [], "multi": False, "includeAll": False},
        {"type": "query", "name": "unlocks", "label": "Unlocks", "datasource": DIGITAL,
         "description": "The unlocks over the time range: what hands-on time per unlock is divided by.",
         "definition": f'SELECT sum("unlocks") FROM "screen_time" WHERE {USER} AND $timeFilter',
         "query": f'SELECT sum("unlocks") FROM "screen_time" WHERE {USER} AND $timeFilter',
         "refresh": 2, "sort": 0, "hide": 2, "current": {}, "options": [], "multi": False, "includeAll": False},
    ] + [
        # Each weekday's days with data: what a cell of the week heatmaps is
        # divided by. A day with data is a day with an app session (any app: an
        # ad hoc filter never reaches a variable).
        {"type": "query", "name": f"days_{n}", "label": f"{day} with data", "datasource": DIGITAL,
         "description": f"The {day} of the time range that hold data: what a {day} cell is divided by.",
         "definition": (q := f'SELECT count("d") FROM (SELECT count("count") AS "d" FROM "app_session" WHERE {USER} '
                             f'AND "local_weekday" = {n} AND $timeFilter GROUP BY time(1d) fill(none) tz(\'$tz\'))'),
         "query": q, "refresh": 2, "sort": 0, "hide": 2, "current": {}, "options": [], "multi": False,
         "includeAll": False}
        for n, day in enumerate(WEEKDAYS, start=1)
    ] + [
        # Grafana adds each filter to every query, beside its $timeFilter: `app !=
        # <an app>` leaves an app out of every panel (a reminder that keeps the
        # screen on, the home screen...), `app = <an app>` looks at one. Empty:
        # a deployment saves its own in its copy of the dashboard.
        {"type": "adhoc", "name": "filters", "label": "Filters",
         "description": "Leave apps out (app != ...), or look at one (app = ...); category and package work too. "
                        "Screen on and unlocks are the phone's own figures: an app filter leaves them as they are, "
                        "and `app = ...` empties them.",
         "datasource": DIGITAL, "filters": [], "baseFilters": [], "hide": 0},
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
