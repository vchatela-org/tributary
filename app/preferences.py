"""Per-install UI preferences, in SQLite next to the run history: they survive
a restart and a container rebuild, and need no environment variable and no
redeploy.

One of them: which connectors the user chose to *see*. Hiding is **display
only** -- a hidden connector keeps being scheduled, keeps running and keeps
filling its series; it is left out of the overview, and it stays listed on the
settings page, where it can be brought back. The set stored is the *hidden* one,
so a connector added by a later version is visible by default.
"""
import json
from typing import Iterable, Set

from sqlalchemy.orm import Session

from app.models import Preference

HIDDEN_CONNECTORS = "hidden_connectors"


def _value(db: Session, key: str):
    row = db.get(Preference, key)
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


def _store(db: Session, key: str, value) -> None:
    row = db.get(Preference, key) or Preference(key=key)
    row.value = json.dumps(value, sort_keys=True)
    db.add(row)


def hidden_connectors(db: Session) -> Set[str]:
    """The connectors left out of the overview. Empty on a fresh install:
    nothing is hidden behind a discovery step."""
    value = _value(db, HIDDEN_CONNECTORS)
    return set(value) if isinstance(value, list) else set()


def set_hidden_connectors(db: Session, keys: Iterable[str], known: Iterable[str]) -> Set[str]:
    """Replace the hidden set. Only connectors that exist are kept, so a
    renamed or removed one does not linger in the file."""
    allowed = set(known)
    wanted = {str(key) for key in keys} & allowed
    _store(db, HIDDEN_CONNECTORS, sorted(wanted))
    return wanted
