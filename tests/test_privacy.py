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
from tests.fakes import (BOB_RECORDS, MEDISAFE_ACCOUNT, MEDISAFE_NOTE, MYFITNESSPAL_NOTE, MYTHERAPY_NOTE, RENPHO_SECRETS, SECRETS,
                         TODOIST_SECRETS, TODOIST_TASKS, mfp_exercise_row, mfp_food_row, mfp_weight_row,
                         medisafe_row, mytherapy_row, write_medisafe_export, write_myfitnesspal_export,
                         write_mytherapy_export)

# (database, measurement, field): the only text fields allowed out. A new
# one is a privacy decision: add it here on purpose, with docs/schema.md.
ALLOWED_TEXT = {("MediaStats", "reading_session", "title"), ("MediaStats", "book_finished", "title"),
                # MyTherapy: the app's own word for a dose and its unit
                # ("confirmed", "pills"): a bounded vocabulary, not free text.
                # The export's `note` column, free text, is left out (the job
                # wrote it; no export ever carried one).
                ("BodyStats", "medication", "app_status"), ("BodyStats", "medication", "unit"),
                # MyFitnessPal: the app's own exercise name ("Course",
                # "Musculation"...), as the job wrote it into the `activity`
                # field. A bounded vocabulary the app draws from, not the
                # export's free-text `note` column (which is left out).
                ("BodyStats", "exercise", "activity"),
                # Medisafe: a medication's strength ("10 mg", "5 g"), as the
                # script wrote it: a number and a unit the app offers.
                ("BodyStats", "medication", "strength"),
                # Sleep as Android: the session's zone (an IANA name), the
                # hypnogram's stage word (deep/light/rem/awake, a bounded
                # vocabulary), and the comment's hashtags -- a vocabulary the
                # user chose and the dashboard filters on (`tags =~ /#alcohol/`).
                ("SleepStats", "sleep_session", "tz"), ("SleepStats", "sleep_session", "tags"),
                ("SleepStats", "sleep_stage", "stage")}
# Todoist's task text: only with TRIBUTARY_TODOIST_TASK_TEXT (the job it
# replaced wrote it, and its dashboard lists the completions). Medisafe's dose
# note: only with TRIBUTARY_MEDISAFE_NOTES (the script wrote it, and the
# Medication dashboard's dose table shows it).
OPT_IN_TEXT = {("TodoistStats", "todoist_task_completed", "content"),
               ("TodoistStats", "todoist_task_completed", "labels"),
               ("TodoistStats", "todoist_completion", "content"),
               ("BodyStats", "medication", "note"),
               # Sleep as Android's comment free text: only with
               # TRIBUTARY_SLEEP_AS_ANDROID_NOTES (the script wrote it).
               ("SleepStats", "sleep_session", "note")}


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


def test_no_mytherapy_alias_or_note_is_kept(mytherapy_connector, influx, db, monkeypatch, caplog, tmp_path):
    """The dose's alias and the export's `note` column never leave; the `med`
    tag holds the name TRIBUTARY_MYTHERAPY_NAMES gives the alias, as the job
    wrote it (and as the dashboard reads it)."""
    from app.config import settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "influxdb_user", "alice")
    export = write_mytherapy_export(tmp_path / "MyTherapy Archive.csv",
                                    [mytherapy_row(name="First", note=MYTHERAPY_NOTE),
                                     mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    runner.run(db, mytherapy_connector, dry_run=True, file=str(export), filename=export.name, trigger="upload")
    export = write_mytherapy_export(tmp_path / "MyTherapy Archive.csv",
                                    [mytherapy_row(name="First", note=MYTHERAPY_NOTE),
                                     mytherapy_row(name="Second", scheduled="2026-03-02 20:00:00")])
    runner.run(db, mytherapy_connector, file=str(export), filename=export.name, trigger="upload")

    seen = {**_everything_seen(influx), "logs": caplog.text}
    for where, text in seen.items():
        for private in ("First", "Second", MYTHERAPY_NOTE, "Actual", "scheduled_date"):
            assert private not in text, (where, private)
    assert "med=Alpha\\ med" in seen["influxdb"] and "med=Beta\\ med" in seen["influxdb"]


def test_no_myfitnesspal_member_name_or_note_is_kept(myfitnesspal_connector, influx, db, monkeypatch, caplog,
                                                     tmp_path):
    """The export's free-text `note` column and its member names never leave;
    the `activity` field holds the app's own exercise name, as the job wrote it
    (and as the dashboard reads it)."""
    from app.config import settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "influxdb_user", "alice")

    def export():
        return write_myfitnesspal_export(
            tmp_path / "export.zip", span=("2026-03-01", "2026-03-31"),
            weight=[mfp_weight_row()],
            food=[mfp_food_row(**{"Calories": "600", "export_headers.note": MYFITNESSPAL_NOTE})],
            exercise=[mfp_exercise_row(activity="Course")])

    path = export()
    runner.run(db, myfitnesspal_connector, dry_run=True, file=str(path), filename=path.name, trigger="upload")
    path = export()
    runner.run(db, myfitnesspal_connector, file=str(path), filename=path.name, trigger="upload")

    seen = {**_everything_seen(influx), "logs": caplog.text}
    for where, text in seen.items():
        assert MYFITNESSPAL_NOTE not in text, where
        assert "Récapitulatif" not in text, where
    assert 'activity="Course"' in seen["influxdb"]


def test_no_medisafe_note_or_account_is_kept(medisafe_connector, influx, db, monkeypatch, caplog, tmp_path):
    """A dose's note (free text) never leaves unless TRIBUTARY_MEDISAFE_NOTES
    asks for it; the account number in the export's name is masked
    everywhere: InfluxDB, the stored runs, the API, the pages, the logs."""
    from app.config import settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "influxdb_user", "alice")
    name = f"Doses_{MEDISAFE_ACCOUNT}_000.csv"
    for dry_run in (True, False):
        path = write_medisafe_export(tmp_path / name, [medisafe_row(strength="10 mg", notes=MEDISAFE_NOTE)])
        runner.run(db, medisafe_connector, dry_run=dry_run, file=str(path), filename=name, trigger="upload")

    seen = {**_everything_seen(influx), "logs": caplog.text}
    for where, text in seen.items():
        assert MEDISAFE_NOTE not in text, where
        assert MEDISAFE_ACCOUNT not in text, where
    assert "Doses_&lt;account&gt;_000.csv" in seen["pages"] or "Doses_<account>_000.csv" in seen["api"]
    assert 'strength="10 mg"' in seen["influxdb"]


def test_no_whoop_token_secret_profile_or_note_is_kept(whoop_api, whoop_connector, influx, db, monkeypatch,
                                                         caplog, tmp_path):
    """The app's secret, every token WHOOP issued and the authorization codes
    never leave: not in InfluxDB, not in plain text in SQLite (the grant is
    sealed), not in the logs, the API or the pages. Nor would a profile's
    email if WHOOP ever put one in a record, nor a journal entry's note. The
    journal's questions are tags, custom ones included."""
    from urllib.parse import parse_qs, urlparse

    from app import oauth
    from app.config import settings
    from app.models import OAuthToken, utcnow
    from tests.fakes import WHOOP_CLIENT_SECRET, WHOOP_EMAIL, WHOOP_NOTE, write_whoop_export

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(settings, "influxdb_user", "alice")
    whoop_api.records["sleep"][0]["email"] = WHOOP_EMAIL
    client = TestClient(app)
    sent = client.post("/connectors/whoop/connect", headers={"sec-fetch-site": "same-origin"},
                       follow_redirects=False)
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    code = whoop_api.new_code()
    client.get("/connectors/whoop/callback", params={"code": code, "state": state},
               headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
    runner.run(db, whoop_connector, dry_run=True)
    runner.run(db, whoop_connector)
    db.query(OAuthToken).update({OAuthToken.expires_at: utcnow()})  # the next run refreshes
    db.commit()
    runner.run(db, whoop_connector, full=True)
    for dry_run in (True, False):
        path = write_whoop_export(tmp_path / "my_whoop_data.zip")
        runner.run(db, whoop_connector, dry_run=dry_run, file=str(path), filename=path.name, trigger="upload")
    assert whoop_api.events.count("token:refresh_token") == 1 and len(whoop_api.issued) == 4

    # Every log but the test client's own request lines (it stands for the
    # browser, which does hold the callback's address).
    logs = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records if not r.name.startswith("httpx"))
    seen = {**_everything_seen(influx), "logs": logs,
            "whoop page": client.get("/connectors/whoop").text + client.get("/settings").text}
    assert 'question=Read\\ in\\ bed\\ before\\ sleeping?' in seen["influxdb"]
    assert oauth.load("whoop", "Whoop") is not None
    for where, text in seen.items():
        for secret in (*whoop_api.issued, WHOOP_CLIENT_SECRET, code, state, WHOOP_EMAIL, WHOOP_NOTE, "glasses"):
            assert secret not in text, (where, secret)

    client.post("/connectors/whoop/disconnect", headers={"sec-fetch-site": "same-origin"})
    assert oauth.load("whoop", "Whoop") is None
    for secret in whoop_api.issued:
        assert secret not in _dump_sqlite()


def test_a_callback_s_query_is_redacted_in_the_access_log():
    from app.routers.connections import RedactCallbackQuery

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.20:5000", "GET", "/connectors/whoop/callback?code=c0de&state=st4te",
                                "1.1", 303), None)
    assert RedactCallbackQuery().filter(record)
    assert record.getMessage() == '203.0.113.20:5000 - "GET /connectors/whoop/callback?<redacted> HTTP/1.1" 303'
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '"%s %s"', ("GET", "/runs?page=2"), None)
    RedactCallbackQuery().filter(other)
    assert other.getMessage() == '"GET /runs?page=2"'
