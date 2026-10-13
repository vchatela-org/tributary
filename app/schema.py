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

# ── BodyStats: body composition ─────────────────────────────────────────
# Also written by other tools (a scale's history import, as its own source),
# always with float fields. `scale` and `user_id` are the Renpho connector's
# (the scale's model, the Renpho account's numeric id): kept from the sync it
# was ported from, as they key its series.

BODY_COMPOSITION = Measurement(
    database="BodyStats", name="body_composition", tags=("scale", "user_id"),
    fields={name: float for name in (
        "weight_kg", "bmi", "body_fat_pct", "body_water_pct", "muscle_pct", "bone_pct", "bmr_kcal",
        "visceral_fat", "subcutaneous_fat_pct", "protein_pct", "body_age", "lean_body_mass_kg",
        "fat_free_weight_kg", "heart_rate_bpm", "cardiac_index", "body_shape",
    )},
)

# Medication reminders' doses: MyTherapy and Medisafe, each under its own
# source. Medisafe's export alone carries `strength` and `note`; a dose's note
# is written whole, as the script that first wrote it did (and with
# TRIBUTARY_MEDISAFE_NOTES only).
MEDICATION = Measurement(
    database="BodyStats", name="medication", tags=("med", "status"),
    fields={"taken": int, "app_status": str, "dose": float, "unit": str, "delay_min": int,
            "strength": str, "note": str},
    text_max=None,
)
