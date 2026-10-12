"""The connector contract: one module per source, registered in
app/connectors/__init__.py.

A connector declares:
  - `key`: its id in the URL and the API, and the `source` tag of its points
    (one writer per source: no other tool writes this source's series);
  - `label`, `description`;
  - `settings_class`: its settings, read from the environment under its own
    prefix (TRIBUTARY_<KEY>_...), secrets as SecretStr so they never show in
    a log or a repr; never stored, never returned by the API;
  - `measurements`: what it writes, with the declared field types
    (app/schema.py for the shared ones);
and implements one or both inputs:
  - API: `api_configured()` and `sync(cursor) -> SyncResult(points, new cursor)`.
    The cursor is the connector's own JSON-able state (e.g. the newest update
    it has seen), saved after a successful run only, None on the first run
    (and on a full one, but for what `full_cursor` keeps). A source made of
    independent parts (several accounts) may report the parts that failed
    in `errors` instead of raising: the others are written and the run shows
    as failed;
  - files: `file_types` (accepted suffixes) and `import_file(path) -> points`,
    for an upload in the UI and, when TRIBUTARY_<KEY>_WATCH_DIR is set, the
    files of a watched folder (all of them at once through `import_files`;
    a hidden file is not one: a NAS writes its own sidecars next to the
    exports). With `rebuild_own_source`, a change in the folder drops the
    connector's own series and rewrites them from every file in it (the
    folder is then the source of truth); such a connector sets
    `accepts_uploads = False` (one file alone can't be merged with the
    others) and is asked for the folder even when it holds no file at all,
    so that it can refuse rather than let the series be emptied.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterable, List, Optional, Tuple, Type

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.points import Measurement, Point


class ConnectorError(Exception):
    """A run that could not complete, with a message meant for the UI.
    Never put a secret in it."""


class ConnectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Minutes between two scheduled runs.
    interval_minutes: int = 60
    # File connectors: a folder whose new or changed files are imported at
    # each scheduled run (mount it read-only).
    watch_dir: Optional[str] = None


@dataclass
class SyncResult:
    points: List[Point]
    cursor: Any = None
    # What the run read, for the run's message ("3 new sessions").
    summary: List[str] = field(default_factory=list)
    # Parts of the source that failed (one account of several), for the
    # run's message: the points of the others are still written and the
    # cursor saved, so the connector keeps the failed parts' cursor as it
    # was (the next run reads them again). Never put a secret in it.
    errors: List[str] = field(default_factory=list)


class Connector:
    key: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "bi-plug"
    settings_class: ClassVar[Type[ConnectorSettings]] = ConnectorSettings
    measurements: ClassVar[Tuple[Measurement, ...]] = ()
    # Environment variables the API input needs, for the UI's hint.
    required_env: ClassVar[Tuple[str, ...]] = ()
    file_types: ClassVar[Tuple[str, ...]] = ()
    # Whether the UI offers an upload of a single file. A connector whose
    # watched folder is its source of truth (rebuild_own_source) sets this
    # False: one file can't be merged with the others, and rebuilding its
    # series from it alone would drop the rest.
    accepts_uploads: ClassVar[bool] = True
    rebuild_own_source: ClassVar[bool] = False

    def __init__(self) -> None:
        self.settings = self.settings_class()

    @property
    def source(self) -> str:
        return self.key

    @property
    def env_prefix(self) -> str:
        return self.settings_class.model_config.get("env_prefix", "")

    # -- Inputs --------------------------------------------------------------

    def api_configured(self) -> bool:
        return False

    @property
    def accepts_files(self) -> bool:
        """Whether an upload of one file makes sense: the UI's form, and
        POST /api/connectors/{key}/upload."""
        return bool(self.file_types) and self.accepts_uploads

    @property
    def watch_dir(self) -> Optional[Path]:
        """The folder read at each run (`file_types` says which files), or
        None when the connector has none."""
        return Path(self.settings.watch_dir) if self.file_types and self.settings.watch_dir else None

    def is_configured(self) -> bool:
        """Whether it runs on a schedule: its API is set up, or it watches a
        folder. An upload works either way."""
        return self.api_configured() or self.watch_dir is not None

    def sync(self, cursor: Any) -> SyncResult:
        raise NotImplementedError

    def full_cursor(self, cursor: Any) -> Any:
        """The cursor a full resync starts from: nothing by default (read
        everything again). A connector keeps here what must not change
        between runs, e.g. a time it had to choose for a point."""
        return None

    def import_file(self, path: Path) -> Iterable[Point]:
        raise NotImplementedError

    def import_files(self, paths: Iterable[Path]) -> Iterable[Point]:
        """The files of the watched folder, as one input. A connector whose
        exports supersede one another merges them here (each file replaces
        what the older ones said over its own range); by default each file is
        imported on its own. `paths` is empty when the folder holds no file
        the connector accepts: one whose folder is its source of truth raises
        rather than let the core drop its series with nothing to write
        back."""
        for path in paths:
            try:
                found = list(self.import_file(path))
            except ConnectorError as exc:
                raise ConnectorError(f"{path.name}: {exc}") from None
            except (OSError, ValueError, KeyError) as exc:
                raise ConnectorError(f"{path.name}: unreadable ({type(exc).__name__}: {exc}).") from None
            yield from found

    # -- Schedule ------------------------------------------------------------

    def interval_seconds(self) -> float:
        return max(1, self.settings.interval_minutes) * 60.0

    def schedule_label(self) -> str:
        minutes = max(1, self.settings.interval_minutes)
        if minutes % 1440 == 0:
            every = "day" if minutes == 1440 else f"{minutes // 1440} days"
        elif minutes % 60 == 0:
            every = "hour" if minutes == 60 else f"{minutes // 60} hours"
        else:
            every = f"{minutes} min"
        return f"every {every}"
