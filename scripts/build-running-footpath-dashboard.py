#!/usr/bin/env python3
"""Writes grafana/dashboards/running-footpath.json: the Footpath of a Stryd Duo
(the path of each foot through a stride), as the Stryd connector writes it in
BodyStats (footpath_loop, footpath_step).

Every query is written here: edit this file, run
`python3 scripts/build-running-footpath-dashboard.py`, commit both files
(tests/test_dashboards.py checks they agree).

A run's loop is drawn three times, as the foot's path seen from the side (forward
and up), from behind (sideways and up) and from above (forward and sideways), with
a second run to compare it with. Grafana has no 3D panel of its own: the three
views are its XY chart. Below, the shape over time, step by step, at the paces
picked: the faster the pace, the higher and longer the loop, so the form is only
compared at the same pace.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "running-footpath.json"

BODY = {"type": "influxdb", "uid": "${body}"}
# The two feet (not sources: rule 2 of docs/dashboards-design.md colours sources,
# and no source is drawn here). A pair from the validated palette.
LEFT, RIGHT = "#3987e5", "#d95926"
FEET = (("left", LEFT), ("right", RIGHT))

LOOP = '"footpath_loop"'
STEPS = '"footpath_step"'
WHO = "\"user\" = '$user'"
AT_PACE = "\"pace\" =~ /^(${pace:regex})$/"
DAY = "GROUP BY time(1d), \"side\" fill(none) tz('$tz')"

_ids = iter(range(1, 1000))


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def target(query, ref, alias=None, fmt="time_series"):
    t = {"refId": ref, "datasource": BODY, "rawQuery": True, "resultFormat": fmt, "query": query}
    if alias:
        t["alias"] = alias
    return t


def row(title, y):
    return {"type": "row", "title": title, "id": next(_ids), "collapsed": False, "gridPos": pos(0, y, 24, 1),
            "panels": []}


def colour(matcher, hex_, dashed=False):
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": hex_}}]
    if dashed:
        props.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}})
    return {"matcher": matcher, "properties": props}


# The loop's fields, per view: (x field, its label, y field, its label).
VIEWS = {
    "side": ("x_cm", "Forward (cm)", "z_cm", "Up (cm)"),
    "back": ("y_cm", "Sideways (cm)", "z_cm", "Up (cm)"),
    "top": ("x_cm", "Forward (cm)", "y_cm", "Sideways (cm)"),
}


def view(title, description, which, p):
    """One projection of the run's two loops and the compared run's two."""
    xf, xlabel, yf, ylabel = VIEWS[which]
    runs = (("A", "$run", False), ("B", "$compare", True))
    targets, overrides = [], []
    for i, (letter, var, dashed) in enumerate(runs):
        for j, (side, hex_) in enumerate(FEET):
            ref = f"{letter}{j + 1}"
            targets.append(target(f'SELECT "{xf}", "{yf}" FROM {LOOP} WHERE "run" = \'{var}\' AND "side" = '
                                  f"'{side}' AND {WHO}", ref, fmt="table"))
            label = f"{side} foot" + (", compared run" if dashed else "")
            override = colour({"id": "byFrameRefID", "options": ref}, hex_, dashed)
            override["properties"].append({"id": "displayName", "value": label})
            overrides.append(override)
    overrides += [
        {"matcher": {"id": "byName", "options": xf}, "properties": [{"id": "custom.axisLabel", "value": xlabel}]},
        {"matcher": {"id": "byName", "options": yf}, "properties": [{"id": "custom.axisLabel", "value": ylabel}]},
    ]
    return {
        "type": "xychart", "title": title, "id": next(_ids), "description": description, "datasource": BODY,
        "gridPos": p,
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": LEFT},
                                     "custom": {"show": "lines", "lineWidth": 2, "lineStyle": {"fill": "solid"},
                                                "pointSize": {"fixed": 1}, "pointShape": "circle",
                                                "pointStrokeWidth": 0, "fillOpacity": 0,
                                                "axisPlacement": "auto", "axisBorderShow": False}},
                        "overrides": overrides},
        # Auto: each query's frame is a series, its first number field the x, the
        # next one the y. (Manual mapping picks frames by index only, and a run
        # without one foot's loop has a frame less.)
        "options": {"mapping": "auto", "series": [{}],
                    "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "single", "sort": "none"}},
        "targets": targets,
    }


def trend(title, description, expr, unit, p, decimals=1):
    """A form figure per run (the median of its steps at the paces picked), per foot."""
    return {
        "type": "timeseries", "title": title, "id": next(_ids), "description": description, "datasource": BODY,
        "gridPos": p,
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "color": {"mode": "fixed", "fixedColor": LEFT},
                                     "custom": {"drawStyle": "points", "pointSize": 6, "showPoints": "always",
                                                "lineWidth": 1, "fillOpacity": 0, "spanNulls": True,
                                                "axisBorderShow": False}},
                        "overrides": [colour({"id": "byName", "options": side}, hex_) for side, hex_ in FEET]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "targets": [target(f'SELECT {expr} FROM {STEPS} WHERE {WHO} AND {AT_PACE} AND $timeFilter {DAY}', "A",
                           alias="$tag_side")],
    }


SIDE_DESC = ("The run's typical stride, seen from the side: each foot's path from mid-stance to the next mid-stance, "
             "with the run's forward motion taken out, so the loop shows where the foot travels relative to the body. "
             "To the right is forward. The foot pushes off and travels back (left of 0), lifts, swings forward, "
             "reaches ahead and comes down. It is the median, point by point, of the run's steps at its usual pace "
             "(within 5% of its median speed). Dashed: the run to compare with.")


def build():
    global _ids
    _ids = iter(range(1, 1000))
    panels = []
    y = 0

    panels.append(row("Footpath of a run", y)); y += 1
    panels.append(view("Seen from the side", SIDE_DESC, "side", pos(0, y, 12, 11)))
    panels.append(view("Seen from behind", "The same loops seen from behind: sideways and up. Stryd reports the left "
                       "foot's sideways travel as negative. Dashed: the run to compare with.", "back",
                       pos(12, y, 6, 11)))
    panels.append(view("Seen from above", "The same loops seen from above: forward (to the right) and sideways. "
                       "Dashed: the run to compare with.", "top", pos(18, y, 6, 11)))
    y += 11
    fields = {"Lift": ("cm", 1), "Back swing": ("cm", 1), "Forward reach": ("cm", 1), "Sideways": ("cm", 1),
              "Pace": ("clocks", 1), "Steps": ("none", 0)}
    panels.append({
        "type": "table", "title": "The two runs' loops", "id": next(_ids),
        "description": "Read on the loops above, per foot: Lift is the loop's highest point; Back swing how far "
                       "behind the foot travels after push-off, Forward reach how far ahead it swings (both from "
                       "mid-stance); Sideways its sideways range; Pace and Steps, the pace and the number of steps "
                       "the loop is the median of.",
        "datasource": BODY, "gridPos": pos(0, y, 24, 7),
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": k},
                                       "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": d}]}
                                      for k, (u, d) in fields.items()]},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "targets": [target(
            'SELECT max("z_cm") AS "Lift", min("x_cm") * -1 AS "Back swing", max("x_cm") AS "Forward reach", '
            'spread("y_cm") AS "Sideways", 1000 / last("speed_mps") AS "Pace", last("steps") AS "Steps" '
            f"FROM {LOOP} WHERE (\"run\" = '$run' OR \"run\" = '$compare') AND {WHO} GROUP BY \"run\", \"side\"",
            "A", fmt="table")],
        "transformations": [{"id": "merge", "options": {}}, {"id": "organize", "options": {
            "excludeByName": {"Time": True},
            "indexByName": {k: i for i, k in enumerate(["run", "side", *fields])},
            "renameByName": {"run": "Run", "side": "Foot"}}}],
    })
    y += 7

    panels.append(row("Form over time, at the same pace", y)); y += 1
    pace_note = (" One point per run and foot: the median of its steps at the paces picked in *Pace*. Compare runs "
                 "at the same pace: the loop grows with speed.")
    panels.append(trend("Foot lift", "How high the foot rises in the swing (the loop's highest point)." + pace_note,
                        'median("lift_cm")', "suffix: cm", pos(0, y, 12, 8)))
    panels.append(trend("Back swing", "How far behind the foot travels after push-off, from mid-stance." + pace_note,
                        'median("back_cm")', "suffix: cm", pos(12, y, 12, 8)))
    y += 8
    panels.append(trend("Forward reach", "How far ahead the foot swings before it comes down, from mid-stance."
                        + pace_note, 'median("reach_cm")', "suffix: cm", pos(0, y, 12, 8)))
    panels.append(trend("Touchdown", "Where the foot meets the ground, ahead of mid-stance: the further ahead, the "
                        "more the foot lands in front of the body (overstriding)." + pace_note,
                        'median("touchdown_cm")', "suffix: cm", pos(12, y, 12, 8)))
    y += 8
    panels.append(trend("Sideways travel", "The foot's sideways range through the stride." + pace_note,
                        'median("width_cm")', "suffix: cm", pos(0, y, 12, 8)))
    panels.append(trend("Pace of the steps compared", "The median pace of the steps the graphs above read, per run: "
                        "check that the runs compared ran at the same pace. One point per run and foot.",
                        '1000 / median("speed_mps")', "clocks", pos(12, y, 12, 8)))
    y += 8

    return {
        "uid": "tributary-running-footpath",
        "title": "Running footpath",
        "description": "The path of each foot through a stride, from a Stryd Duo's Footpath (the Stryd connector: "
                       "footpath_loop, footpath_step): a run's typical stride seen from the side, from behind and "
                       "from above, compared with another run, and the shape of the stride over time at the same pace.",
        "tags": ["tributary", "running"],
        "editable": True,
        "graphTooltip": 0,
        "schemaVersion": 41,
        "time": {"from": "now-2y", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "refresh": "",
        "links": [
            {"type": "link", "title": "Running", "url": "/d/tributary-running", "icon": "dashboard",
             "asDropdown": False, "includeVars": False, "keepTime": True, "targetBlank": False, "tooltip": "",
             "tags": []},
        ],
        "annotations": {"list": []},
        "templating": {"list": variables()},
        "panels": panels,
    }


def query_var(name, label, description, query, sort=1, multi=False):
    v = {"type": "query", "name": name, "label": label, "description": description, "datasource": BODY,
         "definition": query, "query": query, "refresh": 1, "sort": sort, "hide": 0, "current": {}, "options": [],
         "multi": multi, "includeAll": multi}
    if multi:
        v["current"] = {"text": "All", "value": "$__all"}
    return v


def variables():
    runs = f'SHOW TAG VALUES FROM {LOOP} WITH KEY = "run" WHERE {WHO}'
    return [
        {"type": "datasource", "name": "body", "label": "InfluxDB BodyStats", "query": "influxdb",
         "regex": "/BodyStats/i", "hide": 0, "refresh": 1, "current": {}, "options": [], "includeAll": False,
         "multi": False},
        {"type": "textbox", "name": "tz", "label": "Time zone",
         "description": "IANA time zone the days are cut in (TRIBUTARY_TZ).", "query": "Europe/Paris",
         "current": {"text": "Europe/Paris", "value": "Europe/Paris"},
         "options": [{"selected": True, "text": "Europe/Paris", "value": "Europe/Paris"}], "hide": 0},
        query_var("user", "User", "The person, in BodyStats.", f'SHOW TAG VALUES FROM {STEPS} WITH KEY = "user"'),
        # Latest first (sort 2: alphabetical, descending; the names are local start times).
        query_var("run", "Run", "The run whose loops are drawn, by its local start time.", runs, sort=2),
        query_var("compare", "Compare with", "A second run, drawn dashed.", runs, sort=2),
        query_var("pace", "Pace", "The paces (per km, 15 s bands: 4:30 is 4:30 to 4:44) whose steps the graphs over "
                  "time read.", f'SHOW TAG VALUES FROM {STEPS} WITH KEY = "pace" WHERE {WHO}', multi=True),
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
