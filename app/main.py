import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import scheduler
from app.__version__ import __version__
from app.config import settings
from app.database import init_db
from app.routers import api, connections, web

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
)
init_db()
# Connectors run in a background thread; the first scheduled run waits
# TRIBUTARY_FIRST_RUN_DELAY seconds. Nothing here blocks the start.
scheduler.start()

app = FastAPI(
    title="Tributary",
    version=__version__,
    description="Pulls personal measurements from many sources into InfluxDB 1.x, for Grafana.",
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(web.router)
app.include_router(connections.router)
app.include_router(api.router)
