from datetime import datetime, timezone
from typing import Optional

from fastapi.templating import Jinja2Templates

from app import categories
from app.__version__ import __version__
from app.config import settings
from app.points import _zone

templates = Jinja2Templates(directory="templates")


def localtime(value: Optional[datetime], seconds: bool = False) -> str:
    """A stored (naive UTC) time, in TRIBUTARY_TZ."""
    if value is None:
        return ""
    local = value.replace(tzinfo=timezone.utc).astimezone(_zone(settings.tz))
    return local.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def duration(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start or not end:
        return ""
    seconds = (end - start).total_seconds()
    return f"{seconds:.1f} s" if seconds < 60 else f"{int(seconds // 60)} min {int(seconds % 60):02d} s"


templates.env.filters["localtime"] = localtime
templates.env.globals["duration"] = duration
templates.env.globals["app_version"] = __version__
templates.env.globals["tz"] = settings.tz
templates.env.globals["max_upload_mb"] = settings.max_upload_mb
templates.env.globals["staged_ttl_hours"] = settings.staged_upload_ttl_hours
templates.env.globals["category_icon"] = categories.icon_for
