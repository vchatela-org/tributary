"""Every integration is grouped by category, in the code and in the docs: one
fixed list, and tests that fail when a registered connector is missing from the
README's table, sits in the wrong category, or is described there otherwise than
the code says (what it writes, its default schedule), the way the schema doc test
works for the measurements.
"""
import re
from pathlib import Path

from app import connectors
from app.categories import CATEGORIES, EXTERNAL_TOOLS, hue_for

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CONNECTORS_DOC = ROOT / "docs" / "connectors" / "README.md"


def _sections(text: str, start: str) -> str:
    """The text of a `## <start>` section, up to the next `## `."""
    assert start in text, f"no “{start}” section"
    return text.split(start, 1)[1].split("\n## ", 1)[0]


def _documented(text: str) -> dict:
    """{category: {"connectors": [labels], "tools": [labels]}} from a README's
    `### Category` headings, the bold names of its tables, and its
    "Compatible tools:" lines."""
    out = {}
    category = None
    for line in text.splitlines():
        heading = re.match(r"^### (.+?)\s*$", line)
        if heading:
            category = heading.group(1).strip()
            out[category] = {"connectors": [], "tools": []}
            continue
        if category is None:
            continue
        row = re.match(r"^\|\s*\[?\*\*(.+?)\*\*", line)
        if row:
            out[category]["connectors"].append(row.group(1).strip())
        tools = re.match(r"^Compatible tools: (.*)$", line)
        if tools:
            out[category]["tools"] = [name.strip() for name in re.findall(r"\*\*(.+?)\*\*", tools.group(1))]
    return out


def test_every_connector_names_a_known_category():
    for connector in connectors.all_connectors():
        assert connector.category in CATEGORIES, (connector.key, connector.category)


def test_a_connector_that_reads_an_api_says_when_and_an_upload_one_says_nothing():
    """A source with an API runs on a schedule and carries the cron it defaults
    to; an upload-only source has none, on purpose. Neither is left implicit."""
    from app import schedule
    from app.connectors.base import Connector

    for connector in connectors.all_connectors():
        reads_api = type(connector).api_configured is not Connector.api_configured
        if reads_api:
            assert connector.default_schedule, f"{connector.key} has no default schedule"
            assert schedule.validate(connector.default_schedule) == connector.default_schedule
        else:
            assert not connector.default_schedule, f"{connector.key} is upload-only: no schedule"


def test_every_category_wears_its_own_hue_in_both_themes():
    """One identity hue per category, from the stylesheet's validated slots: a
    category added without one would fall back to grey, and two sharing a slot
    could not be told apart."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    light, dark = css.split('[data-bs-theme="dark"] {', 1)
    dark = dark.split("}", 1)[0]
    hues = [hue_for(name) for name in CATEGORIES]
    assert sorted(hues) == list(range(1, len(CATEGORIES) + 1)), hues
    for slot in hues:
        assert f"--tb-c{slot}:" in light and f"--tb-c{slot}:" in dark, slot
        assert f".tb-hue-{slot} {{ --tb-hue: var(--tb-c{slot}); }}" in css, slot
    assert hue_for("Not a category") == 0


def _cells(line: str) -> list:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _readme_tables(text: str) -> dict:
    """The README's two tables, by the first cell of their header: "Source" (a
    row per connector, `[**Label**](page)` then its category, and a row for a
    category with no connector yet) and "Compatible tool" (its categories,
    comma-separated)."""
    tables, rows = {}, None
    for line in text.splitlines():
        if not line.startswith("|"):
            rows = None
            continue
        cells = _cells(line)
        if rows is None:
            rows = tables.setdefault(cells[0], [])
        elif not set("".join(cells)) <= set("-: "):
            rows.append(cells)
    return tables


def _label(cell: str):
    found = re.match(r"^\[?\*\*(.+?)\*\*", cell)
    return found.group(1).strip() if found else None


def _readme_sources() -> list:
    text = _sections(README.read_text(encoding="utf-8"), "## Supported sources")
    rows = _readme_tables(text).get("Source")
    assert rows, "the README has no table of sources"
    return rows


def test_the_readme_lists_every_connector_under_its_category():
    rows = _readme_sources()
    documented = {_label(row[0]): row[1] for row in rows if _label(row[0])}
    assert documented == {c.label: c.category for c in connectors.all_connectors()}
    # A category without a connector still has its row ("not yet").
    assert {row[1] for row in rows} == set(CATEGORIES)


def test_the_readme_says_what_each_connector_writes_and_when_it_runs():
    """The table's cells come from the code: the measurements and their
    database, how the data arrives, and the default schedule as the UI words it."""
    from app import schedule

    rows = {_label(row[0]): row for row in _readme_sources() if _label(row[0])}
    for connector in connectors.all_connectors():
        _, _, how, writes, when = rows[connector.label]
        names, database = writes.split("→")
        documented = {(db, name) for db in re.findall(r"`([^`]+)`", database)
                      for name in re.findall(r"`([^`]+)`", names)}
        assert documented == {(m.database, m.name) for m in connector.measurements}, connector.key
        if connector.default_schedule:
            assert how.startswith("API"), connector.key
            assert when == schedule.describe(connector.default_schedule), connector.key
        else:
            assert how.startswith("Export"), connector.key
            assert when.startswith("none"), connector.key


def test_the_readme_lists_the_compatible_tools_by_category():
    text = _sections(README.read_text(encoding="utf-8"), "## Supported sources")
    documented = {}
    for row in _readme_tables(text).get("Compatible tool", []):
        for category in row[1].split(","):
            documented.setdefault(category.strip(), []).append(_label(row[0]))
    for name in CATEGORIES:
        expected = sorted(tool.label for tool in EXTERNAL_TOOLS.get(name, ()))
        assert sorted(documented.get(name, [])) == expected, (name, documented.get(name), expected)


def test_the_readme_calls_the_compatible_tools_external():
    """A reader must not take garmin-grafana for something Tributary runs."""
    text = _sections(README.read_text(encoding="utf-8"), "## Supported sources")
    assert ("does not run" in text or "not run by Tributary" in text or "external" in text.lower())
    for tools in EXTERNAL_TOOLS.values():
        for tool in tools:
            assert tool.url.startswith("https://"), tool.label


def test_the_connectors_doc_lists_every_connector_under_its_category():
    text = CONNECTORS_DOC.read_text(encoding="utf-8")
    documented = _documented(_sections(text, "## The connectors"))
    for name in CATEGORIES:
        here = sorted(c.label for c in connectors.all_connectors() if c.category == name)
        labels = sorted(documented.get(name, {"connectors": []})["connectors"])
        assert labels == here, (name, labels, here)


def test_the_readme_shows_the_ui():
    """Screenshots of the interface, kept in the repository: Markdown images, and
    the `<img src>` / `<source srcset>` of a light/dark `<picture>`."""
    text = README.read_text(encoding="utf-8")
    images = (re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
              + re.findall(r'<(?:img|source)\b[^>]*?\b(?:src|srcset)="([^"]+)"', text))
    assert images, "the README shows no screenshot"
    for path in images:
        assert not path.startswith(("http://", "https://")), f"{path} would be fetched from the network"
        assert (ROOT / path).is_file(), f"{path} is missing"
