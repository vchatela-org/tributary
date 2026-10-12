"""What leaves Tributary, and what it keeps: numbers and declared tags in
InfluxDB, book or show titles as the only text (and Todoist's task text,
when asked for); no secret, no email, no IP address, no account profile
anywhere -- not in InfluxDB, not in its own database, not in the logs, not
in the API."""
import logging

from fastapi.testclient import TestClient

from app import connectors, runner
from app.database import engine
from app.main import app
from tests.fakes import BOB_RECORDS, RENPHO_SECRETS, SECRETS, TODOIST_SECRETS, TODOIST_TASKS

# (database, measurement, field): the only text fields allowed out. A new
# one is a privacy decision: add it here on purpose, with docs/schema.md.
ALLOWED_TEXT = {("MediaStats", "reading_session", "title"), ("MediaStats", "book_finished", "title")}
# Todoist's task text: only with TRIBUTARY_TODOIST_TASK_TEXT (the job it
# replaced wrote it, and its dashboard lists the completions).
OPT_IN_TEXT = {("TodoistStats", "todoist_task_completed", "content"),
               ("TodoistStats", "todoist_task_completed", "labels"),
               ("TodoistStats", "todoist_completion", "content")}


def test_only_titles_and_opt_in_text_are_declared_as_text():
    declared = {(m.database, m.name, name) for c in connectors.all_connectors() for m in c.measurements
                for name, kind in m.field_types.items() if kind is str}
    assert declared <= ALLOWED_TEXT | OPT_IN_TEXT


def _dump_sqlite() -> str:
    with engine.connect() as conn:
        raw = conn.connection.driver_connection
        return "\n".join(raw.iterdump())


def test_no_token_email_or_ip_is_ever_stored_written_or_logged(abs_api, abs_connector, influx, db, caplog):
    caplog.set_level(logging.DEBUG)
    runner.run(db, abs_connector, dry_run=True)
    runner.run(db, abs_connector)
    abs_api.sessions[0]["updatedAt"] += 1000
    runner.run(db, abs_connector, full=True)

    client = TestClient(app)
    seen = {
        "influxdb": "\n".join(influx.written_lines()) + "\n".join(q for _db, q in influx.queries),
        "sqlite": _dump_sqlite(),
        "logs": caplog.text,
        "api": client.get("/api/connectors").text + client.get("/api/runs").text
        + "".join(client.get(f"/api/runs/{r['id']}").text for r in client.get("/api/runs").json()),
        "pages": client.get("/").text + client.get("/runs").text,
    }
    for where, text in seen.items():
        for secret in SECRETS:
            assert secret not in text, (where, secret)
        # Nor the episode's own title, the author, the bookmark, the user name.
        for private in ("Episode 1", "Frank Herbert", "Key passage", "reader"):
            assert private not in text, (where, private)
    # Only declared text fields reached InfluxDB.
    from tests.fakes import parse
    for line in influx.written_lines():
        measurement, _tags, fields, _ts = parse(line)
        texts = {name for name, value in fields.items() if value.startswith('"')}
        assert {("MediaStats", measurement, name) for name in texts} <= ALLOWED_TEXT


def test_the_api_never_returns_settings(abs_api):
    text = TestClient(app).get("/api/connectors").text
    assert "abs.example" not in text and "the-api-key" not in text


def test_no_renpho_credential_profile_or_device_detail_is_kept(bob, renpho_connector, influx, db, caplog):
    caplog.set_level(logging.DEBUG)
    runner.run(db, renpho_connector, dry_run=True)
    runner.run(db, renpho_connector)
    bob.echo_email = True
    bob.accounts["bob@example.com"] = ("changed", 100002, BOB_RECORDS)  # a refusal quoting the email
    runner.run(db, renpho_connector, full=True)

    client = TestClient(app)
    seen = {
        "influxdb": "\n".join(influx.written_lines()) + "\n".join(q for _db, q in influx.queries),
        "sqlite": _dump_sqlite(),
        "logs": caplog.text,
        "api": client.get("/api/connectors").text + client.get("/api/runs").text
        + "".join(client.get(f"/api/runs/{r['id']}").text for r in client.get("/api/runs").json()),
        "pages": client.get("/").text + client.get("/runs").text,
    }
    assert "Renpho refused the login of bob" in seen["api"]
    for where, text in seen.items():
        # Credentials, the login's token and profile, the person's height,
        # the scale's MAC address.
        for secret in RENPHO_SECRETS + ("AA:BB:CC:DD:EE:FF", "170.5"):
            assert secret not in text, (where, secret)


def _everything_seen(influx) -> dict:
    client = TestClient(app)
    return {
        "influxdb": "\n".join(influx.written_lines()) + "\n".join(q for _db, q in influx.queries),
        "sqlite": _dump_sqlite(),
        "api": client.get("/api/connectors").text + client.get("/api/runs").text
        + "".join(client.get(f"/api/runs/{r['id']}").text for r in client.get("/api/runs").json()),
        "pages": client.get("/").text + client.get("/runs").text,
    }


def test_no_todoist_token_profile_or_task_text_is_kept(todoist_api, todoist_connector, influx, db, caplog,
                                                       monkeypatch):
    """The token and /user's profile (email, name, its own token) never;
    the tasks' content and labels only when asked for, and then in InfluxDB
    and a dry run's preview only; their descriptions never."""
    caplog.set_level(logging.DEBUG)
    runner.run(db, todoist_connector, dry_run=True)
    runner.run(db, todoist_connector)
    runner.run(db, todoist_connector, full=True)
    todoist_api.token = "rotated"
    runner.run(db, todoist_connector)  # refused: the error names no token

    seen = {**_everything_seen(influx), "logs": caplog.text}
    contents = [task["content"] for task in TODOIST_TASKS] + ["Water the plants", "Old chore", "errand"]
    for where, text in seen.items():
        for private in TODOIST_SECRETS + ("a private note",) + tuple(contents):
            assert private not in text, (where, private)

    monkeypatch.setattr(todoist_connector.settings, "task_text", True)
    todoist_api.token = TODOIST_SECRETS[0]
    runner.run(db, todoist_connector, full=True)
    seen = {**_everything_seen(influx), "logs": caplog.text}
    assert "Buy milk, eggs" in seen["influxdb"]
    assert "Buy milk, eggs" not in seen["logs"] + seen["api"] + seen["pages"]
    for where, text in seen.items():
        for private in TODOIST_SECRETS + ("a private note",):
            assert private not in text, (where, private)
