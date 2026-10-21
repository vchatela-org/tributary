"""The categories every integration is grouped under, in the README and in the
UI. One fixed list: a connector names one of them (`Connector.category`), and a
test keeps the README's tables in step.

A category is also where the *compatible tools* sit: maintained tools that write
into the same InfluxDB and are not run by Tributary (docs/compatible-tools.md).
They are shown next to Tributary's own connectors so a reader sees everything
that feeds a domain, and they are never scheduled, hidden or configured here.
"""
from typing import Dict, NamedTuple, Optional, Tuple

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
    run. `writes` is where its numbers land, for the reader's benefit.
    `freshness` names its probe in app/freshness.py, if any: when its data last
    arrived, read from InfluxDB (timestamps only) and shown on its card."""
    label: str
    url: str
    writes: str
    note: str
    freshness: Optional[str] = None


GARMIN_GRAFANA = ExternalTool(
    label="garmin-grafana",
    url="https://github.com/arpanghosh8453/garmin-grafana",
    writes="InfluxDB `GarminStats`",
    note="Activity, sleep, heart rate, body battery... from the Garmin API, with its own dashboards.",
    freshness="garmin-grafana",
)
FITBIT_GRAFANA = ExternalTool(
    label="fitbit-grafana",
    url="https://github.com/arpanghosh8453/fitbit-grafana",
    writes="InfluxDB `FitbitHealthStats`",
    note="Heart rate, steps, sleep, SpO₂... from the Fitbit (Google Health) API, with its own dashboards.",
    freshness="fitbit-grafana",
)
# Whoops (kryoseu/whoops) was listed under Sleep until Tributary's own Whoop
# connector, built on it, replaced it: one writer per source, and it wrote
# PostgreSQL, which no shipped dashboard reads. It is credited in the README and
# in docs/connectors/whoop.md.
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
    "Body & health": (GARMIN_GRAFANA, FITBIT_GRAFANA),
    "Medication": (),
    "Nutrition & fitness": (),
    "Sleep": (GARMIN_GRAFANA, FITBIT_GRAFANA),
    "Reading & media": (TAKEBACK,),
    "Productivity": (TAKEBACK,),
}


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


# One identity hue per category: slots 1-6 of the dataviz reference palette, in
# CATEGORIES order (the order is what keeps neighbours apart for colour-blind
# readers, so a hue follows its category, never its position on a page). The
# hex values live in static/style.css (`--tb-c1`...), validated light and dark;
# a hue is always shown beside the category's or the source's name, never alone.
def hue_for(name: str) -> int:
    """The category's slot, 1-based; 0 (a neutral grey) for an unknown one."""
    return CATEGORIES.index(name) + 1 if name in CATEGORIES else 0
