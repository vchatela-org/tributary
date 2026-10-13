"""Every integration is grouped by category, in the code and in the docs: one
fixed list, and a test that fails when a registered connector is missing from the
README's table or sits in the wrong category (the way the schema doc test works
for the measurements).
"""
import re
from pathlib import Path

from app import connectors
from app.categories import CATEGORIES, EXTERNAL_TOOLS

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


def test_the_readme_groups_every_integration_by_category():
    documented = _documented(_sections(README.read_text(encoding="utf-8"), "## Integrations"))
    for name in CATEGORIES:
        assert name in documented, f"the README has no “{name}” section"
        labels, tools = documented[name]["connectors"], documented[name]["tools"]
        here = sorted(c.label for c in connectors.all_connectors() if c.category == name)
        assert sorted(labels) == here, (name, sorted(labels), here)
        expected_tools = sorted(tool.label for tool in EXTERNAL_TOOLS.get(name, ()))
        assert sorted(tools) == expected_tools, (name, sorted(tools), expected_tools)


def test_no_connector_is_documented_outside_its_category():
    """A connector in the wrong section is as bad as a missing one."""
    documented = _documented(_sections(README.read_text(encoding="utf-8"), "## Integrations"))
    where = {label: name for name, found in documented.items() for label in found["connectors"]}
    for connector in connectors.all_connectors():
        assert where.get(connector.label) == connector.category, (connector.label, connector.category)


def test_the_readme_calls_the_compatible_tools_external():
    """A reader must not take garmin-grafana for something Tributary runs."""
    text = _sections(README.read_text(encoding="utf-8"), "## Integrations")
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
    """Screenshots of the interface, kept in the repository."""
    text = README.read_text(encoding="utf-8")
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert images, "the README shows no screenshot"
    for path in images:
        assert not path.startswith(("http://", "https://")), f"{path} would be fetched from the network"
        assert (ROOT / path).is_file(), f"{path} is missing"
