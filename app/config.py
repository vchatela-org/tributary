"""Core settings, from the environment (TRIBUTARY_*) or a .env file.
Each connector reads its own settings the same way, under its own prefix
(TRIBUTARY_ABS_* for Audiobookshelf): see app/connectors/."""
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRIBUTARY_", env_file=".env", extra="ignore")

    # Run history and connector cursors only: the measurements go to InfluxDB.
    data_dir: str = "./data"
    database_url: Optional[str] = None
    debug: bool = False

    # InfluxDB 1.x. Without a URL nothing is written; a dry run still works.
    influxdb_url: Optional[str] = None
    # The `user` tag of every point (a connector with several accounts sets
    # its own per point).
    influxdb_user: Optional[str] = None
    # Missing databases are created, with year-long shards; existing ones are
    # never altered.
    influxdb_create_databases: bool = True

    # local_hour / local_weekday fields: InfluxQL can't shift a time zone.
    tz: str = "Europe/Paris"

    # Largest file the upload form takes.
    max_upload_mb: int = 200

    # Seconds between the start and the first scheduled run: a run never
    # happens at startup itself. Also how long a missed run waits before its
    # one catch-up (never several: app/scheduler.py).
    first_run_delay: float = 60.0

    # How long an uploaded export that was *checked* (a dry run) is kept so
    # "Import for real" does not ask for it again. It holds personal data: past
    # this, the file and its row are deleted (app/runner.py purge_staged).
    staged_upload_ttl_hours: float = 24.0

    # garmin-grafana's freshness line (app/freshness.py): the database it writes,
    # of which only two timestamps are read. Empty: no line. Its newest data older
    # than the hours below is marked stale (a warning: Tributary does not run it).
    garmin_grafana_database: str = "GarminStats"
    garmin_grafana_stale_hours: float = 24.0

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{Path(self.data_dir) / 'tributary.db'}"


settings = Settings()
