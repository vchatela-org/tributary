"""Google Play's category of an app, for App Usage's `category` tag (opt-in:
TRIBUTARY_APP_USAGE_PLAY_LOOKUP).

Android lets a developer declare a category in the app's manifest (what App
Usage reads: GAME, VIDEO, SOCIAL...), and few do. Every app on Google Play has
a store category instead, which its public page carries as structured data
(`"applicationCategory": "VIDEO_PLAYERS"`). Google publishes no API for it, so
this reads that page:

  - only for the packages an import needs (used in the backup, and not already
    decided by the user's mapping, their choice in App Usage or the built-in
    list), one request each, DELAY_S apart, at most MAX_LOOKUPS and BUDGET_S
    per import; the rest wait for the next import;
  - cached in TRIBUTARY_DATA_DIR (CACHE_FILE), each answer for FOUND_DAYS, an
    app Google Play doesn't have (404: sideloaded, withdrawn, a system
    package) for MISSING_DAYS, so an import of the same backup asks nothing
    again and a dry run and its import agree;
  - the package name is the only thing sent (no cookie, no account), to
    play.google.com. Google knows the apps installed from Play already; the
    lookup is off by default all the same, as it tells Google which of them
    are used;
  - a page that fails to load (network, a 5xx, a 429) stops the lookups for
    this import, never the import: those apps fall back to the next rule.

The store's categories say what an app is, not how it is used (browsers are
COMMUNICATION, launchers TOOLS): GENRES maps them to Tributary's vocabulary,
and app_usage_categories.KNOWN, read first, corrects the well-known
exceptions.
"""
import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PLAY_URL = "https://play.google.com/store/apps/details"
CACHE_FILE = "app_usage_play.json"
TIMEOUT_S = 15
DELAY_S = 0.5
MAX_LOOKUPS = 400
BUDGET_S = 240
FOUND_DAYS = 180
MISSING_DAYS = 30
# An app page is ~1 MB; the category sits in its head.
MAX_BYTES = 4 * 2**20
# Generic, and nothing about the user (no address, no account).
USER_AGENT = "Mozilla/5.0 (compatible; Tributary)"

_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_CATEGORY = re.compile(rb'"applicationCategory"\s*:\s*"([A-Z_]+)"')

# Google Play's categories to Tributary's. GAME_* (every game genre) is games.
GENRES: Dict[str, str] = {
    "VIDEO_PLAYERS": "watching", "ENTERTAINMENT": "watching",
    "MUSIC_AND_AUDIO": "listening",
    "SOCIAL": "social", "DATING": "social",
    "COMMUNICATION": "communication",
    "BUSINESS": "productivity", "PRODUCTIVITY": "productivity", "EDUCATION": "productivity",
    "MAPS_AND_NAVIGATION": "navigation",
    "HOUSE_AND_HOME": "smart_home",
    "HEALTH_AND_FITNESS": "health", "MEDICAL": "health",
    "SHOPPING": "shopping", "FOOD_AND_DRINK": "shopping",
    "FINANCE": "finance",
    "NEWS_AND_MAGAZINES": "news", "WEATHER": "news", "SPORTS": "news",
    "PHOTOGRAPHY": "photos", "ART_AND_DESIGN": "photos",
    "TOOLS": "system", "PERSONALIZATION": "system", "LIBRARIES_AND_DEMO": "system",
    # What an app is there, not a use: travel booking, a car's app, books to
    # read (an audiobook app is in KNOWN), a lifestyle brand...
    "TRAVEL_AND_LOCAL": "other", "AUTO_AND_VEHICLES": "other", "BOOKS_AND_REFERENCE": "other",
    "LIFESTYLE": "other", "EVENTS": "other", "PARENTING": "other", "BEAUTY": "other", "COMICS": "other",
}


def category_of(genre: Optional[str]) -> Optional[str]:
    """Tributary's category for a Google Play category; None for none."""
    if not genre:
        return None
    if genre == "GAME" or genre.startswith("GAME_"):
        return "games"
    return GENRES.get(genre)


class Lookup(NamedTuple):
    """{package: Google Play's category, None when it has none}, and what the
    import says about it."""
    genres: Dict[str, Optional[str]]
    summary: List[str]


class PlayStoreUnreachable(Exception):
    pass


def fetch(package: str) -> Optional[str]:
    """The app's Google Play category; None when Google Play has no such app
    or its page carries none. PlayStoreUnreachable for anything else."""
    url = f"{PLAY_URL}?{urlencode({'id': package, 'hl': 'en'})}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (a fixed https address)
            page = response.read(MAX_BYTES)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise PlayStoreUnreachable(f"HTTP {exc.code}") from None
    except (URLError, OSError) as exc:
        raise PlayStoreUnreachable(type(exc).__name__) from None
    found = _CATEGORY.search(page)
    return found.group(1).decode() if found else None


class PlayCache:
    """{package: {"genre": str | None, "checked": "YYYY-MM-DD"}}, in a JSON file."""

    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.apps: Dict[str, Dict] = data.get("apps", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.apps = {}

    def fresh(self, package: str, today: date) -> bool:
        entry = self.apps.get(package)
        if not isinstance(entry, dict):
            return False
        try:
            checked = date.fromisoformat(entry["checked"])
        except (KeyError, TypeError, ValueError):
            return False
        return today - checked < timedelta(days=FOUND_DAYS if entry.get("genre") else MISSING_DAYS)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"apps": self.apps}, sort_keys=True, indent=0), encoding="utf-8")
        os.replace(tmp, self.path)


def lookup(packages: Iterable[str], data_dir: str, today: Optional[date] = None) -> Lookup:
    """Google Play's category of each package: from the cache, or read now
    within this import's budget."""
    today = today or date.today()
    cache = PlayCache(Path(data_dir) / CACHE_FILE)
    wanted = sorted({p for p in packages if _PACKAGE.match(p)})
    stale = [p for p in wanted if not cache.fresh(p, today)]
    asked = missing = 0
    left = 0
    error = None
    started = time.monotonic()
    for i, package in enumerate(stale):
        if asked >= MAX_LOOKUPS or time.monotonic() - started > BUDGET_S:
            left = len(stale) - i
            break
        if asked:
            time.sleep(DELAY_S)
        try:
            genre = fetch(package)
        except PlayStoreUnreachable as exc:
            error, left = str(exc), len(stale) - i
            break
        asked += 1
        missing += genre is None
        cache.apps[package] = {"genre": genre, "checked": today.isoformat()}
    if asked:
        cache.save()
    genres = {p: cache.apps[p].get("genre") for p in wanted if isinstance(cache.apps.get(p), dict)}
    summary = []
    if asked:
        summary.append(f"Google Play: {asked} app(s) looked up, {missing} not on it")
    if error:
        summary.append(f"Google Play unreachable ({error}): {left} app(s) left to the next import")
    elif left:
        summary.append(f"Google Play: {left} app(s) left to the next import")
    return Lookup(genres, summary)
