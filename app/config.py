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
    # happens at startup itself.
    first_run_delay: float = 60.0

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{Path(self.data_dir) / 'tributary.db'}"


settings = Settings()
