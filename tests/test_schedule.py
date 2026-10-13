"""When a connector runs: a 5-field cron in TRIBUTARY_TZ, and where the value
came from (the UI, the environment, the deprecated interval, or the connector's
own default).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic_settings import SettingsConfigDict

from app import schedule
from app.connectors.base import Connector, ConnectorSettings


class FakeSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix="TRIBUTARY_FAKE_", env_file=".env", extra="ignore")


class Fake(Connector):
    """Runs daily at 03:10 unless something says otherwise."""
    key = "fake"
    label = "Fake"
    category = "Productivity"
    default_schedule = "10 3 * * *"
    settings_class = FakeSettings
    required_env = ()

    def api_configured(self):
        return True


def resolve(stored=None, **env):
    """Resolve for one connector with these settings (as the environment would
    have set them)."""
    connector = Fake()
    for name, value in env.items():
        setattr(connector.settings, name, value)
    return schedule.resolve(connector, stored, "Europe/Paris")


# -- Presets and expressions --------------------------------------------------------------

def test_a_preset_becomes_a_cron_expression():
    assert schedule.build_cron("manual") is None
    assert schedule.build_cron("hourly") == "0 * * * *"
    assert schedule.build_cron("every6h") == "0 */6 * * *"
    assert schedule.build_cron("daily", at="03:10") == "10 3 * * *"
    assert schedule.build_cron("daily", at="23:45") == "45 23 * * *"
    assert schedule.build_cron("weekly", at="08:00", weekday="1") == "0 8 * * 1"
    assert schedule.build_cron("custom", custom="  0   5  *  *  * ") == "0 5 * * *"


def test_a_preset_needs_a_time_and_a_day():
    with pytest.raises(schedule.CronError, match="Time must look like"):
        schedule.build_cron("daily", at="noon")
    with pytest.raises(schedule.CronError, match="out of range"):
        schedule.build_cron("daily", at="25:00")
    with pytest.raises(schedule.CronError, match="day of the week"):
        schedule.build_cron("weekly", at="08:00", weekday="")
    with pytest.raises(schedule.CronError, match="Unknown preset"):
        schedule.build_cron("fortnightly")


def test_an_expression_must_be_five_parsable_fields():
    assert schedule.validate("  0   3  *  *  * ") == "0 3 * * *"
    with pytest.raises(schedule.CronError, match="is required"):
        schedule.validate("   ")
    with pytest.raises(schedule.CronError, match="5 fields"):
        schedule.validate("0 3 * *")
    # The seconds field cronsim also takes: a run must land on the minute.
    with pytest.raises(schedule.CronError, match="5 fields"):
        schedule.validate("0 0 3 * * *")
    with pytest.raises(schedule.CronError, match="Bad minute"):
        schedule.validate("60 * * * *")
    with pytest.raises(schedule.CronError, match="Bad hour"):
        schedule.validate("0 25 * * *")


def test_an_expression_is_described_in_words():
    assert schedule.describe("0 * * * *") == "Every hour"
    assert schedule.describe("0 */6 * * *") == "Every 6 hours"
    assert schedule.describe("*/15 * * * *") == "Every 15 minutes"
    assert schedule.describe("10 3 * * *") == "Daily at 03:10"
    assert schedule.describe("10 3 * * 1") == "Weekly on Monday at 03:10"
    # Anything else: cronsim's own English.
    assert schedule.describe("0 0 1 * *") == "At 00:00 on the first day of every month"


def test_the_next_runs_are_in_the_configured_time_zone():
    after = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    runs = schedule.next_runs("30 0 * * *", "Europe/Paris", 3, after=after)
    paris = [moment.astimezone(ZoneInfo("Europe/Paris")) for moment in runs]
    assert [(m.hour, m.minute) for m in paris] == [(0, 30)] * 3
    assert paris[0].date().isoformat() == "2026-10-14"  # 00:30 Paris, after 14:00 Paris
    # Stored times are naive UTC everywhere else.
    assert schedule.to_utc(runs[0]) == runs[0].astimezone(timezone.utc).replace(tzinfo=None)


def test_the_presets_mark_the_one_an_expression_matches():
    chosen = {p["key"] for p in schedule.presets_for("10 3 * * *") if p["chosen"]}
    assert chosen == {"daily"}
    chosen = {p["key"] for p in schedule.presets_for("0 */6 * * *") if p["chosen"]}
    assert chosen == {"every6h"}
    # An expression no preset covers marks Custom, and only Custom.
    chosen = {p["key"] for p in schedule.presets_for("7 4 2 * *") if p["chosen"]}
    assert chosen == {"custom"}
    # Nothing saved: the editor starts on Manual only.
    chosen = {p["key"] for p in schedule.presets_for(None) if p["chosen"]}
    assert chosen == {"manual"}


# -- Where the value comes from -----------------------------------------------------------

def test_a_saved_schedule_wins_over_everything():
    runs = resolve("0 5 * * *", schedule="0 6 * * *", interval_minutes=15)
    assert (runs.cron, runs.source) == ("0 5 * * *", "ui")
    assert runs.source_label == "set here"


def test_manual_only_is_saved_and_beats_the_environment():
    """The user asked for no schedule by name: the environment must not bring one
    back."""
    runs = resolve("", schedule="0 6 * * *")
    assert (runs.cron, runs.scheduled) == (None, False)
    assert (runs.source, runs.label) == ("ui", "Manual only")


def test_the_environment_wins_over_the_connectors_default():
    runs = resolve(None, schedule="0 5 * * *")
    assert (runs.cron, runs.source) == ("0 5 * * *", "env")
    assert runs.next_run is not None


def test_an_unreadable_environment_value_falls_back_and_says_so():
    runs = resolve(None, schedule="not a cron")
    assert (runs.cron, runs.source) == ("10 3 * * *", "default")
    assert "TRIBUTARY_FAKE_SCHEDULE" in runs.note and "not a cron expression" in runs.note


def test_the_deprecated_interval_is_still_honoured():
    """An existing deployment sets only the interval: it keeps working."""
    runs = resolve(None, interval_minutes=15)
    assert (runs.cron, runs.source) == ("*/15 * * * *", "interval")
    assert runs.source_label == "from the deprecated interval"
    runs = resolve(None, interval_minutes=1440)
    assert (runs.cron, runs.source) == ("10 3 * * *", "interval")


def test_an_interval_with_no_cron_equivalent_falls_back_to_the_default():
    """An interval is counted from the start, not a time of day: 90 minutes has
    no cron, and the connector's default applies with a word about it."""
    runs = resolve(None, interval_minutes=90)
    assert (runs.cron, runs.source) == ("10 3 * * *", "default")
    assert "no cron equivalent" in runs.note


def test_the_interval_mapping():
    assert schedule.cron_from_interval(15) == "*/15 * * * *"
    assert schedule.cron_from_interval(60) == "0 * * * *"      # not "every 1 hours"
    assert schedule.cron_from_interval(120) == "0 */2 * * *"
    assert schedule.cron_from_interval(90) is None
    assert schedule.cron_from_interval(1440) is None           # a day: the default's time
    assert schedule.cron_from_interval(0) is None
    assert schedule.cron_from_interval(7 * 60) == "0 */7 * * *"


def test_an_unconfigured_source_has_no_schedule():
    """An upload-only source has nothing to read on its own, whatever the
    environment says."""
    connector = Fake()
    connector.api_configured = lambda: False
    runs = schedule.resolve(connector, "0 5 * * *", "Europe/Paris")
    assert (runs.cron, runs.source, runs.scheduled) == (None, "none", False)


def test_the_connectors_own_default_is_used_when_nothing_else_says():
    runs = resolve()
    assert (runs.cron, runs.source) == ("10 3 * * *", "default")
    assert runs.source_label == "from the connector's default"


def test_a_connector_with_no_default_and_nothing_set_is_never_scheduled():
    class Manual(Fake):
        default_schedule = None

    runs = schedule.resolve(Manual(), None, "Europe/Paris")
    assert (runs.cron, runs.scheduled) == (None, False)


def test_the_clock_and_weekday_of_an_expression_seed_the_editor():
    assert schedule.clock_of("10 3 * * *") == "03:10"
    assert schedule.clock_of("*/15 * * * *") == "03:10"  # not a time of day: the fallback
    assert schedule.clock_of(None, "08:00") == "08:00"
    assert schedule.weekday_of("10 3 * * 1") == "1"
    assert schedule.weekday_of("10 3 * * *") == "1"
