import json
from datetime import datetime, timezone

import pytest

from app import schedule
from app import runner
from app.connectors.base import ConnectorError
from app.connectors.renpho import (
    BODY_FIELDS,
    BODY_LEVELS,
    BODY_THRESHOLDS,
    LEVELS,
    RenphoSettings,
    numbered_env,
    parse_thresholds,
    scale_range,
    weigh_ins,
)
from app.models import Cursor
from app.points import encode
from tests.fakes import ALICE_RECORDS, BOB_RECORDS, THRESHOLDS, parse, weigh_in

NEWEST = ("body_composition,source=renpho,user=alice,user_id=100001 "
          "bmi=24.1,bmr_kcal=1617.0,body_fat_pct=18.2,visceral_fat=6.0,weight_kg=78.4 1758000000")


def lines_of(result, user=None):
    return encode(result.points, "renpho", user).get("BodyStats", [])


def by_measurement(lines, measurement):
    return [parse(line) for line in lines if parse(line)[0] == measurement]


def set_thresholds(monkeypatch, connector, thresholds):
    monkeypatch.setattr(connector.settings, "thresholds", json.dumps(thresholds))


# -- Mapping ----------------------------------------------------------------------------

def test_weigh_ins_are_written_as_the_ported_job_wrote_them(renpho_cloud, renpho_connector):
    """The exact line: tags (scale left out when the scale has no name),
    float fields only for what was measured (Renpho's 0 = not measured),
    the record's own time, to the second."""
    lines = lines_of(renpho_connector.sync(None))
    body = [line for line in lines if line.startswith("body_composition")]
    assert body[-1] == NEWEST
    _m, tags, fields, ts = parse(body[0])
    assert ts == 1_757_000_000  # from milliseconds
    assert tags == {"source": "renpho", "scale": "ES-CS20M", "user_id": "100001", "user": "alice"}
    assert fields == {"weight_kg": "79.0", "bmi": "24.5", "body_fat_pct": "19.0", "body_water_pct": "55.5",
                      "visceral_fat": "7.0", "bmr_kcal": "1600.0"}
    # The undated record and the one with nothing measured are not weigh-ins.
    assert len(body) == 2


def test_levels_share_the_weigh_in_tags_and_time(renpho_cloud, renpho_connector):
    levels = by_measurement(lines_of(renpho_connector.sync(None)), "body_levels")
    (_m, older_tags, older, older_ts), (_m2, tags, newest, ts) = levels
    assert (tags, ts) == ({"source": "renpho", "user": "alice", "user_id": "100001"}, 1_758_000_000)
    # A value on a boundary gets the upper level (visceral fat 6, BMR 1617).
    assert newest == {"weight_kg": "1.0", "bmi": "1.0", "body_fat_pct": "3.0", "visceral_fat": "1.0",
                      "bmr_kcal": "1.0"}
    # The lowest level is 0, and written; no boundaries for water, no level.
    assert older_tags["scale"] == "ES-CS20M" and older_ts == 1_757_000_000
    assert older["bmr_kcal"] == "0.0" and "body_water_pct" not in older


def test_thresholds_are_tagged_by_user_only_at_the_run_time(renpho_cloud, renpho_connector):
    before = int(datetime.now(timezone.utc).timestamp())
    (_m, tags, fields, ts), = by_measurement(lines_of(renpho_connector.sync(None), user="someone"),
                                             "body_thresholds")
    assert tags == {"user": "alice"}  # no source, and its own user whatever the default
    assert before <= ts <= int(datetime.now(timezone.utc).timestamp())
    assert {k: v for k, v in fields.items() if k.startswith(("weight_kg", "bmr_kcal", "body_fat_pct"))} == {
        "weight_kg_t1": "60.0", "weight_kg_t2": "80.0", "weight_kg_t3": "95.0", "weight_kg_min": "42.5",
        "weight_kg_max": "112.5", "bmr_kcal_t1": "1617.0", "bmr_kcal_min": "1212.75", "bmr_kcal_max": "2021.25",
        "body_fat_pct_t1": "6.0", "body_fat_pct_t2": "13.0", "body_fat_pct_t3": "17.0", "body_fat_pct_t4": "25.0",
        "body_fat_pct_min": "0.0", "body_fat_pct_max": "31.33"}
    assert len(fields) == 24


def test_records_sharing_a_key_are_merged_the_later_winning_never_summed():
    first = weigh_in(1_758_000_000, weight=70.0, bmi=22.0)
    second = weigh_in(1_758_000_000_400, weight=71.0, water=50.0)  # the same second
    other_scale = weigh_in(1_758_000_000, scale="Other", weight=90.0)
    merged, other = weigh_ins([first, second, other_scale])
    assert merged.fields == {"weight_kg": 71.0, "bmi": 22.0, "body_water_pct": 50.0}
    assert other.fields == {"weight_kg": 90.0}


@pytest.mark.parametrize("raw, epoch", [
    (1_758_000_000, 1_758_000_000), (1_758_000_000_999, 1_758_000_000), ("1758000000", 1_758_000_000),
    (None, None), ("", None), ("garbage", None), (0, None), (-5, None), (float("inf"), None),
])
def test_timestamps(raw, epoch):
    result = weigh_ins([weigh_in(1, weight=70.0, timeStamp=raw)])
    assert [w.epoch for w in result] == ([epoch] if epoch else [])


def test_zero_nan_booleans_and_text_are_not_measurements():
    result, = weigh_ins([weigh_in(1_758_000_000, weight="70.5", bmi=float("nan"), water=True, muscle="n/a",
                                  bodyfat=0.0)])
    assert result.fields == {"weight_kg": 70.5}


def test_the_user_id_comes_from_buserid_else_userid():
    record = weigh_in(1_758_000_000, weight=70.0, bUserId=None, userId=42)
    assert weigh_ins([record])[0].tags == {"scale": "ES-CS20M", "user_id": "42"}


def test_every_renpho_metric_is_mapped():
    """A metric renpho-api adds would otherwise be dropped silently."""
    from renpho.constants import METRICS

    assert {key for key, _label, _unit in METRICS} == set(BODY_FIELDS)


def test_declared_fields():
    assert set(BODY_LEVELS.fields) == set(LEVELS)
    assert len(BODY_THRESHOLDS.fields) == sum(levels + 1 for levels in LEVELS.values())
    assert all(kind is float for m in (BODY_LEVELS, BODY_THRESHOLDS) for kind in m.fields.values())


@pytest.mark.parametrize("bounds, expected", [
    ((18.5, 25.0, 30.0), (12.75, 35.75)),
    ((6.0, 13.0, 17.0, 25.0), (0.0, 31.33)),  # never below 0
    ((1617.0,), (1212.75, 2021.25)),
])
def test_scale_range(bounds, expected):
    assert scale_range(bounds) == expected


# -- Boundaries -----------------------------------------------------------------------

def test_thresholds_parse():
    assert parse_thresholds(json.dumps({"a": {"bmr_kcal": [1600]}})) == {"a": {"bmr_kcal": (1600.0,)}}
    assert parse_thresholds("") == {} and parse_thresholds(None) == {}


@pytest.mark.parametrize("raw, message", [
    ("{nope", "not valid JSON"),
    ("[]", "must be a JSON object"),
    ('{"a": [1]}', "must map metrics"),
    ('{"a": {"fat_free_weight_kg": []}}', "not a rated metric"),
    ('{"a": {"bmi": [18.5, 25]}}', "4 levels, so 3 boundaries, not 2"),
    ('{"a": {"bmi": [30, 25, 18.5]}}', "strictly ascending"),
    ('{"a": {"bmi": [18.5, "25", 30]}}', "list of numbers"),
    ('{"a": {"bmi": [18.5, true, 30]}}', "list of numbers"),
])
def test_bad_thresholds_are_explained(raw, message):
    with pytest.raises(ConnectorError, match=message):
        parse_thresholds(raw)


def test_huge_boundaries_are_explained():
    with pytest.raises(ConnectorError, match="list of numbers"):
        parse_thresholds('{"a": {"bmr_kcal": [1' + "0" * 400 + ']}}')


def test_boundaries_for_no_account_fail_the_run(renpho_cloud, renpho_connector, influx, db, monkeypatch):
    """A typo in a user name would otherwise leave the levels frozen, silently."""
    set_thresholds(monkeypatch, renpho_connector, {"Alice": THRESHOLDS["alice"]})
    run = runner.run(db, renpho_connector)
    assert run.status == "error" and run.message == (
        "TRIBUTARY_RENPHO_THRESHOLDS names no account: 'Alice' (the accounts' users: 'alice').")
    assert influx.writes == []


def test_bad_thresholds_fail_the_run_and_write_nothing(renpho_cloud, renpho_connector, influx, db, monkeypatch):
    monkeypatch.setattr(renpho_connector.settings, "thresholds", '{"alice": {"bmi": [1]}}')
    run = runner.run(db, renpho_connector)
    assert run.status == "error" and "TRIBUTARY_RENPHO_THRESHOLDS" in run.message
    assert influx.writes == []


# -- Cursor -----------------------------------------------------------------------------

def test_a_run_writes_only_what_is_new_or_changed(renpho_cloud, renpho_connector, influx, db):
    run = runner.run(db, renpho_connector)
    assert run.status == "ok", run.message
    assert run.message == ("alice: 2 new or changed weigh-in(s), alice: level boundaries: "
                           "5 point(s) written (BodyStats 5).")
    run = runner.run(db, renpho_connector)
    assert (run.status, run.points, run.message) == ("ok", 0, "Nothing new.")

    records = renpho_cloud.accounts["alice@example.com"][2]
    # A new weigh-in, and an old one assigned late in the app.
    records.insert(0, weigh_in(1_759_000_000, scale=None, weight=77.0))
    records.append(weigh_in(1_700_000_000, weight=81.0))
    run = runner.run(db, renpho_connector)
    assert (run.points, run.message) == (4, "alice: 2 new or changed weigh-in(s): 4 point(s) written (BodyStats 4).")
    assert sorted(ts for _t, _f, ts in influx.stored("body_composition")) == [
        1_700_000_000, 1_757_000_000, 1_758_000_000, 1_759_000_000]


def test_new_boundaries_rewrite_the_levels_they_change(renpho_cloud, renpho_connector, influx, db, monkeypatch):
    runner.run(db, renpho_connector)
    set_thresholds(monkeypatch, renpho_connector, {"alice": {**THRESHOLDS["alice"], "bmi": [18.5, 24.0, 30.0]}})
    run = runner.run(db, renpho_connector)
    assert run.message.startswith("alice: 2 new or changed weigh-in(s), alice: level boundaries: 5 point(s)")
    assert {fields["bmi"] for _t, fields, _ts in influx.stored("body_levels")} == {"2.0"}
    # Only the weigh-ins whose level moved.
    set_thresholds(monkeypatch, renpho_connector, {"alice": {**THRESHOLDS["alice"], "bmi": [18.5, 24.3, 30.0]}})
    assert runner.run(db, renpho_connector).message.startswith("alice: 1 new or changed weigh-in(s)")


def test_boundaries_are_rewritten_at_least_weekly_as_the_job_did(renpho_cloud, renpho_connector):
    """A dashboard reading the latest within its time range, or a retention
    policy, must always find them."""
    def thresholds(result):
        return by_measurement(lines_of(result), "body_thresholds")

    first = renpho_connector.sync(None)
    assert thresholds(first) and not thresholds(renpho_connector.sync(first.cursor))
    six_days_later = json.loads(json.dumps(first.cursor))
    six_days_later["thresholds"]["alice"]["written"] -= 6 * 86_400
    assert not thresholds(renpho_connector.sync(six_days_later))
    a_week_later = json.loads(json.dumps(first.cursor))
    a_week_later["thresholds"]["alice"]["written"] -= 7 * 86_400
    result = renpho_connector.sync(a_week_later)
    assert len(thresholds(result)) == 1 and result.summary == ["alice: level boundaries"]
    assert result.cursor["thresholds"]["alice"]["written"] > a_week_later["thresholds"]["alice"]["written"]


def test_a_full_resync_overwrites_instead_of_duplicating(renpho_cloud, renpho_connector, influx, db):
    runner.run(db, renpho_connector)
    before = influx.stored("body_composition") + influx.stored("body_levels")
    run = runner.run(db, renpho_connector, full=True)
    assert run.points == 5
    assert influx.stored("body_composition") + influx.stored("body_levels") == before
    # Stamped with the run time: one more boundaries point, read as the latest.
    assert len(influx.stored("body_thresholds")) in (1, 2)
    assert not [q for _db, q in influx.queries if q.startswith("DROP")]


def test_the_cursor_holds_digests_only(renpho_cloud, renpho_connector, influx, db):
    runner.run(db, renpho_connector)
    cursor = json.loads(db.get(Cursor, "renpho").value_json)["sync"]
    assert set(cursor) == {"weigh_ins", "thresholds"}
    assert len(cursor["weigh_ins"]["alice"]) == 2 and all(len(d) == 16 for d in cursor["weigh_ins"]["alice"])
    assert set(cursor["thresholds"]["alice"]) == {"digest", "written"}
    text = json.dumps(cursor)
    for value in ("78.4", "1758000000", "100001", "ES-CS20M", "alice@example.com"):
        assert value not in text


def test_dry_run_writes_nothing_and_keeps_the_cursor(renpho_cloud, renpho_connector, influx, db):
    run = runner.run(db, renpho_connector, dry_run=True)
    assert run.status == "ok" and run.points == 5
    assert run.preview.startswith("# BodyStats\nbody_composition,scale=ES-CS20M,source=renpho,")
    assert NEWEST in run.preview.splitlines()
    assert influx.writes == [] and db.get(Cursor, "renpho") is None
    assert runner.run(db, renpho_connector).points == 5


# -- Accounts ---------------------------------------------------------------------------

def test_each_account_is_tagged_with_its_user(bob, renpho_connector, influx, db):
    run = runner.run(db, renpho_connector)
    assert run.status == "ok", run.message
    users = {tags["user"]: tags["user_id"] for tags, _f, _ts in influx.stored("body_composition")}
    assert users == {"alice": "100001", "bob": "100002"}
    # Levels and boundaries for alice only: bob has none.
    assert {tags["user"] for tags, _f, _ts in influx.stored("body_levels") + influx.stored("body_thresholds")} == {
        "alice"}


def test_the_first_account_defaults_to_the_influxdb_user(renpho_cloud, renpho_connector, monkeypatch):
    monkeypatch.setattr(renpho_connector.settings, "user", None)
    monkeypatch.setattr("app.config.settings.influxdb_user", "alice")
    assert [a.user for a in renpho_connector.accounts()] == ["alice"]
    # Its boundaries are found under that name too.
    assert by_measurement(lines_of(renpho_connector.sync(None)), "body_thresholds")
    # No name at all: no user tag, and no boundaries to look up.
    monkeypatch.setattr("app.config.settings.influxdb_user", None)
    monkeypatch.setattr(renpho_connector.settings, "thresholds", None)
    lines = lines_of(renpho_connector.sync(None))
    assert all("user" not in parse(line)[1] for line in lines)
    assert not by_measurement(lines, "body_levels")


def test_a_failed_account_does_not_block_the_others(bob, renpho_connector, influx, db):
    bob.accounts["bob@example.com"] = ("changed", 100002, BOB_RECORDS)
    run = runner.run(db, renpho_connector)
    assert run.status == "error"
    assert run.message.endswith("5 point(s) written (BodyStats 5). Failed: Renpho refused the login of bob "
                                "(code 50001: password error).")
    assert {tags["user"] for tags, _f, _ts in influx.stored("body_composition")} == {"alice"}
    # Bob's part of the cursor didn't move: read again, alone, once fixed.
    bob.accounts["bob@example.com"] = ("bob-password", 100002, BOB_RECORDS)
    run = runner.run(db, renpho_connector)
    assert (run.status, run.message) == ("ok", "bob: 1 new or changed weigh-in(s): 1 point(s) written "
                                               "(BodyStats 1).")


def test_every_account_failing_fails_the_run_and_keeps_the_cursor(bob, renpho_connector, influx, db):
    runner.run(db, renpho_connector)
    cursor = db.get(Cursor, "renpho").value_json
    bob.accounts.clear()
    run = runner.run(db, renpho_connector)
    assert run.status == "error" and run.message.count("refused the login") == 2
    db.expire_all()
    assert db.get(Cursor, "renpho").value_json == cursor


def test_a_dry_run_shows_a_failed_account(bob, renpho_connector, db):
    bob.accounts.pop("bob@example.com")
    run = runner.run(db, renpho_connector, dry_run=True)
    assert run.status == "error" and run.points == 5 and "Failed: Renpho refused the login of bob" in run.message


@pytest.mark.parametrize("numbered, message", [
    ({"TRIBUTARY_RENPHO_EMAIL_2": "b@example.com"}, "TRIBUTARY_RENPHO_PASSWORD_2, TRIBUTARY_RENPHO_USER_2 missing"),
    ({"TRIBUTARY_RENPHO_EMAIL_3": "c@example.com", "TRIBUTARY_RENPHO_PASSWORD_3": "s3cret",  # ggignore
      "TRIBUTARY_RENPHO_USER_3": "c"}, "without a gap"),
    ({"TRIBUTARY_RENPHO_EMAIL_2": "b@example.com", "TRIBUTARY_RENPHO_PASSWORD_2": "s3cret",  # ggignore
      "TRIBUTARY_RENPHO_USER_2": "alice"}, "differently"),
])
def test_misconfigured_accounts_are_explained(renpho_cloud, renpho_connector, monkeypatch, numbered, message):
    monkeypatch.setattr(renpho_connector.settings, "_numbered", numbered)
    with pytest.raises(ConnectorError, match=message) as error:
        renpho_connector.accounts()
    assert "@example.com" not in str(error.value) and "s3cret" not in str(error.value)


def test_several_accounts_need_the_first_named(bob, renpho_connector, monkeypatch):
    monkeypatch.setattr(renpho_connector.settings, "user", None)
    with pytest.raises(ConnectorError, match="must name the first account"):
        renpho_connector.accounts()


def test_numbered_accounts_come_from_the_environment_then_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRIBUTARY_RENPHO_EMAIL_2=file@example.com\ntributary_renpho_user_2=from-file\n"
                        "TRIBUTARY_RENPHO_PASSWORD_2=\nTRIBUTARY_RENPHO_EMAIL_3=file@example.com\nOTHER_2=x\n")
    monkeypatch.setenv("TRIBUTARY_RENPHO_EMAIL_2", "env@example.com")
    monkeypatch.setenv("TRIBUTARY_RENPHO_PASSWORD_2", "from-env")  # ggignore
    monkeypatch.setenv("TRIBUTARY_RENPHO_EMAIL_3", "")  # set empty: unsets the file's
    assert numbered_env(str(env_file)) == {"TRIBUTARY_RENPHO_EMAIL_2": "env@example.com",
                                           "TRIBUTARY_RENPHO_PASSWORD_2": "from-env",  # ggignore
                                           "TRIBUTARY_RENPHO_USER_2": "from-file"}


def test_emails_lose_stray_whitespace(bob, renpho_connector, monkeypatch):
    """A newline at the end of a secret would otherwise fail every login."""
    monkeypatch.setattr(renpho_connector.settings, "email", " alice@example.com\n")
    renpho_connector.settings.numbered["TRIBUTARY_RENPHO_EMAIL_2"] = "bob@example.com\r\n"
    result = renpho_connector.sync(None)
    assert not result.errors and {parse(line)[1].get("user") for line in lines_of(result)} == {"alice", "bob"}


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRIBUTARY_RENPHO_EMAIL", "a@example.com")
    monkeypatch.setenv("TRIBUTARY_RENPHO_PASSWORD", "from-env")  # ggignore
    monkeypatch.setenv("TRIBUTARY_RENPHO_PASSWORD_2", "second-from-env")  # ggignore
    loaded = RenphoSettings()
    # interval_minutes is deprecated and left unset: it is distinguishable from
    # "60", so it does not outrank the connector's own default schedule.
    assert (loaded.email, loaded.password.get_secret_value(), loaded.interval_minutes) == (
        "a@example.com", "from-env", None)
    assert loaded.schedule is None
    assert loaded.numbered["TRIBUTARY_RENPHO_PASSWORD_2"] == "second-from-env"
    for text in (repr(loaded), json.dumps(loaded.model_dump(mode="json"))):
        assert "from-env" not in text


def test_configured_only_with_email_and_password(renpho_connector, monkeypatch):
    from pydantic import SecretStr

    assert not renpho_connector.is_configured()
    monkeypatch.setattr(renpho_connector.settings, "email", "a@example.com")
    monkeypatch.setattr(renpho_connector.settings, "password", SecretStr(""))
    assert not renpho_connector.is_configured()
    monkeypatch.setattr(renpho_connector.settings, "password", SecretStr("p"))
    assert renpho_connector.is_configured()
    runs = schedule.resolve(renpho_connector, None, "Europe/Paris")
    assert (runs.cron, runs.label, runs.source) == ("52 2 * * *", "Daily at 02:52", "default")


# -- HTTP (renpho-api against a fake Renpho cloud) ----------------------------------------

def test_every_page_of_every_scale_is_read(renpho_cloud, renpho_connector):
    records = [weigh_in(1_700_000_000 + i * 86_400, weight=70.0 + i / 10) for i in range(120)]
    renpho_cloud.accounts["alice@example.com"] = ("alice-password", 100001, records)
    result = renpho_connector.sync(None)
    assert len(by_measurement(lines_of(result), "body_composition")) == 120
    assert renpho_cloud.calls.count("RenphoHealth/scale/queryBodyCompositionMeasureData") == 3


def test_a_refusal_never_quotes_the_credentials(renpho_cloud, renpho_connector):
    renpho_cloud.echo_email = True
    renpho_cloud.accounts["alice@example.com"] = ("other", 100001, ALICE_RECORDS)
    with pytest.raises(ConnectorError) as error:
        renpho_connector.sync(None)
    assert str(error.value) == "Renpho refused the login of alice (code 50001: Account *** or password error)."


def test_the_client_has_a_timeout_and_its_session_is_closed(renpho_cloud, renpho_connector, monkeypatch):
    """renpho-api sets no timeout of its own: without one, a hung call would
    hold the scheduler (every connector) forever."""
    import requests

    from app.connectors.renpho import TIMEOUT_SECONDS, _Session

    closed, timeouts = [], []
    request = requests.Session.request

    def recording(self, method, url, *args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", recording)
    monkeypatch.setattr(_Session, "close", lambda self: closed.append(self))
    renpho_connector.sync(None)
    assert timeouts and set(timeouts) == {TIMEOUT_SECONDS}
    assert len(closed) == 1


def test_a_hung_call_times_out(renpho_cloud, renpho_connector, monkeypatch):
    monkeypatch.setattr("app.connectors.renpho.TIMEOUT_SECONDS", 0.3)
    renpho_cloud.hang = True
    with pytest.raises(ConnectorError, match=r"did not answer within 0.3 s \(alice\)"):
        renpho_connector.sync(None)


def test_unreachable_and_unexpected_answers_are_errors_not_crashes(renpho_cloud, renpho_connector, monkeypatch):
    monkeypatch.setattr("renpho.client.API_BASE_URL", "http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(ConnectorError, match=r"Renpho unreachable \(alice\): ConnectionError"):
        renpho_connector.sync(None)
    monkeypatch.setattr("renpho.client.API_BASE_URL", renpho_cloud.url + "/nowhere")
    with pytest.raises(ConnectorError, match="Renpho refused the login of alice"):
        renpho_connector.sync(None)
    monkeypatch.setattr(renpho_connector, "_client", lambda account: type(
        "Broken", (), {"get_all_measurements": lambda self: {"not": "a list"}, "_session": None})())
    with pytest.raises(ConnectorError, match=r"Unexpected answer from Renpho \(alice\)"):
        renpho_connector.sync(None)
