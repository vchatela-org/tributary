"""MyFitnessPal: the exact series the one-off import script wrote
(docs/porting.md), from an export imported in the UI."""
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import runner
from app.config import settings
from app.connectors import myfitnesspal
from app.main import app
from tests.fakes import (epoch, mfp_exercise_row, mfp_food_row, mfp_weight_row,
                         write_myfitnesspal_export)

# The `user` tag: the script's --user, TRIBUTARY_INFLUXDB_USER here.
USER = "alice"


@pytest.fixture()
def connector(myfitnesspal_connector, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", USER)
    return myfitnesspal_connector


def _import(db, connector, tmp_path, weight=(), food=(), exercise=(), name="export.zip", span=None, **options):
    """Import an export as the UI's upload does."""
    path = write_myfitnesspal_export(tmp_path / name, weight=weight, food=food, exercise=exercise, span=span)
    return runner.run(db, connector, file=str(path), filename=name, trigger="upload", **options)


def _mfp(day_ts, measurement="body_composition", **tags):
    return ("BodyStats", measurement, tuple(sorted({"source": "myfitnesspal", "user": USER, **tags}.items())), day_ts)


def test_an_import_writes_the_scripts_exact_lines(connector, influx, db, tmp_path):
    """One point per weigh-in, meal and workout, at noon local, with the
    script's fields and types: weight_kg and body_fat_pct (float), the macros
    always and a secondary nutrient only when reported, and a workout's
    category, activity, energy_kcal and duration_s (int)."""
    run = _import(db, connector, tmp_path,
                  weight=[mfp_weight_row(weight="70.1", fat="18.2")],
                  food=[mfp_food_row(**{"Calories": "600", "Lipides (g)": "20", "Glucides (g)": "60",
                                        "Protéines (g)": "30", "Fibres": "5", "Sucres": "0"})],
                  exercise=[mfp_exercise_row(activity="Course", kcal="300", minutes="30")])
    assert run.status == "ok" and run.points == 3
    assert run.message == ("Imported export.zip: 3 point(s) written (BodyStats 3), "
                           "after dropping its own series from 2026-03-01 to 2026-03-01.")
    # 12:00 in Paris is 11:00 UTC in March (CET).
    ts = epoch(2026, 3, 1, 11, 0)
    assert influx.written_lines() == [
        f'body_composition,source=myfitnesspal,user={USER} body_fat_pct=18.2,weight_kg=70.1 {ts}',
        f'exercise,category=run,source=myfitnesspal,user={USER} '
        f'activity="Course",duration_s=1800i,energy_kcal=300.0 {ts}',
        f'nutrition,meal=lunch,source=myfitnesspal,user={USER} '
        f'carbs_g=60.0,energy_kcal=600.0,fat_g=20.0,fiber_g=5.0,protein_g=30.0 {ts}',
    ]


def test_a_weigh_in_another_source_already_has_is_not_imported(connector, influx, db, tmp_path):
    """The app shows the scales' readings; only the app's own weigh-ins (a day
    no other source has within WEIGHT_TOLERANCE_KG) are written."""
    influx.points[("BodyStats", "body_composition",
                   (("source", "renpho"), ("user", USER)), epoch(2026, 3, 1, 8, 0))] = {"weight_kg": "70.05"}
    influx.points[("BodyStats", "body_composition",
                   (("source", "withings"), ("user", USER)), epoch(2026, 3, 2, 8, 0))] = {"weight_kg": "80.0"}
    run = _import(db, connector, tmp_path, weight=[
        mfp_weight_row(day="2026-03-01", weight="70.1"),   # renpho has 70.05 the same day: skipped
        mfp_weight_row(day="2026-03-02", weight="70.1"),   # withings' 80.0 is outside tolerance: imported
        mfp_weight_row(day="2026-03-03", weight="70.1"),   # no other source that day: imported
        mfp_weight_row(day="2026-03-04", weight=""),        # no weight: not a record
    ])
    assert run.status == "ok" and run.points == 2
    assert sorted(ts for tags, _f, ts in influx.stored("body_composition")
                  if dict(tags).get("source") == "myfitnesspal") == [
        epoch(2026, 3, 2, 11, 0), epoch(2026, 3, 3, 11, 0)]


def test_records_are_stamped_at_noon_local_across_a_dst_change(connector, influx, db, tmp_path):
    """The export has dates only: 12:00 local, so a day lands on the right date
    whether a query groups days in UTC or in Paris."""
    _import(db, connector, tmp_path, weight=[
        mfp_weight_row(day="2026-03-28", weight="70.1"),  # CET (+01:00)
        mfp_weight_row(day="2026-03-30", weight="70.1"),  # CEST (+02:00): the clocks went forward on the 29th
    ])
    assert [ts for _tags, _fields, ts in influx.stored("body_composition")] == [
        epoch(2026, 3, 28, 11, 0), epoch(2026, 3, 30, 10, 0)]


def test_an_import_replaces_its_own_series_over_the_days_it_covers_only(connector, influx, db, tmp_path):
    """The export covers the date range the user picked (the app writes it in
    each member's name): over those days a workout it no longer has goes with
    it; the days outside them keep what they had, so an export of the last
    month never empties the years before it. Another source, or another
    user's points (the script's --replace was per user), are never
    touched."""
    influx.points[("BodyStats", "body_composition",
                   (("source", "renpho"), ("user", USER)), epoch(2026, 3, 10, 8, 0))] = {"weight_kg": "90.0"}
    before, inside, after = epoch(2026, 2, 28, 11, 0), epoch(2026, 3, 20, 11, 0), epoch(2026, 4, 1, 10, 0)
    for ts in (before, inside, after):
        influx.points[_mfp(ts, "exercise", category="run")] = {"activity": '"Course"', "energy_kcal": "300.0"}
    bob = ("BodyStats", "exercise", (("category", "run"), ("source", "myfitnesspal"), ("user", "bob")), inside)
    influx.points[bob] = {"energy_kcal": "1.0"}
    run = _import(db, connector, tmp_path, span=("2026-03-01", "2026-03-31"),
                  weight=[mfp_weight_row(day="2026-03-10", weight="70.1")])
    assert run.message == ("Imported export.zip: 1 point(s) written (BodyStats 1), "
                           "after dropping its own series from 2026-03-01 to 2026-03-31.")
    # Local midnights: 2026-03-01 00:00 is CET (+01:00), 2026-04-01 00:00 CEST (+02:00).
    assert [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))] == [
        f'DELETE FROM "{m}" WHERE "source" = \'myfitnesspal\' AND "user" = \'{USER}\' '
        "AND time >= '2026-02-28T23:00:00Z' AND time < '2026-03-31T22:00:00Z'"
        for m in ("body_composition", "nutrition", "exercise")]
    assert sorted((tags["user"], ts) for tags, _f, ts in influx.stored("exercise")) == [
        (USER, before), (USER, after), ("bob", inside)]
    assert sorted((tags["source"], ts) for tags, _f, ts in influx.stored("body_composition")) == [
        ("myfitnesspal", epoch(2026, 3, 10, 11, 0)), ("renpho", epoch(2026, 3, 10, 8, 0))]


def test_each_series_is_replaced_over_its_own_files_days(connector, influx, tmp_path):
    """A record outside the range the names give widens its own file's span
    only (it is rewritten, so its day is replaced): a stray weigh-in must not
    drop meals and workouts the export doesn't hold. Without a range in the
    names, a file spans its records' own dates; a file with neither leaves
    its series as it is."""
    def spans(name, span, **tables):
        return connector.import_file(write_myfitnesspal_export(tmp_path / name, span=span, **tables)).spans

    weigh_in, workout = [mfp_weight_row(day="2026-04-02")], [mfp_exercise_row(day="2026-03-05")]
    march = (date(2026, 3, 1), date(2026, 3, 31))
    assert spans("a.zip", ("2026-03-01", "2026-03-31"), weight=weigh_in, exercise=workout) == {
        "body_composition": (date(2026, 3, 1), date(2026, 4, 2)), "nutrition": march, "exercise": march}
    assert spans("b.zip", None, weight=weigh_in, exercise=workout) == {
        "body_composition": (date(2026, 4, 2), date(2026, 4, 2)), "exercise": (date(2026, 3, 5), date(2026, 3, 5))}


def test_a_date_in_a_folder_name_does_not_widen_the_span(connector, influx, tmp_path):
    """Only a file name's range counts: a folder named after a later date
    would otherwise drop days the export doesn't hold."""
    path = write_myfitnesspal_export(tmp_path / "a.zip", span=("2026-03-01", "2026-03-31"),
                                     weight=[mfp_weight_row(day="2026-03-10")])
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tmp_path / "b.zip", "w") as nested:
        for name in source.namelist():
            nested.writestr(f"2026-10-20/{name}", source.read(name))
    assert set(connector.import_file(tmp_path / "b.zip").spans.values()) == {(date(2026, 3, 1), date(2026, 3, 31))}


def test_two_exports_in_one_zip_are_refused(connector, influx, db, tmp_path):
    """Their ranges would be replaced with one of them only."""
    first = write_myfitnesspal_export(tmp_path / "a.zip", span=("2023-01-01", "2023-12-31"),
                                      weight=[mfp_weight_row(day="2023-03-10")])
    with zipfile.ZipFile(first) as source, zipfile.ZipFile(tmp_path / "both.zip", "w") as both:
        for name in source.namelist():
            both.writestr(f"2023/{name}", source.read(name))
            both.writestr(f"2024/{name}", source.read(name))
    run = runner.run(db, connector, file=str(tmp_path / "both.zip"), filename="both.zip", trigger="upload")
    assert run.status == "error" and "more than one weight CSV" in run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_a_renamed_column_fails_the_import_rather_than_write_zeros(connector, influx, db, tmp_path):
    path = tmp_path / "renamed.zip"
    write_myfitnesspal_export(path, food=[mfp_food_row(**{"Calories": "600"})])
    with zipfile.ZipFile(path) as source:
        members = {name: source.read(name).decode() for name in source.namelist()}
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text.replace("Lipides (g)", "Lipides"))
    run = runner.run(db, connector, file=str(path), filename="renamed.zip", trigger="upload")
    assert run.status == "error" and "the food CSV has no column 'Lipides (g)'" in run.message
    assert influx.stored("nutrition") == []


def test_a_malformed_csv_is_a_clear_error(connector, influx, db, tmp_path):
    """The csv module's own error (a field over its size limit) names the
    file, like any unreadable export, instead of an unexpected error."""
    huge = "x" * 200_000
    run = _import(db, connector, tmp_path, name="huge.zip",
                  food=[mfp_food_row(**{"Calories": "1", "export_headers.note": huge})])
    assert run.status == "error" and run.message.startswith("huge.zip: unreadable (Error: field larger")


def test_the_dedup_reads_only_the_weigh_ins_days(connector, influx, db, tmp_path):
    """The other sources' readings of this user, over the days the weigh-ins
    CSV covers -- not their whole history."""
    _import(db, connector, tmp_path, span=("2026-03-01", "2026-03-31"), weight=[mfp_weight_row(day="2026-03-10")])
    selects = [q for _db, q in influx.queries if q.startswith("SELECT")]
    assert selects == [f'SELECT "weight_kg" FROM "body_composition" WHERE "user" = \'{USER}\' '
                       f'AND "source" != \'myfitnesspal\' '
                       "AND time >= '2026-02-28T23:00:00Z' AND time < '2026-03-31T22:00:00Z'"]


def test_an_export_without_weigh_ins_reads_no_influxdb(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path, exercise=[mfp_exercise_row()])
    assert run.status == "ok"
    assert not [q for _db, q in influx.queries if q.startswith("SELECT")]


def test_a_fresh_influxdb_without_bodystats_imports_every_weigh_in(connector, influx, db, tmp_path):
    """No database yet means no other source's reading: the import creates
    it, as any first write does."""
    run = _import(db, connector, tmp_path, weight=[mfp_weight_row()])
    assert run.status == "ok" and run.points == 1, run.message
    assert "BodyStats" in influx.databases


def test_a_dry_run_without_influxdb_says_the_dedup_needs_it(connector, influx, db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_url", None)
    path = write_myfitnesspal_export(tmp_path / "export.zip", weight=[mfp_weight_row()])
    run = runner.run(db, connector, dry_run=True, file=str(path), filename="export.zip", trigger="upload")
    assert run.status == "error"
    assert run.message.startswith("export.zip: the weigh-in dedup reads the other sources' readings in InfluxDB")
    assert "TRIBUTARY_INFLUXDB_URL" in run.message


def test_two_weigh_ins_or_meals_on_one_day_keep_the_later_one(connector, influx, db, tmp_path):
    """They share a key (noon on the date): the later row wins, as the
    script's consecutive writes did, instead of the numbers being summed."""
    _import(db, connector, tmp_path,
            weight=[mfp_weight_row(weight="70.1", fat="18.0"), mfp_weight_row(weight="70.3")],
            food=[mfp_food_row(meal="Snacks", **{"Calories": "100"}), mfp_food_row(meal="Snacks", **{"Calories": "150"})])
    (_tags, weigh_in, _ts), = influx.stored("body_composition")
    assert weigh_in == {"weight_kg": "70.3", "body_fat_pct": "18.0"}
    (_tags, meal, _ts), = influx.stored("nutrition")
    assert meal["energy_kcal"] == "150.0"


def test_a_dry_run_of_an_import_drops_nothing(connector, influx, db, tmp_path):
    """*Dry run* shows the line protocol and writes nothing at all, the drop
    included."""
    path = write_myfitnesspal_export(tmp_path / "export.zip", weight=[mfp_weight_row()])
    run = runner.run(db, connector, dry_run=True, file=str(path), filename="export.zip", trigger="upload")
    assert run.status == "ok" and run.points == 1
    # Only the weigh-ins have a span (the other files are empty): only they are named.
    assert run.message == ("Dry run. Imported export.zip: 1 point(s) would be written (BodyStats 1), "
                           "after dropping its own body_composition from 2026-03-01 to 2026-03-01.")
    assert "body_composition,source=myfitnesspal" in run.preview
    assert influx.written_lines() == []
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_duplicate_workouts_are_collapsed_as_the_script_dropped_them(connector, influx, db, tmp_path):
    """Rows identical in date, activity, calories and minutes (a linked app
    pushed the same workout again), and same-day workouts with the same
    calories (two linked apps each pushed every one): the shortest is kept."""
    run = _import(db, connector, tmp_path, exercise=[
        mfp_exercise_row(day="2026-03-01", activity="Course", kcal="300", minutes="30"),
        mfp_exercise_row(day="2026-03-01", activity="Course", kcal="300", minutes="30"),   # identical
        mfp_exercise_row(day="2026-03-01", activity="Aérobic, général", kcal="300", minutes="31"),
        mfp_exercise_row(day="2026-03-02", activity="Marche", kcal="100", minutes="20"),
        mfp_exercise_row(day="2026-03-02", activity="Natation", kcal="150", minutes="40"),
    ])
    assert run.points == 3
    by_ts = {ts: dict(tags)["category"] for tags, _f, ts in influx.stored("exercise")}
    assert by_ts[epoch(2026, 3, 1, 11, 0)] == "run"           # the generic "Aérobic" copy is dropped
    assert by_ts[epoch(2026, 3, 2, 11, 0)] == "walk"          # two workouts that day: a second apart
    assert by_ts[epoch(2026, 3, 2, 11, 0) + 1] == "swim"


def test_a_calorie_adjustment_is_not_a_workout_and_keeps_no_duration(connector, influx, db, tmp_path):
    """A linked device's daily calorie adjustment, not Garmin's step count:
    the script wrote it under its own category and dropped the placeholder
    "1 minute"."""
    _import(db, connector, tmp_path, exercise=[
        mfp_exercise_row(day="2026-03-01", activity="Garmin Connect calorie adjustment",
                         kcal="250", minutes="1")])
    (tags, fields, _ts), = influx.stored("exercise")
    assert tags["category"] == "adjustment"
    assert fields == {"activity": '"Garmin Connect calorie adjustment"', "energy_kcal": "250.0"}


def test_a_zero_secondary_nutrient_is_left_absent_and_a_macro_zero_is_kept(connector, influx, db, tmp_path):
    """A 0 in a secondary nutrient means "not reported" (the export writes its
    0); the macros always carry their 0."""
    _import(db, connector, tmp_path, food=[
        mfp_food_row(meal="Snacks", **{"Calories": "0", "Lipides (g)": "0", "Glucides (g)": "0",
                                       "Protéines (g)": "0", "Fibres": "0", "Sodium (mg)": "5"})])
    (tags, fields, _ts), = influx.stored("nutrition")
    assert tags["meal"] == "snacks"
    assert fields == {"carbs_g": "0.0", "energy_kcal": "0.0", "fat_g": "0.0", "protein_g": "0.0",
                      "sodium_mg": "5.0"}


def test_an_unknown_meal_fails_the_import(connector, influx, db, tmp_path):
    run = _import(db, connector, tmp_path, food=[mfp_food_row(meal="Brunch", **{"Calories": "400"})])
    assert run.status == "error" and "unknown meal 'Brunch'" in run.message
    assert influx.stored("nutrition") == []
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_a_file_that_is_not_an_export_names_what_is_missing(connector, influx, db, tmp_path):
    path = tmp_path / "shopping.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("items.csv", "item,price\nmilk,1.20\n")
    run = runner.run(db, connector, file=str(path), filename="shopping.zip", trigger="upload")
    assert run.status == "error"
    assert "not a MyFitnessPal export: no exercise, food, weight CSV" in run.message


def test_an_export_holding_no_record_is_refused_rather_than_empty_the_series(connector, influx, db, tmp_path):
    """An export with its headers and nothing else: importing it would drop the
    series with nothing to write back, so the run fails instead."""
    run = _import(db, connector, tmp_path, name="empty.zip")
    assert run.status == "error" and "holds nothing to import" in run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_the_user_tag_is_required_for_the_weigh_in_dedup(myfitnesspal_connector, influx, db, tmp_path,
                                                         monkeypatch):
    """The dedup reads the other sources' readings of `user`: with none set,
    every weigh-in the scales already reported would be imported again."""
    monkeypatch.setattr(settings, "influxdb_user", "")
    path = write_myfitnesspal_export(tmp_path / "export.zip", weight=[mfp_weight_row()])
    run = runner.run(db, myfitnesspal_connector, file=str(path), filename="export.zip", trigger="upload")
    assert run.status == "error" and "TRIBUTARY_INFLUXDB_USER is not set" in run.message
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_the_activity_category_map():
    """First matching prefix wins, so "vélo elliptique" is not "bike"; a linked
    device's adjustment has its own category."""
    assert myfitnesspal.category("Course") == "run"
    assert myfitnesspal.category("Vélo") == "bike"
    assert myfitnesspal.category("Vélo elliptique") == "elliptical"
    assert myfitnesspal.category("Musculation") == "strength"
    assert myfitnesspal.category("Garmin Connect calorie adjustment") == "adjustment"
    assert myfitnesspal.category("Yoga") == "other"


def test_the_declared_series_is_the_scripts(connector):
    """The tags and types the script wrote: body_composition is shared (the
    Renpho connector's), declared once in app/schema.py."""
    assert myfitnesspal.MyFitnessPalConnector.key == "myfitnesspal"
    assert myfitnesspal.MyFitnessPalConnector.measurements[0] is myfitnesspal.BODY_COMPOSITION
    assert myfitnesspal.NUTRITION.tag_keys == ("source", "user", "meal")
    assert myfitnesspal.NUTRITION.field_types == {name: float for name in (
        "energy_kcal", "fat_g", "carbs_g", "protein_g", "saturated_fat_g", "polyunsaturated_fat_g",
        "monounsaturated_fat_g", "trans_fat_g", "cholesterol_mg", "sodium_mg", "potassium_mg",
        "fiber_g", "sugar_g")}
    # exercise is shared with WHOOP's workouts now, declared once in
    # app/schema.py: the script's tags, and its three fields with their types.
    from app import schema
    assert myfitnesspal.EXERCISE is schema.EXERCISE
    assert myfitnesspal.EXERCISE.tag_keys == ("source", "user", "category", "sport")
    assert {name: myfitnesspal.EXERCISE.field_types[name] for name in ("activity", "energy_kcal", "duration_s")} \
        == {"activity": str, "energy_kcal": float, "duration_s": int}
    assert (myfitnesspal.EXERCISE.last_wins, myfitnesspal.EXERCISE.text_max) == (False, 250)
    assert myfitnesspal.MyFitnessPalConnector.rebuild_own_source is True
    assert connector.env_prefix == "TRIBUTARY_MYFITNESSPAL_"


def test_it_takes_an_upload_in_the_ui(connector):
    """It has no API and no schedule: the export comes through the UI's
    *Import file* form (or POST /api/connectors/myfitnesspal/upload)."""
    from app import connectors

    assert connectors.get("myfitnesspal") is not None
    assert connector.is_configured() is False  # nothing to schedule
    assert connector.accepts_files is True and connector.file_types == (".zip",)
    page = TestClient(app).get("/").text
    section, = [part for part in page.split("<section") if 'id="myfitnesspal"' in part]
    assert 'action="/connectors/myfitnesspal/check"' in section


def test_a_watched_folder_is_never_used(connector, tmp_path):
    """Each export covers its own date range: a folder of them is not the
    whole history a watched folder's rebuild would replace the series with."""
    connector.settings.watch_dir = str(tmp_path)
    try:
        assert connector.watch_dir is None and connector.is_configured() is False
    finally:
        connector.settings.watch_dir = None
