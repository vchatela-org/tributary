from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    connector: str
    trigger: str
    dry_run: bool
    full: bool
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    message: Optional[str] = None
    points: Optional[int] = None
    # The check this import applied ("Import for real"), when it was promoted.
    promoted_from: Optional[int] = None


class RunDetail(RunOut):
    preview: Optional[str] = None


class MeasurementOut(BaseModel):
    database: str
    name: str
    tags: List[str]
    fields: Dict[str, str]


class StagedOut(BaseModel):
    """An uploaded export that was checked and is kept for its import: the file
    lives until `expires_at`, and importing applies exactly what the check
    showed."""
    filename: str
    created_at: datetime
    expires_at: datetime
    points: Optional[int] = None


class PresetOut(BaseModel):
    key: str
    label: str
    chosen: bool


class ScheduleOut(BaseModel):
    """When a connector runs, and where that came from (the UI shows both)."""
    cron: Optional[str] = None
    label: str
    source: str                      # ui / env / interval / default / none
    source_label: str
    next_run: Optional[datetime] = None
    next_runs: List[datetime] = []
    # A value that could not be used (an unreadable expression), to show as-is.
    note: Optional[str] = None
    # The connector's own default, for "Reset to default".
    default_cron: Optional[str] = None
    default_label: Optional[str] = None
    presets: List[PresetOut] = []
    # The editor's starting values.
    at: str = "03:10"
    weekday: str = "1"
    custom: str = ""


class ScheduleIn(BaseModel):
    """Either an explicit `cron`, or a `preset` the server expands (so no
    JavaScript is needed to build one). `preset: "manual"` means *no schedule*
    and is stored as such, overriding the environment and the default."""
    cron: Optional[str] = None
    preset: Optional[str] = None
    at: str = ""
    weekday: str = ""
    custom: str = ""
    # Forget what the UI saved: TRIBUTARY_<KEY>_SCHEDULE, or the connector's own
    # default, applies again.
    reset: bool = False


class PreferencesIn(BaseModel):
    """The connectors to leave out of the overview. Display only: a hidden
    connector keeps running and keeps filling its series."""
    hidden: List[str] = []


class ConnectorOut(BaseModel):
    """What the API says of a connector. Never its settings: they may hold
    secrets."""
    key: str
    label: str
    description: str
    icon: str
    category: str
    configured: bool
    hidden: bool = False
    schedule: Optional[str] = None           # the label, e.g. "Daily at 03:10"
    schedule_cron: Optional[str] = None
    schedule_source: str = "none"
    next_run: Optional[datetime] = None
    staged: Optional[StagedOut] = None
    accepts_files: bool
    file_types: List[str]
    required_env: List[str]
    measurements: List[MeasurementOut]
    last_run: Optional[RunOut] = None
