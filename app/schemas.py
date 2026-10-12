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


class RunDetail(RunOut):
    preview: Optional[str] = None


class MeasurementOut(BaseModel):
    database: str
    name: str
    tags: List[str]
    fields: Dict[str, str]


class ConnectorOut(BaseModel):
    """What the API says of a connector. Never its settings: they may hold
    secrets."""
    key: str
    label: str
    description: str
    configured: bool
    schedule: Optional[str] = None
    # Whether it takes an upload of one file (the UI's form, POST
    # /api/connectors/{key}/upload). A connector that reads a folder without
    # taking uploads still lists file_types: those are the files it accepts
    # there.
    accepts_files: bool
    file_types: List[str]
    required_env: List[str]
    measurements: List[MeasurementOut]
    last_run: Optional[RunOut] = None
