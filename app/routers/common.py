"""What the web pages and the API share: starting a run, receiving an
upload, refusing cross-site requests."""
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request, UploadFile

from app import connectors, scheduler
from app.config import settings
from app.connectors.base import Connector
from app.database import uploads_dir


def same_origin(request: Request) -> None:
    """Tributary has no login (it runs locally or behind an auth proxy), so
    a page of another site must not be able to start a run in the user's
    browser. Browsers say where a request comes from in Sec-Fetch-Site; one
    too old for that still sends Origin with a form POST, which must then
    be this host. A client sending neither (curl, a script) is not a page."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site not in ("same-origin", "none"):
            raise HTTPException(status_code=403, detail="Cross-site request refused")
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if origin == "null" or urlparse(origin).netloc.lower() != host.split(",")[0].strip().lower():
        raise HTTPException(status_code=403, detail="Cross-site request refused")


def connector_or_404(key: str) -> Connector:
    connector = connectors.get(key)
    if connector is None:
        raise HTTPException(status_code=404, detail="Unknown connector")
    return connector


def queue_run(connector: Connector, dry_run: bool = False, full: bool = False) -> None:
    if not scheduler.request(connector.key, trigger="manual", dry_run=dry_run, full=full):
        raise HTTPException(status_code=409, detail=f"{connector.label} is not configured")


def queue_upload(connector: Connector, upload: UploadFile, dry_run: bool = False) -> str:
    """Save the upload where only the run reads it (deleted after), queue
    its import. Returns the original file name."""
    if not connector.accepts_files:
        raise HTTPException(status_code=400, detail=f"{connector.label} takes no file")
    filename = Path(upload.filename or "upload").name
    if not filename.lower().endswith(connector.file_types):
        raise HTTPException(status_code=400,
                            detail=f"{connector.label} takes {', '.join(connector.file_types)} files")
    suffix = next(t for t in connector.file_types if filename.lower().endswith(t))
    target = uploads_dir() / f"{uuid.uuid4().hex}{suffix}"
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    with open(target, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File larger than {settings.max_upload_mb} MB")
            out.write(chunk)
    if not scheduler.request(connector.key, trigger="upload", dry_run=dry_run, file=str(target), filename=filename):
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=f"{connector.label} can't take a file now")
    return filename


def safe_back(request: Request, default: str = "/") -> str:
    """The page a form was posted from -- its path only, so never out of
    this app, whatever host the proxy in front of it answers on. "//host"
    and "/\\host" are paths a browser reads as another site."""
    referer = urlparse(request.headers.get("referer") or "")
    back = (referer.path or default) + (f"?{referer.query}" if referer.query else "")
    if not back.startswith("/") or back.startswith("//") or "\\" in back:
        return default
    return back
