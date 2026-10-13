"""The shipped Grafana dashboards are generic: they work on anyone's series and
carry nobody's. A dashboard exported from a personal Grafana breaks these rules
in predictable ways (a fixed datasource, a saved user, a panel coloured by one
person's project names), so each rule is a test.
"""
import json
import re
from pathlib import Path

import pytest

from app import connectors
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted((ROOT / "grafana" / "dashboards").glob("*.json"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "dashboards.md").read_text(encoding="utf-8")

# Where each measurement lives, as the connectors declare it.
DATABASE = {m.name: m.database for c in connectors.all_connectors() for m in c.measurements}
# The `source` of Tributary's connectors, and of the tools writing next to them
# that a dashboard colours (docs/schema.md: Takeback's Kindle and Audible sessions).
SOURCES = {c.key for c in connectors.all_connectors()} | {"kindle", "audible"}
# Tag values every user has: the schema's own vocabularies, nobody's data.
VOCABULARY = {"p1", "p2", "p3", "p4", "taken", "missed", "skipped", "unlogged",
              "breakfast", "lunch", "dinner", "snacks", "audiobook", "ebook", "podcast"}
# Built-in Grafana datasources a panel may use besides the dashboard's own.
BUILT_IN = {"-- Grafana --", "__expr__"}


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
        assert v["type"] in ("datasource", "query", "textbox"), (dashboard["uid"], v["name"], v["type"])
        current = v.get("current") or {}
        if v["type"] == "query":
            assert current.get("value") in (None, "", "$__all", ["$__all"]), (dashboard["uid"], v["name"], current)
            assert all(o.get("value") in ("$__all",) for o in v.get("options", [])), (dashboard["uid"], v["name"])
        elif v["type"] == "textbox":
            assert v["name"] == "tz", (dashboard["uid"], v["name"])
            assert v["query"] == Settings.model_fields["tz"].default, (dashboard["uid"], v["query"])
        else:
            assert current.get("value") in (None, ""), (dashboard["uid"], v["name"], current)


def test_days_are_cut_in_the_tz_variable(dashboard):
    names = {v["name"] for v in _variables(dashboard)}
    for _, target in _queries(dashboard):
        for zone in re.findall(r"tz\('([^']*)'\)", target["query"]):
            assert zone in ("$tz", "${tz}"), (dashboard["uid"], zone)
            assert "tz" in names, dashboard["uid"]


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
