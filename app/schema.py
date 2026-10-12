"""The measurements of the shared schema that Tributary's connectors write,
declared once (docs/schema.md is the human version; a test keeps both in
step). Other tools write into the same databases: a field's type here must
be the type they write, or InfluxDB 1.x rejects one of them.

One database per domain; within it, `source` names the tracker.
"""
from app.points import Measurement

# ── MediaStats: reading and listening ──────────────────────────────────
# Also written by Takeback (Kindle, Audible, as source=kindle/audible,
# with a `pages` int field of its own).

READING_SESSION = Measurement(
    database="MediaStats", name="reading_session", tags=("kind", "device"),
    fields={"minutes": float, "sessions": int, "title": str},
    local_time=True,
)

BOOK_FINISHED = Measurement(
    database="MediaStats", name="book_finished", tags=("kind",),
    fields={"count": int, "title": str},
    local_time=True,
)
