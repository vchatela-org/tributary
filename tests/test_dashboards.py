"""The shipped Grafana dashboards are generic: they work on anyone's series and
carry nobody's. A dashboard exported from a personal Grafana breaks these rules
in predictable ways (a fixed datasource, a saved user, a panel coloured by one
person's project names), so each rule is a test.
"""
import json
import re
from pathlib import Path

import pytest

from app import connectors, schema
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted((ROOT / "grafana" / "dashboards").glob("*.json"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "dashboards.md").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "dashboards-design.md").read_text(encoding="utf-8")


def _written_elsewhere():
    """{measurement: database} of the series the dashboards read that no Tributary
    connector writes yet (garmin-grafana's, the sleep contract's): the table of
    dashboards-design.md's "Series written by other tools"."""
    section = DESIGN.split("\n## Series written by other tools\n", 1)[1].split("\n## ", 1)[0]
    return {m: db for db, m in re.findall(r"^\| `(\w+)` \| `(\w+)` \|", section, re.MULTILINE)}


# Where each measurement lives, as the connectors declare it, or as the design doc
# documents it for another tool.
DECLARED = {m.name: m.database for c in connectors.all_connectors() for m in c.measurements}
DATABASE = {**_written_elsewhere(), **DECLARED}
# The `source` of Tributary's connectors, and of the tools writing next to them
# that a dashboard colours (docs/schema.md: Takeback's Kindle and Audible sessions;
# dashboards-design.md: `garmin` names garmin-grafana's series, `whoop` a strap).
SOURCES = {c.key for c in connectors.all_connectors()} | {"kindle", "audible", "garmin", "whoop"}
# Tag values every user has: the schema's own vocabularies, nobody's data.
VOCABULARY = {"p1", "p2", "p3", "p4", "taken", "missed", "skipped", "unlogged",
              "breakfast", "lunch", "dinner", "snacks", "audiobook", "ebook", "podcast"}
# Built-in Grafana datasources a panel may use besides the dashboard's own.
BUILT_IN = {"-- Grafana --", "-- Mixed --", "__expr__"}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _panels(panels):
    for p in panels:
        yield p
        yield from _panels(p.get("panels", []))


def _variables(d):
    return d.get("templating", {}).get("list", [])


def _datasource_variables(d):
    """{"${name}": database} for each datasource variable (its regex names the database)."""
    out = {}
    for v in _variables(d):
        if v["type"] == "datasource":
            found = re.fullmatch(r"/(\w+)/i?", v.get("regex", ""))
            assert found, f"{d['uid']}: datasource variable ${v['name']} does not pick a database by name"
            out[f"${{{v['name']}}}"] = found.group(1)
    return out


def _queries(d):
    for p in _panels(d["panels"]):
        for t in p.get("targets", []):
            if t.get("query"):
                yield p, t
    for v in _variables(d):
        if v.get("definition"):
            yield None, {"query": v["definition"], "datasource": v.get("datasource")}


@pytest.fixture(params=DASHBOARDS, ids=lambda p: p.stem)
def dashboard(request):
    return _load(request.param)


def test_there_are_dashboards_and_their_uids_are_tributary_s():
    uids = [_load(p)["uid"] for p in DASHBOARDS]
    assert len(uids) >= 5 and len(set(uids)) == len(uids), uids
    assert all(u.startswith("tributary-") for u in uids), uids


def test_every_panel_reads_through_a_datasource_variable(dashboard):
    """No datasource uid of the Grafana it was exported from."""
    allowed = set(_datasource_variables(dashboard)) | BUILT_IN
    for p in _panels(dashboard["panels"]):
        for holder in [p] + p.get("targets", []):
            ds = holder.get("datasource")
            if isinstance(ds, dict):
                assert ds.get("uid") in allowed, (dashboard["uid"], p.get("title"), ds)
            else:
                assert ds is None, (dashboard["uid"], p.get("title"), ds)


def test_a_series_written_elsewhere_is_documented_once():
    """The design doc's table of other tools' series may not contradict a
    connector: once a connector writes one, the database must agree."""
    elsewhere = _written_elsewhere()
    assert {"SleepSummary", "sleep_session", "daily_activity"} <= set(elsewhere), elsewhere
    for name, database in elsewhere.items():
        assert DECLARED.get(name, database) == database, (name, database, DECLARED[name])


def test_queries_read_declared_measurements_from_their_database(dashboard):
    databases = _datasource_variables(dashboard)
    for panel, target in _queries(dashboard):
        uid = (target.get("datasource") or {}).get("uid")
        for name in re.findall(r'FROM\s+"([^"]+)"', target["query"]):
            assert name in DATABASE, (dashboard["uid"], name)
            assert DATABASE[name] == databases.get(uid), (dashboard["uid"], name, uid)


def test_nothing_is_saved_in_the_variables(dashboard):
    """A variable must not ship with a value from someone's data (their user tag,
    a medication...): the lists fill from the reader's own series. The time zone
    is the one value allowed, at TRIBUTARY_TZ's default."""
    for v in _variables(dashboard):
        assert v["type"] in ("datasource", "query", "textbox", "custom"), (dashboard["uid"], v["name"], v["type"])
        current = v.get("current") or {}
        if v["type"] == "custom":
            # a source picker: the dashboard's own lists of sources, plus a source
            # that has no tag to list it by (garmin-grafana's)
            lists = {w["name"] for w in _variables(dashboard) if w["type"] == "query"}
            for item in v["query"].split(","):
                found = re.fullmatch(r"\$\{(\w+):csv\}", item)
                assert (found and found.group(1) in lists) or item in SOURCES, (dashboard["uid"], v["name"], item)
            assert current.get("value") in (None, "", "$__all", ["$__all"]), (dashboard["uid"], v["name"], current)
            assert not v.get("options"), (dashboard["uid"], v["name"])
        elif v["type"] == "query":
            assert current.get("value") in (None, "", "$__all", ["$__all"]), (dashboard["uid"], v["name"], current)
            assert all(o.get("value") in ("$__all",) for o in v.get("options", [])), (dashboard["uid"], v["name"])
        elif v["type"] == "textbox":
            assert v["name"] == "tz", (dashboard["uid"], v["name"])
            assert v["query"] == Settings.model_fields["tz"].default, (dashboard["uid"], v["query"])
        else:
            assert current.get("value") in (None, ""), (dashboard["uid"], v["name"], current)


# docs/dashboards-design.md, rule 3: a number read from one source reads it through
# the picker of its value, under the same name on every dashboard, so a deployment
# pins one default per value. {picker: the fields it may select}, Tributary's names
# and garmin-grafana's.
BODY = set(schema.BODY_COMPOSITION.fields)
PICKERS = {
    "sleep_from": {"asleep_s", "deep_s", "light_s", "rem_s", "awake_s", "awakenings", "bedtime_min", "wake_min",
                   "sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds",
                   "awakeSleepSeconds", "awakeCount"},
    "score_from": {"score", "sleepScore"},
    "hrv_from": {"hrv_rmssd_ms", "avgOvernightHrv"},
    "rhr_from": {"resting_hr", "restingHeartRate"},
    "activity_from": {"steps", "totalSteps", "energy_kcal", "activeKilocalories", "bmrKilocalories",
                      "activeSeconds", "highlyActiveSeconds", "sedentarySeconds", "sleepingSeconds"},
    "workouts_from": {"duration_s", "elapsedDuration", "strain", "energy_kcal", "avg_hr", "max_hr"}
                     | {f"zone{n}_s" for n in range(6)} | {f"zone{n}_pct" for n in range(6)}
                     | {f"hrTimeInZone_{n}" for n in range(6)},
    "weight_from": BODY | {"weight", "bodyFat"},
}
# The tags a single-source query may filter on besides its picker's.
FILTERS = {"source", "user", "kind", "category", "sport"}


def test_a_single_source_reads_the_picker_of_its_value(dashboard):
    single = {v["name"] for v in _variables(dashboard) if v["type"] == "custom" and not v.get("multi")}
    assert single <= set(PICKERS), (dashboard["uid"], single - set(PICKERS))
    for panel, target in _queries(dashboard):
        q = target["query"].replace(GARMIN_NIGHT, "")
        for name in re.findall(r"= '\$\{(\w+_from)\}'", q):
            assert name in single, (dashboard["uid"], panel and panel.get("title"), name)
            named = set(re.findall(r'"(\w+)"', q)) - set(re.findall(r'FROM\s+"(\w+)"', q)) \
                - set(re.findall(r'AS\s+"([^"]+)"', q)) - FILTERS
            assert named <= PICKERS[name], (dashboard["uid"], panel and panel.get("title"), name, named - PICKERS[name])


# docs/dashboards-design.md, "Series written by other tools": a garmin-grafana
# night counts when it has time asleep and REM.
GARMIN_NIGHT = '"sleepTimeSeconds" > 0 AND "remSleepSeconds" > 0'


def test_a_garmin_night_has_time_asleep_and_rem(dashboard):
    for panel, target in _queries(dashboard):
        if re.search(r'FROM\s+"SleepSummary"', target["query"]):
            assert GARMIN_NIGHT in target["query"], (dashboard["uid"], panel and panel.get("title"))


def test_a_garmin_day_is_read_once(dashboard):
    """DailyStats holds one summary per day, and a day can be written twice:
    a per-day value reads it with max(), never a sum."""
    for panel, target in _queries(dashboard):
        q = target["query"]
        for inner in re.findall(r'SELECT ((?:(?!SELECT).)*?)FROM\s+"DailyStats"(?:(?!SELECT).)*?GROUP BY time\(1d\)', q):
            assert "sum(" not in inner, (dashboard["uid"], panel and panel.get("title"), inner)


def test_days_are_cut_in_the_tz_variable(dashboard):
    names = {v["name"] for v in _variables(dashboard)}
    for panel, target in _queries(dashboard):
        for clause in re.findall(r'GROUP BY time\((?:1d|1w)[^)]*\)(?:,\s*"[^"]+")*(?:\s+fill\([^)]*\))?(?:\s+tz\([^)]*\))?',
                                 target["query"]):
            assert "tz(" in clause, (dashboard["uid"], panel and panel.get("title"), clause)
        for zone in re.findall(r"tz\('([^']*)'\)", target["query"]):
            assert zone in ("$tz", "${tz}"), (dashboard["uid"], zone)
            assert "tz" in names, dashboard["uid"]


def test_an_offset_bucket_is_shifted_backwards(dashboard):
    """InfluxDB 1.x drops buckets when a positive GROUP BY time() offset meets
    tz(): time(1d, 12h) tz('Europe/Paris') lost 14 of 569 nights, time(1w, 4d)
    2.5% of the hours asleep. The same boundaries with a negative offset
    (time(1d, -12h), time(1w, -3d)) lose none."""
    for panel, target in _queries(dashboard):
        for offset in re.findall(r"time\(\w+,\s*([^)]*)\)", target["query"]):
            assert offset.strip().startswith("-"), (dashboard["uid"], panel and panel.get("title"), offset)


def _produced_names(panel):
    """What a panel's own data can be called: query aliases and AS names, the
    fields and tags it selects, the names its transformations give."""
    names = {"Time", "Value"} | SOURCES | VOCABULARY
    for t in panel.get("targets", []):
        if t.get("alias"):
            names.add(t["alias"])
        q = t.get("query") or ""
        names.update(re.findall(r'AS "([^"]+)"', q))
        names.update(re.findall(r'"([A-Za-z_][A-Za-z0-9_ %]*)"', q))
    for tr in panel.get("transformations", []):
        options = tr.get("options", {})
        names.update((options.get("renameByName") or {}).values())
        if options.get("alias"):
            names.add(options["alias"])
    return names


def test_every_override_names_something_the_panel_produces(dashboard):
    """An override keyed on any other name came from the data of whoever built the
    dashboard (their project or medication names): it is that data, published."""
    for p in _panels(dashboard["panels"]):
        names = _produced_names(p)
        for o in p.get("fieldConfig", {}).get("overrides", []):
            m = o.get("matcher", {})
            option = m.get("options")
            if m.get("id") == "byName":
                assert option in names, (dashboard["uid"], p.get("title"), option)
            elif m.get("id") == "byRegexp":
                pattern = str(option).strip("/")
                assert any(re.search(pattern, n) for n in names) or any(
                    re.search(pattern, f"{n} · {s}") for n in names for s in SOURCES), (dashboard["uid"], p.get("title"), option)
            else:
                assert m.get("id") in ("byType", "byFrameRefID", "byValue"), (dashboard["uid"], p.get("title"), m)


def _source_colours(panel):
    """[(source, fixed colour)] of the overrides that colour one source's series:
    by its name, or by a name ending in it ("weight · renpho")."""
    out = []
    for o in panel.get("fieldConfig", {}).get("overrides", []):
        m = o.get("matcher", {})
        name = m.get("options") if m.get("id") == "byName" else None
        if m.get("id") == "byRegexp":
            found = re.fullmatch(r"/\.\*(\w+)\$/", str(m.get("options")))
            name = found and found.group(1)
        if name in SOURCES:
            out += [(name, p["value"]["fixedColor"]) for p in o.get("properties", [])
                    if p.get("id") == "color" and p["value"].get("mode") == "fixed"]
    return out


def test_a_source_keeps_one_colour_everywhere_and_shares_it_with_no_other_in_a_panel():
    """docs/dashboards-design.md, rule 2: a scale, a watch, a strap are told apart
    by colour on every dashboard."""
    colours = {}
    for path in DASHBOARDS:
        d = _load(path)
        for p in _panels(d["panels"]):
            in_panel = dict(_source_colours(p))
            for source, colour in in_panel.items():
                colours.setdefault(source, set()).add(colour.lower())
            assert len({c.lower() for c in in_panel.values()}) == len(in_panel), (d["uid"], p.get("title"), in_panel)
    assert colours and all(len(c) == 1 for c in colours.values()), colours


def test_a_series_per_source_is_coloured_by_its_name(dashboard):
    """A source no override names gets its colour from its name, so a source
    appearing does not repaint the others (rule 2)."""
    for p in _panels(dashboard["panels"]):
        if p.get("type") != "timeseries":
            continue
        if any("$tag_source" in (t.get("alias") or "") for t in p.get("targets", [])):
            mode = p["fieldConfig"]["defaults"].get("color", {}).get("mode")
            assert mode == "palette-classic-by-name", (dashboard["uid"], p.get("title"), mode)


def test_a_healthspan_tile_turns_yellow_below_its_guideline_only():
    """docs/dashboards-design.md, rule 5: a tile's colour flags a published
    guideline missed, never a source; met, the tile is in the text colour."""
    d = _load(ROOT / "grafana" / "dashboards" / "healthspan.json")
    guided = {p["title"]: p for p in _panels(d["panels"]) if p.get("type") == "stat"
              and p["fieldConfig"]["defaults"]["color"]["mode"] == "thresholds"}
    assert set(guided) == {"Sleep / night", "Intensity / week", "Steps / day"}
    for title, p in guided.items():
        below, met = p["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert (below["value"], below["color"], met["color"]) == (None, "dark-yellow", "text"), title
        assert f"Yellow below {met['value']:,}" in p["description"], title


def test_links_stay_among_tributary_s_dashboards(dashboard):
    uids = {_load(p)["uid"] for p in DASHBOARDS}
    for link in dashboard.get("links", []):
        found = re.fullmatch(r"/d/([\w-]+)", link.get("url", ""))
        assert found and found.group(1) in uids, (dashboard["uid"], link)


def test_the_readme_and_the_doc_list_every_dashboard():
    section = README.split("## Graphs: in Grafana", 1)[1].split("\n## ", 1)[0]
    for path in DASHBOARDS:
        d = _load(path)
        assert f"](grafana/dashboards/{path.name})" in section, path.name
        assert f"\n## {d['title']}\n" in DOC, d["title"]
    for image in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", DOC):
        assert (ROOT / "docs" / image).is_file(), image
