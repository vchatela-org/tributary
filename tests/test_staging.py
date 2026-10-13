"""Check, then import: an uploaded export is kept between the two, and the
import applies exactly what the check showed -- never a diff the user did not
see.

What is pinned here:
  - the file and its row outlive the check, and are gone once imported;
  - a failed check leaves nothing behind (no orphan file, no row);
  - every way the check can go stale is refused, with the stage dropped, rather
    than applied: another file, another setting, another user, another outcome;
  - the TTL, and a file that vanished under the row;
  - the name stored is the masked one the run shows.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import runner, scheduler
from app.config import settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings
from app.database import uploads_dir
from app.points import Measurement, Point

STEPS = Measurement("ActivityStats", "steps", tags=("device",), fields={"steps": int})


class StepsSettings(ConnectorSettings):
    # Stands for the settings an import reads (a names map, a cut-off date).
    names: str = ""


class Steps(Connector):
    """A file connector: `date,steps` lines. `wobbly` makes the same file yield
    other points, as a source that reads InfluxDB while importing can (the
    MyFitnessPal weigh-in dedup does)."""
    key = "steps"
    label = "Steps"
    category = "Nutrition & fitness"
    required_env = ()
    file_types = (".csv",)
    measurements = (STEPS,)
    settings_class = StepsSettings
    rebuild_own_source = True
    wobbly = 0

    def import_fingerprint(self):
        return {"names": self.settings.names}

    def import_file(self, path):
        for line in Path(path).read_text().splitlines():
            if line.strip() == "boom":
                raise ConnectorError("not a steps export")
            day, steps = line.split(",")
            yield Point(STEPS, datetime.fromisoformat(day), {"device": "watch"}, {"steps": int(steps)})
        if self.wobbly:
            yield Point(STEPS, datetime.fromisoformat("2025-01-09"), {"device": "watch"},
                        {"steps": self.wobbly})


@pytest.fixture()
def steps():
    from app import connectors

    connector = connectors.register(Steps())
    yield connector
    connector.wobbly = 0
    connectors.unregister(Steps.key)


def upload(tmp_path, body, name="steps.csv"):
    path = uploads_dir() / name
    path.write_bytes(body)
    return path


def check(db, steps, tmp_path, body=b"2025-01-01,8000\n", name="steps.csv"):
    """A dry run of an uploaded file, as the router queues it."""
    path = upload(tmp_path, body, name)
    return path, runner.run(db, steps, trigger="upload", dry_run=True, file=str(path), filename=name)


# -- The happy path ----------------------------------------------------------------------

def test_a_check_keeps_the_file_and_records_what_it_would_write(db, steps, influx, tmp_path):
    path, run = check(db, steps, tmp_path)
    assert run.status == "ok" and run.points == 1
    assert path.exists()                                    # kept for the import
    assert influx.stored() == []                            # nothing written
    stage = runner.staged_upload(db, steps)
    assert stage is not None and stage.dry_run_id == run.id
    assert Path(stage.path) == path
    assert stage.file_sha256 == runner.file_sha256(path)
    assert stage.points_sha256 == runner.points_sha256(runner.encode(
        list(steps.import_file(path)), steps.source, settings.influxdb_user, settings.tz))


def test_importing_applies_the_check_and_needs_no_second_upload(db, steps, influx, tmp_path):
    path, run = check(db, steps, tmp_path)
    imported = runner.promote(db, steps)
    assert imported.status == "ok" and not imported.dry_run
    assert imported.promoted_from == run.id                 # the two stay told apart
    assert imported.points == 1
    assert [(t, f) for t, f, _ts in influx.stored("steps")] == [
        ({"device": "watch", "source": "steps"}, {"steps": "8000i"})]
    assert not path.exists() and runner.staged_upload(db, steps) is None


def test_a_dry_run_without_a_file_keeps_nothing(db, steps, influx, tmp_path):
    """The "Dry run" button reads its source; there is no file to keep."""
    steps.api_configured = lambda: True
    steps.sync = lambda cursor: __import__("app.connectors.base", fromlist=["SyncResult"]).SyncResult(
        [Point(STEPS, datetime(2025, 1, 1), {"device": "watch"}, {"steps": 5})])
    run = runner.run(db, steps, dry_run=True)
    assert run.status == "ok" and runner.staged_upload(db, steps) is None


# -- Nothing left behind on error --------------------------------------------------------

def test_a_failed_check_leaves_no_file_and_no_row(db, steps, influx, tmp_path):
    path = upload(tmp_path, b"boom\n", "bad.csv")
    run = runner.run(db, steps, trigger="upload", dry_run=True, file=str(path), filename="bad.csv")
    assert run.status == "error" and run.message == "bad.csv: not a steps export"
    assert not path.exists()
    assert runner.staged_upload(db, steps) is None


def test_a_failed_import_keeps_the_check_so_it_can_be_retried(db, steps, influx, tmp_path):
    """InfluxDB down, say: the user should not have to upload the file again."""
    path, run = check(db, steps, tmp_path)
    settings.influxdb_url = ""          # nothing to write to
    failed = runner.promote(db, steps)
    assert failed.status == "error" and "InfluxDB is not configured" in failed.message
    assert path.exists()
    stage = runner.staged_upload(db, steps)
    assert stage is not None and Path(stage.path) == path


# -- Every way a check can go stale ------------------------------------------------------

def test_another_file_is_refused(db, steps, influx, tmp_path):
    path, run = check(db, steps, tmp_path)
    path.write_bytes(b"2025-01-01,9000\n")                  # replaced in the meantime
    with pytest.raises(ConnectorError, match="not the one that was checked"):
        runner.promote(db, steps)
    assert runner.staged_upload(db, steps) is None          # dropped, still importable no more
    assert influx.stored() == []


def test_another_setting_is_refused(db, steps, influx, tmp_path):
    check(db, steps, tmp_path)
    steps.settings.names = '{"a": "b"}'                     # the names map changed
    with pytest.raises(ConnectorError, match="settings changed since the file was checked"):
        runner.promote(db, steps)
    assert runner.staged_upload(db, steps) is None


def test_another_user_is_refused(db, steps, influx, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "influxdb_user", "alice")
    check(db, steps, tmp_path)
    monkeypatch.setattr(settings, "influxdb_user", "bob")   # a span replace would reach them
    with pytest.raises(ConnectorError, match="TRIBUTARY_INFLUXDB_USER changed"):
        runner.promote(db, steps)


def test_another_outcome_is_refused_at_import_time(db, steps, influx, tmp_path):
    """The last gate: the file and the settings are the same, but the points are
    not (a source reading InfluxDB while importing can do this). The run fails
    rather than write a diff nobody reviewed."""
    check(db, steps, tmp_path)
    steps.wobbly = 3                                        # the same file, other points
    run = runner.promote(db, steps)
    assert run.status == "error"
    assert "no longer yields what was checked" in run.message
    assert influx.stored() == []


def test_nothing_checked_is_refused(db, steps, influx):
    with pytest.raises(ConnectorError, match="Nothing was checked yet"):
        runner.promote(db, steps)


def test_a_check_whose_file_vanished_is_refused(db, steps, influx, tmp_path):
    path, run = check(db, steps, tmp_path)
    path.unlink()
    with pytest.raises(ConnectorError, match="checked file is gone"):
        runner.promote(db, steps)
    assert runner.staged_upload(db, steps) is None


def test_an_import_leaves_nothing_to_import_again(db, steps, influx, tmp_path):
    check(db, steps, tmp_path)
    assert runner.promote(db, steps).status == "ok"
    with pytest.raises(ConnectorError, match="Nothing was checked yet"):
        runner.promote(db, steps)
    assert len(influx.stored("steps")) == 1                 # not written twice


# -- Lifetime ----------------------------------------------------------------------------

def test_an_expired_check_is_dropped_with_its_file(db, steps, influx, tmp_path):
    path, run = check(db, steps, tmp_path)
    stage = runner.staged_upload(db, steps)
    stage.created_at = datetime.utcnow() - timedelta(hours=settings.staged_upload_ttl_hours + 1)
    db.commit()
    assert runner.purge_staged(db) == 1
    db.commit()
    assert not path.exists() and runner.staged_upload(db, steps) is None
    with pytest.raises(ConnectorError, match="Nothing was checked yet"):
        runner.promote(db, steps)


def test_an_expired_check_is_refused_even_before_a_sweep(db, steps, influx, tmp_path):
    """The TTL is enforced when the import is asked for, not only when the process
    restarts: a check past its life is never applied."""
    path, _run = check(db, steps, tmp_path)
    stage = runner.staged_upload(db, steps)
    stage.created_at = datetime.utcnow() - timedelta(hours=settings.staged_upload_ttl_hours + 1)
    db.commit()
    with pytest.raises(ConnectorError, match="expired"):
        runner.promote(db, steps)
    assert influx.stored() == [] and runner.staged_upload(db, steps) is None


def test_a_direct_import_leaves_another_waiting_check_alone(db, steps, influx, tmp_path):
    """Importing another file straight away must not consume a check of a
    different file that is still waiting for its import."""
    first = upload(tmp_path, b"2025-01-01,1\n", "a.csv")
    checked = runner.run(db, steps, trigger="upload", dry_run=True, file=str(first), filename="a.csv")
    assert checked.status == "ok" and runner.staged_upload(db, steps) is not None

    other = tmp_path / "b.csv"
    other.write_text("2025-01-02,2\n")
    assert runner.run(db, steps, trigger="upload", file=str(other), filename="b.csv").status == "ok"

    stage = runner.staged_upload(db, steps)
    assert stage is not None and Path(stage.path) == first and first.exists()
    imported = runner.promote(db, steps)                      # the check still works
    assert imported.status == "ok" and imported.promoted_from == checked.id


def test_a_fresh_check_survives_a_purge_and_a_restart(db, steps, influx, tmp_path):
    """The file and its row live on the data volume: a restart does not lose
    them, and does not treat them as expired."""
    path, _run = check(db, steps, tmp_path)
    assert runner.purge_staged(db) == 0
    db.commit()
    assert path.exists() and runner.staged_upload(db, steps) is not None


def test_a_check_whose_file_was_removed_outside_is_purged(db, steps, influx, tmp_path):
    path, _run = check(db, steps, tmp_path)
    path.unlink()
    assert runner.purge_staged(db) == 1
    db.commit()
    assert runner.staged_upload(db, steps) is None


def test_a_second_check_replaces_the_first_file(db, steps, influx, tmp_path):
    first, _a = check(db, steps, tmp_path, name="one.csv")
    second, _b = check(db, steps, tmp_path, name="two.csv")
    assert not first.exists()                               # the old file does not linger
    assert second.exists()
    stage = runner.staged_upload(db, steps)
    assert Path(stage.path) == second and stage.filename == "two.csv"


def test_a_check_may_be_discarded_by_hand(db, steps, influx, tmp_path):
    path, _run = check(db, steps, tmp_path)
    assert runner.discard_staged(db, steps) is True
    db.commit()
    assert not path.exists() and runner.staged_upload(db, steps) is None
    assert runner.discard_staged(db, steps) is False        # nothing there: not an error


# -- What is stored ---------------------------------------------------------------------

def test_the_stored_name_is_the_one_the_run_shows(db, steps, influx, tmp_path):
    """A connector may mask what is personal in a file name: the stored name is
    the masked one, never the account number itself."""
    class Masked(Steps):
        def upload_label(self, filename):
            return filename.replace("12345", "<account>")

    connector = Masked()
    path = upload(tmp_path, b"2025-01-01,8000\n", "steps_12345.csv")
    run = runner.run(db, connector, trigger="upload", dry_run=True, file=str(path),
                     filename="steps_12345.csv")
    assert run.status == "ok"
    stage = runner.staged_upload(db, connector)
    assert stage.filename == "steps_<account>.csv"
    assert "12345" not in stage.filename
    # And the import writes that name in the history, not the raw one.
    imported = runner.promote(db, connector)
    assert "12345" not in imported.message and "<account>" in imported.message


def test_a_span_replace_is_refused_at_the_check_without_a_user(db, steps, influx, tmp_path, monkeypatch):
    """The import's own guard still holds on the promote path -- and the check
    catches it first, so the user is not led on to a button that would fail."""
    from app.connectors.base import FileImport, Span

    class SpanSteps(Steps):
        def import_file(self, path):
            return FileImport(list(super().import_file(path)),
                              {"steps": Span(datetime(2025, 1, 1).date(), datetime(2025, 1, 2).date())})

    connector = SpanSteps()
    monkeypatch.setattr(settings, "influxdb_user", "")
    path = upload(tmp_path, b"2025-01-01,8000\n")
    checked = runner.run(db, connector, trigger="upload", dry_run=True, file=str(path), filename="steps.csv")
    assert checked.status == "error" and "TRIBUTARY_INFLUXDB_USER is not set" in checked.message
    assert runner.staged_upload(db, connector) is None
    # Nothing to import, and nothing dropped: the series is left alone.
    with pytest.raises(ConnectorError, match="Nothing was checked yet"):
        runner.promote(db, connector)
    assert not [q for _db, q in influx.queries if q.startswith(("DROP", "DELETE"))]


def test_the_scheduler_runs_a_promote_with_nothing_uploaded(steps, no_thread):
    """A promote carries no file: the scheduler must not refuse it for a
    connector it would not otherwise schedule."""
    steps.api_configured = lambda: False
    assert scheduler.request("steps", trigger="upload", promoted_from=3) is True
    assert scheduler.request("steps", trigger="upload") is False   # no file, nothing checked
