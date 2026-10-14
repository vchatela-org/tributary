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

# Workouts: MyFitnessPal's (its export's, including a linked device's daily
# calorie adjustment) and WHOOP's, each under its own source. `activity` is the
# app's own name for the exercise, a bounded vocabulary. The fields after
# `duration_s` are WHOOP's: its strain, heart rate and distances, the seconds
# spent in each heart-rate zone (zone 0 is below zone 1), and, from its export
# only, the share of the workout in zones 1-5 and whether GPS was on (0/1).
# `category` is the shared coarse word (six values); `sport` is WHOOP's own
# finer name (running, weightlifting...), a tag so a Grafana panel can group by
# it — InfluxQL groups by tags only, never by fields.
EXERCISE = Measurement(
    database="BodyStats", name="exercise", tags=("category", "sport"),
    fields={"activity": str, "energy_kcal": float, "duration_s": int,
            "strain": float, "avg_hr": float, "max_hr": float, "distance_m": float, "altitude_gain_m": float,
            "altitude_change_m": float, "percent_recorded": float,
            **{f"zone{n}_s": int for n in range(6)},
            **{f"zone{n}_pct": float for n in range(1, 6)},
            "gps_enabled": int},
)

# A device's day: WHOOP's physiological cycle (sleep onset to the next sleep
# onset), stamped at its start.
DAILY_ACTIVITY = Measurement(
    database="BodyStats", name="daily_activity", tags=(),
    fields={"strain": float, "energy_kcal": float, "avg_hr": float, "max_hr": float, "steps": int,
            "duration_s": int},
)

# ── SleepStats: sleep ───────────────────────────────────────────────────
# The homelab sleep contract, written by WHOOP and by a Sleep as Android
# history import. Where names overlap, the types are the ones already stored.
# One point per sleep, at its end; `kind` is `night` or `nap`. WHOOP's
# recovery, scored from the night's sleep, lands on that sleep's point.
SLEEP_SESSION = Measurement(
    database="SleepStats", name="sleep_session", tags=("kind",),
    fields={
        # the contract
        "start": int, "in_bed_s": int, "asleep_s": int, "awake_s": int, "light_s": int, "deep_s": int,
        "rem_s": int, "cycles": int, "awakenings": int, "tz_offset_min": int, "bedtime_min": int,
        "wake_min": int, "score": float, "respiration_rate": float, "resting_hr": float, "hrv_rmssd_ms": float,
        "avg_spo2_pct": float,
        # WHOOP's own
        "efficiency_pct": float, "consistency_pct": float, "recovery_score": float, "skin_temp_c": float,
        "calibrating": int, "no_data_s": int, "need_baseline_s": int, "need_debt_s": int, "need_strain_s": int,
        "need_nap_s": int, "sleep_need_s": int, "sleep_debt_s": int,
        # Sleep as Android's own (its export): heart rate over the session, its
        # own deep-sleep percentage, the night's subjective rating, an SDNN HRV
        # (a different statistic from RMSSD), snoring and the average noise
        # level, the zone IANA name, and the session's tags and note (text).
        # `manual` (a bool) is true for a session entered by hand, not tracked.
        "avg_hr": float, "min_hr": float, "max_hr": float, "deep_pct": float, "rating": float,
        "sdnn_ms": float, "noise_level": float, "snore_s": float,
        "tz": str, "tags": str, "note": str, "manual": bool,
    },
)

# Sleep as Android's hypnogram: exclusive stage segments (a moment counts
# once), stamped at the segment's start. `level` is Garmin's SleepStageLevel
# scale (0 deep, 1 light, 2 rem, 3 awake), so hypnograms line up across
# sources; `stage` repeats it as a word. The export stamps them in
# milliseconds, and two can land in the same second once Tributary floors them
# to its own precision: `last_wins` keeps the later one's fields rather than
# summing two durations, as InfluxDB's own write did.
SLEEP_STAGE = Measurement(
    database="SleepStats", name="sleep_stage", tags=(),
    fields={"level": int, "stage": str, "duration_s": int},
    last_wins=True,
)

# Sleep as Android's samples during a session, stamped at the sample's time:
# whichever it recorded -- heart rate, SpO2, SDNN. Same milli to whole-second
# folding as the stages: a second holding two samples keeps the later one.
SLEEP_VITALS = Measurement(
    database="SleepStats", name="sleep_vitals", tags=(),
    fields={"hr_bpm": float, "spo2_pct": float, "sdnn_ms": float},
    last_wins=True,
)
