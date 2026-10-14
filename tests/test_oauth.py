"""OAuth in the core (app/oauth.py, app/routers/connections.py) and the box
its tokens are kept in (app/secretbox.py), against a fake WHOOP: the state,
the callback, the rotation of refresh tokens, a 401, `invalid_grant`, the
lock, Disconnect -- and what a missing, wrong or rotated key does."""
import base64
import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import oauth, runner, secretbox
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import OAuthState, OAuthToken, utcnow
from tests.conftest import new_secret_key
from tests.fakes import WHOOP_CLIENT_ID, WHOOP_REDIRECT, WHOOP_TOKEN_PREFIX

SAME = {"sec-fetch-site": "same-origin"}
CROSS = {"sec-fetch-site": "cross-site"}


@pytest.fixture()
def client():
    return TestClient(app)


def stored_row():
    with SessionLocal() as db:
        row = db.get(OAuthToken, "whoop")
        if row is not None:
            db.expunge(row)
        return row


def stored_grant():
    return oauth.load("whoop", "Whoop")


def expire_now():
    """The stored access token is about to expire: the next use refreshes."""
    with SessionLocal() as db:
        row = db.get(OAuthToken, "whoop")
        row.expires_at = utcnow() + timedelta(seconds=30)
        db.commit()


def connect(client, fake):
    """Connect, as the browser does: the page's button, WHOOP's page (the fake
    issues a code), back to the callback with WHOOP's cross-site header."""
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    assert sent.status_code == 303, sent.text
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    back = client.get("/connectors/whoop/callback", params={"code": fake.new_code(), "state": state},
                      headers=CROSS, follow_redirects=False)
    return state, back


# -- The secret box -----------------------------------------------------------------------

def test_a_sealed_value_opens_with_its_key_and_context_only(secret_key):
    sealed = secretbox.seal(b"a refresh token", "whoop")
    assert secretbox.open_(sealed, "whoop") == (b"a refresh token", False)
    raw = base64.urlsafe_b64decode(sealed)
    assert raw[0] == secretbox.VERSION and b"a refresh token" not in raw
    # A random nonce per write: the same value never seals the same way twice.
    assert secretbox.seal(b"a refresh token", "whoop") != sealed


def test_a_wrong_key_is_refused(secret_key, monkeypatch):
    sealed = secretbox.seal(b"secret", "whoop")
    monkeypatch.setattr(settings, "secret_key", SecretStr(new_secret_key()))
    with pytest.raises(secretbox.Unreadable):
        secretbox.open_(sealed, "whoop")
    assert not secretbox.readable(sealed, "whoop")


def test_a_row_moved_to_another_connector_does_not_open(secret_key):
    """The connector's key is the associated data: a sealed grant copied onto
    another connector's row is refused."""
    sealed = secretbox.seal(b"secret", "whoop")
    with pytest.raises(secretbox.Unreadable):
        secretbox.open_(sealed, "another")
    tampered = bytearray(base64.urlsafe_b64decode(sealed))
    tampered[-1] ^= 1
    with pytest.raises(secretbox.Unreadable):
        secretbox.open_(base64.urlsafe_b64encode(bytes(tampered)).decode(), "whoop")
    with pytest.raises(secretbox.Unreadable):
        secretbox.open_("not sealed at all", "whoop")


def test_rotation_opens_with_the_previous_key_and_seals_again_with_the_new(whoop_connected, monkeypatch):
    old = settings.secret_key
    before = stored_row().ciphertext
    monkeypatch.setattr(settings, "secret_key", SecretStr(new_secret_key()))
    monkeypatch.setattr(settings, "secret_key_previous", old)
    assert secretbox.open_(before, "whoop")[1] is True
    grant = stored_grant()
    assert grant.access_token.startswith(WHOOP_TOKEN_PREFIX)
    after = stored_row().ciphertext
    assert after != before and secretbox.open_(after, "whoop")[1] is False
    # Once sealed again, the previous key can go.
    monkeypatch.setattr(settings, "secret_key_previous", None)
    assert stored_grant().refresh_token == grant.refresh_token


def test_the_key_comes_from_a_file_and_must_be_32_bytes_of_base64(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "secret_key", None)
    monkeypatch.setattr(settings, "secret_key_previous", None)
    monkeypatch.setattr(settings, "secret_key_file", None)
    assert secretbox.problem() == ("TRIBUTARY_SECRET_KEY is not set: it must be the base64 of 32 random bytes: "
                                   "openssl rand -base64 32.")
    with pytest.raises(secretbox.SecretKeyError):
        secretbox.seal(b"x", "whoop")
    key_file = tmp_path / "key"
    key_file.write_text(new_secret_key() + "\n")
    monkeypatch.setattr(settings, "secret_key_file", str(key_file))
    assert secretbox.problem() is None
    assert secretbox.open_(secretbox.seal(b"x", "whoop"), "whoop")[0] == b"x"
    monkeypatch.setattr(settings, "secret_key", SecretStr(new_secret_key()))
    assert "Both TRIBUTARY_SECRET_KEY and TRIBUTARY_SECRET_KEY_FILE" in secretbox.problem()
    monkeypatch.setattr(settings, "secret_key_file", None)
    for bad, said in (("not base64 at all!", "is not base64"),
                      (base64.b64encode(b"short").decode(), "holds 5 bytes, not 32")):
        monkeypatch.setattr(settings, "secret_key", SecretStr(bad))
        assert said in secretbox.problem()
        assert bad not in secretbox.problem()
    # openssl's alphabet, or the URL-safe one, padded or not.
    key = base64.urlsafe_b64encode(bytes(range(200, 232))).decode().rstrip("=")
    monkeypatch.setattr(settings, "secret_key", SecretStr(key))
    assert secretbox.problem() is None


def test_no_key_means_no_connect_never_plain_text(client, whoop_api, monkeypatch, db):
    monkeypatch.setattr(settings, "secret_key", None)
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"].startswith("/connectors/whoop?notice=")
    assert "TRIBUTARY_SECRET_KEY+is+not+set" in sent.headers["location"]
    assert db.query(OAuthState).count() == 0 and stored_row() is None
    page = client.get("/connectors/whoop").text
    assert "TRIBUTARY_SECRET_KEY is not set" in page and 'action="/connectors/whoop/connect"' not in page
    # Nor at the callback, should the key vanish in between.
    monkeypatch.setattr(settings, "secret_key", SecretStr(new_secret_key()))
    state = parse_qs(urlparse(oauth.start(connectors_whoop())).query)["state"][0]
    monkeypatch.setattr(settings, "secret_key", None)
    back = client.get("/connectors/whoop/callback", params={"code": whoop_api.new_code(), "state": state},
                      headers=CROSS, follow_redirects=False)
    assert "TRIBUTARY_SECRET_KEY" in back.headers["location"] and stored_row() is None
    assert "token:authorization_code" not in whoop_api.events


def test_a_lost_key_means_reconnect_nothing_worse(whoop_connected, whoop_connector, monkeypatch, db, client):
    monkeypatch.setattr(settings, "secret_key", SecretStr(new_secret_key()))
    record = runner.run(db, whoop_connector, dry_run=True)
    assert record.status == "error"
    assert "can't be decrypted with the secret key set now" in record.message
    assert "reconnect Whoop on its page" in record.message
    assert "the secret key set now can't open it" in client.get("/connectors/whoop").text
    # Reconnecting replaces it.
    _state, back = connect(client, whoop_connected)
    assert back.headers["location"] == "/connectors/whoop#connection"
    assert runner.run(db, whoop_connector, dry_run=True).status == "ok"


# -- Connecting -----------------------------------------------------------------------------

def test_connect_sends_the_browser_to_whoop_with_a_new_state(client, whoop_api, db):
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    target = urlparse(sent.headers["location"])
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert f"{target.scheme}://{target.netloc}/" == whoop_api.url and target.path == "/oauth/oauth2/auth"
    assert {k: v for k, v in query.items() if k != "state"} == {
        "response_type": "code", "client_id": WHOOP_CLIENT_ID, "redirect_uri": WHOOP_REDIRECT,
        "scope": "offline read:recovery read:cycles read:sleep read:workout"}
    assert len(query["state"]) == 16 and query["state"].isalnum()
    # Only its hash is kept.
    rows = db.query(OAuthState).all()
    assert len(rows) == 1 and rows[0].state_sha256 != query["state"] and rows[0].connector == "whoop"


def test_connect_refuses_a_cross_site_request(client, whoop_api, db):
    for site in ("cross-site", "same-site"):
        assert client.post("/connectors/whoop/connect", headers={"sec-fetch-site": site},
                           follow_redirects=False).status_code == 403
    assert client.post("/connectors/whoop/connect", headers={"origin": "https://evil.example",
                                                              "host": "tributary.example"},
                       follow_redirects=False).status_code == 403
    assert db.query(OAuthState).count() == 0


def test_the_callback_works_from_whoop_s_site_and_stores_the_grant_sealed(client, whoop_api, whoop_connector):
    _state, back = connect(client, whoop_api)
    assert back.status_code == 303 and back.headers["location"] == "/connectors/whoop#connection"
    row = stored_row()
    assert row is not None and row.scope == "offline read:recovery read:cycles read:sleep read:workout"
    assert all(token not in row.ciphertext for token in whoop_api.issued)
    assert stored_grant().access_token == whoop_api.issued[0]
    assert whoop_connector.api_configured()
    page = client.get("/connectors/whoop").text
    assert "Connected." in page and "Disconnect" in page and "Reconnect" in page
    assert not any(token in page for token in whoop_api.issued)


def test_a_state_is_single_use(client, whoop_api):
    state, _back = connect(client, whoop_api)
    again = client.get("/connectors/whoop/callback", params={"code": whoop_api.new_code(), "state": state},
                       headers=CROSS, follow_redirects=False)
    assert "already+used" in again.headers["location"]
    assert whoop_api.events.count("token:authorization_code") == 1


def test_an_unknown_or_expired_state_is_refused(client, whoop_api, db):
    unknown = client.get("/connectors/whoop/callback", params={"code": whoop_api.new_code(), "state": "x" * 16},
                         headers=CROSS, follow_redirects=False)
    assert "unknown" in unknown.headers["location"]
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    db.query(OAuthState).update({OAuthState.created_at: utcnow() - timedelta(minutes=11)})
    db.commit()
    late = client.get("/connectors/whoop/callback", params={"code": whoop_api.new_code(), "state": state},
                      headers=CROSS, follow_redirects=False)
    assert "older+than+10+minutes" in late.headers["location"]
    assert "token:authorization_code" not in whoop_api.events and stored_row() is None
    # No state, no code: nothing either.
    assert "unknown" in client.get("/connectors/whoop/callback", headers=CROSS,
                                   follow_redirects=False).headers["location"]


def test_a_state_of_another_connector_is_refused(whoop_api):
    with SessionLocal() as db:
        db.add(OAuthState(state_sha256=oauth._digest("s" * 16), connector="another", created_at=utcnow()))
        db.commit()
    assert oauth.consume_state("whoop", "s" * 16) is False


def test_access_denied_on_whoop_s_page_spends_the_state(client, whoop_api):
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    back = client.get("/connectors/whoop/callback",
                      params={"error": "access_denied", "error_description": "<script>x</script>", "state": state},
                      headers=CROSS, follow_redirects=False)
    assert "Access+was+not+granted" in back.headers["location"] and "script" not in back.headers["location"]
    assert oauth.consume_state("whoop", state) is False


def test_a_refused_code_says_so(client, whoop_api):
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    back = client.get("/connectors/whoop/callback", params={"code": "a-code-whoop-never-issued", "state": state},
                      headers=CROSS, follow_redirects=False)
    assert "refused+the+authorization+code" in back.headers["location"] and stored_row() is None


def test_the_pasted_address_finishes_connecting(client, whoop_api):
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    state = parse_qs(urlparse(sent.headers["location"]).query)["state"][0]
    landed = f"https://localhost/connectors/whoop/callback?code={whoop_api.new_code()}&state={state}"
    assert client.post("/connectors/whoop/callback", data={"url": landed}, headers=CROSS,
                       follow_redirects=False).status_code == 403
    nothing = client.post("/connectors/whoop/callback", data={"url": "https://localhost/"}, headers=SAME,
                          follow_redirects=False)
    assert "holds+no+authorization" in nothing.headers["location"]
    back = client.post("/connectors/whoop/callback", data={"url": landed}, headers=SAME, follow_redirects=False)
    assert back.headers["location"] == "/connectors/whoop#connection" and stored_row() is not None


def test_only_an_oauth_source_has_these_routes(client):
    assert client.post("/connectors/todoist/connect", headers=SAME).status_code == 404
    assert client.get("/connectors/todoist/callback", headers=CROSS).status_code == 404
    assert client.get("/connectors/nope/callback", headers=CROSS).status_code == 404


def test_connect_names_what_the_app_lacks(client, whoop_api, monkeypatch, whoop_connector):
    monkeypatch.setattr(whoop_connector.settings, "client_id", None)
    sent = client.post("/connectors/whoop/connect", headers=SAME, follow_redirects=False)
    assert "Set+TRIBUTARY_WHOOP_CLIENT_ID" in sent.headers["location"]
    page = client.get("/").text
    assert "Set TRIBUTARY_WHOOP_CLIENT_ID (your WHOOP app&#39;s) to connect Whoop." in page


# -- Using the grant ----------------------------------------------------------------------

def test_a_rotated_pair_is_stored_before_the_first_data_call_even_in_a_dry_run(
        whoop_connected, whoop_connector, db):
    old = stored_grant()
    expire_now()
    seen = {}
    whoop_connected.on_data = lambda: seen.update(grant=stored_grant())
    record = runner.run(db, whoop_connector, dry_run=True)
    assert record.status == "ok", record.message
    assert whoop_connected.events[0] == "token:refresh_token"
    # Stored by the time WHOOP answered the first data request.
    assert seen["grant"].refresh_token != old.refresh_token
    assert seen["grant"].refresh_token in whoop_connected.refresh_tokens
    assert old.refresh_token not in whoop_connected.refresh_tokens  # dead, as WHOOP kills it
    assert stored_grant().refresh_token == seen["grant"].refresh_token


def test_a_rotated_pair_survives_a_run_that_fails_afterwards(whoop_connected, whoop_connector, db, influx):
    expire_now()
    whoop_connected.fail["/developer/v2/cycle"] = [(500, {}, {})] * 4
    record = runner.run(db, whoop_connector)
    assert record.status == "error" and "WHOOP answered 500 on /developer/v2/cycle" in record.message
    assert stored_grant().refresh_token in whoop_connected.refresh_tokens
    # So the next run still works.
    assert runner.run(db, whoop_connector).status == "ok"


def test_a_401_refreshes_once_and_retries(whoop_connected, whoop_connector):
    whoop_connected.access_tokens.clear()  # WHOOP invalidated the token early
    whoop_connector.sync(None)
    assert whoop_connected.events.count("token:refresh_token") == 1
    assert whoop_connected.events[:3] == ["GET /developer/v2/activity/sleep", "token:refresh_token",
                                          "GET /developer/v2/activity/sleep"]


def test_a_second_401_fails_after_one_refresh(whoop_connected, whoop_connector, monkeypatch):
    monkeypatch.setattr(whoop_connected, "_authorized", lambda headers: False)
    with pytest.raises(oauth.ConnectorError, match="refused the access token .* even refreshed"):
        whoop_connector.sync(None)
    assert whoop_connected.events.count("token:refresh_token") == 1


def test_invalid_grant_asks_to_reconnect(whoop_connected, whoop_connector, db, influx, client):
    expire_now()
    whoop_connected.refresh_tokens.clear()  # revoked in the WHOOP app, say
    record = runner.run(db, whoop_connector)
    assert record.status == "error"
    assert record.message == "Whoop refused the stored connection (invalid_grant): Reconnect Whoop on its page."
    assert stored_row() is None and not whoop_connector.api_configured()
    assert "Not connected: connect Whoop on its page." in client.get("/").text


def test_refused_app_credentials_name_the_variables(whoop_connected, whoop_connector, monkeypatch, db):
    expire_now()
    monkeypatch.setattr(whoop_connector.settings, "client_secret", SecretStr("another-secret"))
    record = runner.run(db, whoop_connector, dry_run=True)
    assert "invalid_client" in record.message and "TRIBUTARY_WHOOP_CLIENT_SECRET" in record.message
    assert "another-secret" not in record.message
    assert stored_row() is not None  # the grant itself is fine


def test_the_lock_keeps_two_refreshes_from_killing_each_other(whoop_connected, whoop_connector):
    """Two refreshes of one rotating token at once: without the lock, the
    second would send the token the first just killed."""
    errors = []

    def refresh():
        try:
            oauth.access_token(whoop_connector, force_refresh=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and whoop_connected.events.count("token:refresh_token") == 4
    assert stored_grant().refresh_token in whoop_connected.refresh_tokens


def test_a_run_reads_the_token_once_and_keeps_the_scope_across_refreshes(whoop_connected, whoop_connector,
                                                                         monkeypatch):
    calls = []
    real = oauth.access_token
    monkeypatch.setattr(oauth, "access_token", lambda c, force_refresh=False: calls.append(force_refresh)
                        or real(c, force_refresh))
    whoop_connected.page_size = 1  # a dozen requests
    whoop_connector.sync(None)
    assert calls == [False] and len(whoop_connected.data_paths()) > 10
    # A refresh answer without a scope keeps the scope granted.
    monkeypatch.setattr(whoop_connected, "grant", lambda: {
        **type(whoop_connected).grant(whoop_connected), "scope": None})
    real(whoop_connector, force_refresh=True)
    assert stored_row().scope == "offline read:recovery read:cycles read:sleep read:workout"


def test_a_token_with_no_lifetime_is_refreshed_only_when_refused(whoop_connected, whoop_connector):
    with SessionLocal() as db:
        db.get(OAuthToken, "whoop").expires_at = None
        db.commit()
    for _ in range(3):
        oauth.access_token(whoop_connector)
    assert "token:refresh_token" not in whoop_connected.events
    whoop_connected.access_tokens.clear()
    whoop_connector.sync(None)
    assert whoop_connected.events.count("token:refresh_token") == 1


def test_a_bare_401_from_the_token_endpoint_is_not_called_bad_credentials(whoop_connected, whoop_connector):
    expire_now()
    whoop_connected.fail["/oauth/oauth2/token"] = [(401, {}, "not json")]
    with pytest.raises(oauth.ConnectorError) as error:
        oauth.access_token(whoop_connector)
    assert "refused the request (401)" in str(error.value) and "invalid_client" not in str(error.value)
    assert stored_row() is not None


def test_disconnect_revokes_then_forgets(client, whoop_connected, whoop_connector):
    assert client.post("/connectors/whoop/disconnect", headers=CROSS).status_code == 403
    back = client.post("/connectors/whoop/disconnect", headers=SAME, follow_redirects=False)
    assert back.headers["location"] == "/connectors/whoop#connection"
    assert "DELETE /developer/v2/user/access" in whoop_connected.events and whoop_connected.revoked
    assert stored_row() is None and not whoop_connector.api_configured()
    assert "Not connected." in client.get("/connectors/whoop").text


def test_disconnect_forgets_even_when_whoop_cannot_be_told(client, whoop_connected):
    whoop_connected.access_tokens.clear()
    back = client.post("/connectors/whoop/disconnect", headers=SAME, follow_redirects=False)
    assert "could+not+be+told" in back.headers["location"] and stored_row() is None


def test_a_grant_that_expires_is_refreshed_before_the_revocation(client, whoop_connected):
    expire_now()
    client.post("/connectors/whoop/disconnect", headers=SAME, follow_redirects=False)
    assert whoop_connected.events == ["token:refresh_token", "DELETE /developer/v2/user/access"]
    assert whoop_connected.revoked


def connectors_whoop():
    from app import connectors

    return connectors.get("whoop")
