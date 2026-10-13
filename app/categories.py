"""The categories every integration is grouped under, in the README and in the
UI. One fixed list: a connector names one of them (`Connector.category`), and a
test keeps the README's tables in step.

A category is also where the *compatible tools* sit: maintained tools that write
into the same InfluxDB and are not run by Tributary (docs/compatible-tools.md).
They are shown next to Tributary's own connectors so a reader sees everything
that feeds a domain, and they are never scheduled, hidden or configured here.
"""
from typing import Dict, NamedTuple, Tuple

# The order the UI and the README list them in: the domains a person thinks
# about, from the body outwards.
CATEGORIES: Tuple[str, ...] = (
    "Body & health",
    "Medication",
    "Nutrition & fitness",
    "Sleep",
    "Reading & media",
    "Productivity",
)


class ExternalTool(NamedTuple):
    """A maintained tool that writes the same databases, one Tributary does not
    run. `writes` is where its numbers land, for the reader's benefit."""
    label: str
    url: str
    writes: str
    note: str


GARMIN_GRAFANA = ExternalTool(
    label="garmin-grafana",
    url="https://github.com/arpanghosh8453/garmin-grafana",
    writes="InfluxDB `GarminStats`",
    note="Activity, sleep, heart rate, body battery... from the Garmin API, with its own dashboards.",
)
WHOOPS = ExternalTool(
    label="Whoops",
    url="https://github.com/kryoseu/whoops",
    writes="PostgreSQL",
    note="Sleep, recovery, strain. Add a Grafana PostgreSQL datasource next to the InfluxDB ones.",
)
TAKEBACK = ExternalTool(
    label="Takeback",
    url="https://github.com/vchatela-org/takeback",
    writes="InfluxDB `MediaStats`, `ShoppingStats`, `DigitalStats`",
    note="GDPR “download your data” archives (Amazon, Google, Meta...): Kindle reading sessions, "
         "Audible, purchases, activity counts.",
)

# Which tools enrich which category. One tool may appear in several (Garmin
# covers activity *and* sleep); the UI shows them under each.
EXTERNAL_TOOLS: Dict[str, Tuple[ExternalTool, ...]] = {
    "Body & health": (GARMIN_GRAFANA,),
    "Medication": (),
    "Nutrition & fitness": (),
    "Sleep": (GARMIN_GRAFANA, WHOOPS),
    "Reading & media": (TAKEBACK,),
    "Productivity": (TAKEBACK,),
}


def category_of(connector) -> str:
    """The category a connector belongs to, checked by a test."""
    return connector.category


# One Bootstrap icon per category, for the section headings.
CATEGORY_ICONS: Dict[str, str] = {
    "Body & health": "bi-person-standing",
    "Medication": "bi-capsule",
    "Nutrition & fitness": "bi-activity",
    "Sleep": "bi-moon-stars",
    "Reading & media": "bi-headphones",
    "Productivity": "bi-check2-square",
}


def icon_for(name: str) -> str:
    return CATEGORY_ICONS.get(name, "bi-grid")
