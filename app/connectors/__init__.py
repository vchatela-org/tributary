"""Connector registry: one instance per source, in the order the UI lists
them. A new connector is a module in this package, registered below."""
from typing import Dict, List, Optional

from app.connectors.base import Connector

_connectors: Dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _connectors[connector.key] = connector
    return connector


def unregister(key: str) -> None:
    _connectors.pop(key, None)


def get(key: str) -> Optional[Connector]:
    return _connectors.get(key)


def all_connectors() -> List[Connector]:
    return list(_connectors.values())


def _register_builtin() -> None:
    from app.connectors.audiobookshelf import AudiobookshelfConnector
    from app.connectors.medisafe import MedisafeConnector
    from app.connectors.myfitnesspal import MyFitnessPalConnector
    from app.connectors.mytherapy import MyTherapyConnector
    from app.connectors.renpho import RenphoConnector
    from app.connectors.sleep_as_android import SleepAsAndroidConnector
    from app.connectors.todoist import TodoistConnector
    from app.connectors.whoop import WhoopConnector

    register(AudiobookshelfConnector())
    register(RenphoConnector())
    register(TodoistConnector())
    register(MyTherapyConnector())
    register(MyFitnessPalConnector())
    register(MedisafeConnector())
    register(WhoopConnector())
    register(SleepAsAndroidConnector())


_register_builtin()
