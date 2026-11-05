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
views are its XY chart. Then the run km by km (footpath_segment): the loop of
any stretch of it (km 3, km 5 to 8) in the same three views, in 3D, and how it
changes along the run. The 3D panels need the Business Charts panel plugin
(volkovlabs-echarts-panel, which bundles ECharts GL); without it they show
"panel plugin not found" and the rest of the dashboard works. Below, the shape
over time, step by step, at the paces picked: the faster the pace, the higher
and longer the loop, so the form is only compared at the same pace.
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
SEGMENT = '"footpath_segment"'
STRETCH = '"km" >= $from_km AND "km_end" <= $to_km'
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


def stretch_view(title, description, which, p):
    """One projection of the stretch's two loops (the median, point by point,
    of its 500 m segments' loops), and the whole run's typical loops dashed."""
    xf, xlabel, yf, ylabel = VIEWS[which]
    targets, overrides = [], []
    for letter, measurement, extra, dashed in (("S", SEGMENT, f" AND {STRETCH}", False), ("W", LOOP, "", True)):
        for j, (side, hex_) in enumerate(FEET):
            ref = f"{letter}{j + 1}"
            targets.append(target(f'SELECT "phase", "{xf}", "{yf}" FROM {measurement} WHERE "run" = \'$run\' AND '
                                  f"\"side\" = '{side}' AND {WHO}{extra}", ref, fmt="table"))
            label = f"{side} foot" + (", whole run" if dashed else ", km $from_km to $to_km")
            override = colour({"id": "byFrameRefID", "options": ref}, hex_, dashed)
            override["properties"].append({"id": "displayName", "value": label})
            overrides.append(override)
    overrides += [
        {"matcher": {"id": "byName", "options": xf}, "properties": [{"id": "custom.axisLabel", "value": xlabel}]},
        {"matcher": {"id": "byName", "options": yf}, "properties": [{"id": "custom.axisLabel", "value": ylabel}]},
    ]
    panel = view(title, description, which, p)
    panel["targets"] = targets
    panel["fieldConfig"]["overrides"] = overrides
    # The segments' loops, point by point: their median; then x and y alone
    # (auto mapping takes the first two numbers).
    panel["transformations"] = [
        {"id": "groupBy", "options": {"fields": {
            "phase": {"aggregations": [], "operation": "groupby"},
            xf: {"aggregations": ["median"], "operation": "aggregate"},
            yf: {"aggregations": ["median"], "operation": "aggregate"}}}},
        {"id": "organize", "options": {"excludeByName": {"phase": True},
                                       "renameByName": {f"{xf} (median)": xf, f"{yf} (median)": yf}}},
    ]
    return panel


# Business Charts (volkovlabs-echarts-panel) code: the body of a function of
# `context` returning the ECharts option. Shared by the two 3D panels.
JS_COMMON = """\
const theme = context.grafana.theme;
const text = theme.colors.text.secondary;
const line = theme.colors.border.medium;
const column = (frame, name) => {
  const f = frame.fields.find((f) => f.name === name);
  return f ? Array.from(f.values) : [];
};
const median = (a) => {
  const s = [...a].sort((p, q) => p - q);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const span = (points, k) => {
  const v = points.map((p) => p[k]);
  return [Math.floor(Math.min(...v)) - 2, Math.ceil(Math.max(...v)) + 2];
};
const axis = (name, range) => ({
  type: 'value', name, min: range[0], max: range[1], splitNumber: 3, nameTextStyle: { color: text },
  axisLabel: { color: text }, axisLine: { lineStyle: { color: line } },
  splitLine: { lineStyle: { color: line, opacity: 0.4 } }, axisPointer: { show: false },
});
"""

JS_STRIDE = JS_COMMON + f"""\
// The stretch's loop per foot: the median, point by point, of its segments' loops.
const feet = {{ L: ['left foot', '{LEFT}'], R: ['right foot', '{RIGHT}'] }};
const series = [];
for (const frame of context.panel.data.series) {{
  const foot = feet[frame.refId];
  if (!foot) continue;
  const phase = column(frame, 'phase'), xs = column(frame, 'x_cm'), ys = column(frame, 'y_cm'),
    zs = column(frame, 'z_cm');
  const by = new Map();
  phase.forEach((p, i) => {{
    if (!by.has(p)) by.set(p, [[], [], []]);
    const b = by.get(p);
    b[0].push(xs[i]); b[1].push(ys[i]); b[2].push(zs[i]);
  }});
  const data = [...by.keys()].sort((a, b) => a - b).map((p) => by.get(p).map(median));
  if (data.length) series.push({{ type: 'line3D', name: foot[0], data, lineStyle: {{ width: 4, color: foot[1] }} }});
}}
const all = series.flatMap((s) => s.data);
if (!all.length) return {{ title: {{ text: 'No loop in this stretch', textStyle: {{ color: text, fontSize: 14 }} }} }};
const x = span(all, 0), y = span(all, 1), z = span(all, 2);
// One scale on the three axes: the box is as long, wide and high as the loops.
const unit = 200 / (x[1] - x[0]);
return {{
  tooltip: {{ formatter: (p) => `${{p.seriesName}}<br>forward ${{p.value[0].toFixed(1)}} cm<br>`
    + `sideways ${{p.value[1].toFixed(1)}} cm<br>up ${{p.value[2].toFixed(1)}} cm` }},
  legend: {{ top: 0, textStyle: {{ color: text }} }},
  xAxis3D: axis('Forward (cm)', x), yAxis3D: axis('Sideways (cm)', y), zAxis3D: axis('Up (cm)', z),
  grid3D: {{
    boxWidth: 200, boxDepth: (y[1] - y[0]) * unit, boxHeight: (z[1] - z[0]) * unit,
    viewControl: {{ alpha: 25, beta: -30, distance: 240 }},
    axisPointer: {{ show: false }},
  }},
  series,
}};
"""

JS_ALONG = JS_COMMON + f"""\
// Each 500 m segment's loop of one foot, seen from the side (forward, up),
// set along the run's km: light at the stretch's start, dark at its end.
const frame = context.panel.data.series.find((f) => f.refId === 'K');
if (!frame || !frame.length) return {{ title: {{ text: 'No loop in this stretch', textStyle: {{ color: text, fontSize: 14 }} }} }};
const km = column(frame, 'km'), phase = column(frame, 'phase'), xs = column(frame, 'x_cm'), zs = column(frame, 'z_cm');
const loops = new Map();
km.forEach((k, i) => {{
  if (!loops.has(k)) loops.set(k, []);
  loops.get(k).push([phase[i], xs[i], k, zs[i]]);
}});
const kms = [...loops.keys()].sort((a, b) => a - b);
const from = [214, 232, 255], to = [{int(LEFT[1:3], 16)}, {int(LEFT[3:5], 16)}, {int(LEFT[5:7], 16)}];
const shade = (f) => `rgb(${{from.map((c, i) => Math.round(c + (to[i] - c) * f)).join(',')}})`;
const series = kms.map((k, i) => ({{
  type: 'line3D', name: `km ${{k}}`,
  data: loops.get(k).sort((a, b) => a[0] - b[0]).map((p) => p.slice(1)),
  lineStyle: {{ width: 3, color: shade(kms.length > 1 ? i / (kms.length - 1) : 1) }},
}}));
const all = series.flatMap((s) => s.data);
const x = span(all, 0), z = span(all, 2);
const unit = 200 / (x[1] - x[0]);
return {{
  tooltip: {{ formatter: (p) => `${{p.seriesName}}<br>forward ${{p.value[0].toFixed(1)}} cm<br>up ${{p.value[2].toFixed(1)}} cm` }},
  xAxis3D: axis('Forward (cm)', x),
  yAxis3D: {{ ...axis('km', [kms[0], kms[kms.length - 1] + 0.5]) }},
  zAxis3D: axis('Up (cm)', z),
  grid3D: {{
    boxWidth: 200, boxDepth: 160, boxHeight: (z[1] - z[0]) * unit,
    viewControl: {{ alpha: 20, beta: 40, distance: 280 }},
    axisPointer: {{ show: false }},
  }},
  series,
}};
"""


def chart3d(title, description, code, targets, p):
    return {
        "type": "volkovlabs-echarts-panel", "title": title, "id": next(_ids), "description": description,
        "datasource": BODY, "gridPos": p,
        "options": {"renderer": "canvas", "map": "none", "editorMode": "code", "getOption": code,
                    "editor": {"format": "auto"}},
        "targets": targets,
    }


def along(title, description, field, label, unit, p, expr=None, decimals=1):
    """A figure of each 500 m segment, per foot, along the run's km."""
    expr = expr or f'"{field}"'
    targets, overrides = [], []
    for j, (side, hex_) in enumerate(FEET):
        ref = f"A{j + 1}"
        targets.append(target(f'SELECT "km", {expr} AS "{field}" FROM {SEGMENT} WHERE "run" = \'$run\' AND '
                              f"\"side\" = '{side}' AND \"phase\" = 0 AND {WHO}", ref, fmt="table"))
        override = colour({"id": "byFrameRefID", "options": ref}, hex_)
        override["properties"].append({"id": "displayName", "value": f"{side} foot"})
        overrides.append(override)
    overrides += [
        {"matcher": {"id": "byName", "options": "km"},
         "properties": [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 0},
                        {"id": "custom.axisLabel", "value": "km"}]},
        {"matcher": {"id": "byName", "options": field}, "properties": [{"id": "custom.axisLabel", "value": label}]},
    ]
    return {
        "type": "xychart", "title": title, "id": next(_ids), "description": description, "datasource": BODY,
        "gridPos": p,
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "color": {"mode": "fixed", "fixedColor": LEFT},
                                     "custom": {"show": "points_and_lines", "lineWidth": 1,
                                                "lineStyle": {"fill": "solid"}, "pointSize": {"fixed": 5},
                                                "pointShape": "circle", "pointStrokeWidth": 0, "fillOpacity": 50,
                                                "axisPlacement": "auto", "axisBorderShow": False}},
                        "overrides": overrides},
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
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "minWidth": 70}},
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

    panels.append(row("The run km by km: km $from_km to $to_km", y)); y += 1
    stretch = ("The stretch of the run picked in *From km* and *To km* (km 3 is From 2 To 3), per foot: the median, "
               "point by point, of the loops of its 500 m segments, each the median of the segment's steps whatever "
               "their pace. Dashed: the whole run's typical loop, at its usual pace.")
    panels.append(stretch_view("The stretch, from the side", stretch + " To the right is forward.", "side",
                               pos(0, y, 12, 11)))
    panels.append(stretch_view("The stretch, from behind", stretch, "back", pos(12, y, 6, 11)))
    panels.append(stretch_view("The stretch, from above", stretch, "top", pos(18, y, 6, 11)))
    y += 11
    form = {"Lift": ("cm", 1), "Back swing": ("cm", 1), "Forward reach": ("cm", 1), "Touchdown": ("cm", 1),
            "Sideways": ("cm", 1), "Pace": ("clocks", 1), "Steps": ("none", 0), "Starts at": ("dthms", 0)}
    panels.append({
        "type": "table", "title": "The stretch's form", "id": next(_ids),
        "description": "Per foot, over the stretch picked: the median of its 500 m segments' lift, back swing, "
                       "forward reach, touchdown and sideways travel (each the median of the segment's steps), "
                       "their pace, the steps they are drawn from, and when the stretch starts, from the "
                       "run's start.",
        "datasource": BODY, "gridPos": pos(0, y, 24, 5),
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "minWidth": 70}},
                        "overrides": [{"matcher": {"id": "byName", "options": k},
                                       "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": d}]}
                                      for k, (u, d) in form.items()]},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "targets": [target(
            'SELECT median("lift_cm") AS "Lift", median("back_cm") AS "Back swing", median("reach_cm") AS '
            '"Forward reach", median("touchdown_cm") AS "Touchdown", median("width_cm") AS "Sideways", '
            '1000 / median("speed_mps") AS "Pace", sum("steps") AS "Steps", min("elapsed_s") AS "Starts at" '
            f'FROM {SEGMENT} WHERE "run" = \'$run\' AND "phase" = 0 AND '
            f'{STRETCH} AND {WHO} GROUP BY "side"', "A", fmt="table")],
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True},
            "indexByName": {k: i for i, k in enumerate(["side", *form])},
            "renameByName": {"side": "Foot"}}}],
    })
    y += 5
    panels.append(chart3d(
        "The stretch in 3D", "The stretch's loops (as above) in 3D, at the same scale on the three axes: drag to "
        "turn it, scroll to zoom. Needs the Business Charts panel plugin (volkovlabs-echarts-panel).", JS_STRIDE,
        [target(f'SELECT "phase", "x_cm", "y_cm", "z_cm" FROM {SEGMENT} WHERE "run" = \'$run\' AND "side" = '
                f"'{side}' AND {WHO} AND {STRETCH}", ref, fmt="table") for ref, side in (("L", "left"), ("R", "right"))],
        pos(0, y, 12, 14)))
    panels.append(chart3d(
        "Along the run, in 3D ($foot foot)", "The loop of each 500 m segment of the stretch, for the foot picked in "
        "*Foot*, seen from the side (forward and up) and set along the run's km: light at the stretch's start, dark "
        "at its end. A loop that sinks, shortens or reaches further as the km go by shows the stride changing "
        "(fatigue, a hill, a rep). Drag to turn it, scroll to zoom. Needs the Business Charts panel plugin "
        "(volkovlabs-echarts-panel).", JS_ALONG,
        [target(f'SELECT "km", "phase", "x_cm", "z_cm" FROM {SEGMENT} WHERE "run" = \'$run\' AND "side" = '
                f"'$foot' AND {WHO} AND {STRETCH}", "K", fmt="table")],
        pos(12, y, 12, 14)))
    y += 14
    seg_note = (" One point per 500 m of the run and foot (the median of the segment's steps, whatever their pace), "
                "at the km it starts.")
    panels.append(along("Foot lift along the run", "How high the foot rises in the swing." + seg_note, "lift_cm",
                        "Lift", "suffix: cm", pos(0, y, 8, 8)))
    panels.append(along("Back swing along the run", "How far behind the foot travels after push-off." + seg_note,
                        "back_cm", "Back swing", "suffix: cm", pos(8, y, 8, 8)))
    panels.append(along("Forward reach along the run", "How far ahead the foot swings before it comes down."
                        + seg_note, "reach_cm", "Forward reach", "suffix: cm", pos(16, y, 8, 8)))
    y += 8
    panels.append(along("Touchdown along the run", "Where the foot meets the ground, ahead of mid-stance (the "
                        "overstride)." + seg_note, "touchdown_cm", "Touchdown", "suffix: cm", pos(0, y, 8, 8)))
    panels.append(along("Sideways travel along the run", "The foot's sideways range through the stride." + seg_note,
                        "width_cm", "Sideways", "suffix: cm", pos(8, y, 8, 8)))
    panels.append(along("Pace along the run", "The pace of the segment's steps: the loop grows with speed, so read "
                        "the figures beside it at the same pace." + seg_note, "pace", "Pace (per km)", "clocks", pos(16, y, 8, 8),
                        expr='1000 / "speed_mps"'))
    y += 8

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
                       "footpath_loop, footpath_segment, footpath_step): a run's typical stride seen from the side, "
                       "from behind and from above, compared with another run; any stretch of the run, km by km, in "
                       "2D and 3D; and the shape of the stride over time at the same pace.",
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
        # Numerical, ascending (3) and descending (4): the whole run by default.
        query_var("from_km", "From km", "Where the stretch drawn km by km starts (km 3 is From 2 To 3).",
                  f'SELECT DISTINCT("km") FROM {SEGMENT} WHERE "run" = \'$run\' AND {WHO}', sort=3),
        query_var("to_km", "To km", "Where the stretch ends (the last choice is the run's end).",
                  f'SELECT DISTINCT("km_end") FROM {SEGMENT} WHERE "run" = \'$run\' AND {WHO}', sort=4),
        query_var("foot", "Foot", "The foot drawn along the run in 3D.",
                  f'SHOW TAG VALUES FROM {SEGMENT} WITH KEY = "side" WHERE {WHO}'),
        query_var("pace", "Pace", "The paces (per km, 15 s bands: 4:30 is 4:30 to 4:44) whose steps the graphs over "
                  "time read.", f'SHOW TAG VALUES FROM {STEPS} WITH KEY = "pace" WHERE {WHO}', multi=True),
    ]


def main():
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
