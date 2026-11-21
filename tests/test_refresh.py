"""Refresh now (app/refresh.py): a compatible tool's refresh hook, the chain
hooks -> the Tributary connectors it reads -> the connector, and its page."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import connectors, refresh, scheduler
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import SyncRun, utcnow

TOKEN = "invented-refresh-token"  # ggignore
BODY = b'{"status": "ok", "records": 12, "heart_rate": 61}'


class FakeTool:
    """A compatible tool's refresh hook, served in-process: it records each
    request and answers what the test sets."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.location = None
        self.delay = threading.Event()
        self.hold = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": self.rfile.read(length) if length else b""})
                if fake.hold:
                    fake.delay.wait(5)
                self.send_response(fake.status)
                if fake.location:
                    self.send_header("Location", fake.location)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)

            def do_GET(self):  # noqa: N802 -- where a redirect would land
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": b""})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/refresh"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.delay.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def tool():
    fake = FakeTool()
    yield fake
    fake.close()


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def model(monkeypatch):
    """The WHOOP models connector, enabled, reading WHOOP and a Fitbit."""
    connector = connectors.get("whoop_model")
    monkeypatch.setattr(connector.settings, "enabled", True)
    monkeypatch.setattr(connector.settings, "inputs", "whoop,fitbit")
    return connector


# -- The hook -----------------------------------------------------------------------------

def test_a_hook_is_a_post_with_no_body_and_the_bearer_token(tool):
    ok, message = refresh.call_hook(tool.url, TOKEN, timeout=5)
    assert ok and message.startswith("fetched in"), message
    (request,) = tool.requests
    assert request["path"] == "/refresh" and request["body"] == b""
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["User-Agent"].startswith("Tributary/")


def test_without_a_token_no_authorization_is_sent(tool):
    assert refresh.call_hook(tool.url, None, timeout=5)[0]
    assert "Authorization" not in tool.requests[0]["headers"]


def test_an_answer_outside_2xx_is_a_failed_hook(tool):
    tool.status = 503
    assert refresh.call_hook(tool.url, TOKEN, timeout=5) == (False, "answered 503")


def test_a_redirect_is_never_followed(tool):
    """It would take the token along, to wherever it points."""
    tool.status, tool.location = 302, tool.url.replace("/refresh", "/elsewhere")
    ok, message = refresh.call_hook(tool.url, TOKEN, timeout=5)
    assert not ok and "redirect" in message
    assert [r["path"] for r in tool.requests] == ["/refresh"]


def test_a_hook_that_does_not_answer_in_time_fails(tool):
    tool.hold = True
    ok, message = refresh.call_hook(tool.url, TOKEN, timeout=0.3)
    assert not ok and message == "did not answer within 0.3 s"


def test_an_unreachable_hook_fails():
    assert refresh.call_hook("http://127.0.0.1:9/refresh", None, timeout=2) == (False, "could not be reached")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "", "refresh", "http://"])
def test_only_an_http_address_is_called(url):
    assert refresh.call_hook(url, TOKEN) == (False, "its refresh address is not an http(s) URL")


def test_nothing_of_the_hook_is_logged(tool, monkeypatch, caplog):
    """The address, the token and the answer stay out of the log and the steps."""
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_url", tool.url)
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_token", SecretStr(TOKEN))
    step = refresh.Step("hook", "fitbit-grafana", "Ask fitbit-grafana to fetch now")
    with caplog.at_level(logging.DEBUG):
        refresh._hook(step)
    assert step.state == "done"
    shown = caplog.text + step.message + str(step.as_dict())
    for secret in (TOKEN, tool.url, "127.0.0.1", "heart_rate", "records"):
        assert secret not in shown, secret


# -- What a refresh does --------------------------------------------------------------------

def test_the_models_read_whoop_and_the_tools_of_their_inputs(model, monkeypatch):
    assert model.refresh_inputs() == (("whoop",), ("fitbit-grafana",))
    monkeypatch.setattr(model.settings, "inputs", "fitbit,garmin")
    assert model.refresh_inputs() == ((), ("fitbit-grafana", "garmin-grafana"))
    monkeypatch.setattr(model.settings, "inputs", "fitbit,nope")
    assert model.refresh_inputs() == ((), ())  # the run reports the bad list


def test_a_connector_of_its_own_source_has_nothing_to_refresh():
    assert not refresh.refreshable(connectors.get("audiobookshelf"))
    assert refresh.refreshable(connectors.get("whoop_model"))


def test_the_plan_asks_the_tools_with_a_hook_and_reads_the_others_as_they_are(model, monkeypatch, tool):
    monkeypatch.setattr(model.settings, "inputs", "whoop,fitbit,garmin")
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_url", tool.url)
    monkeypatch.setattr(settings, "garmin_grafana_refresh_url", "")
    steps = refresh.plan(model)
    assert [(s.kind, s.key, s.state) for s in steps] == [
        ("hook", "fitbit-grafana", "waiting"),
        ("tool", "garmin-grafana", "skipped"),
        ("run", "whoop", "skipped"),  # not configured here
        ("run", "whoop_model", "waiting"),
    ]
    assert steps[2].message == "not configured"


def _fake_runs(monkeypatch, order, status="ok"):
    """scheduler.request runs at once: it records a finished refresh run."""
    def request(key, **options):
        order.append(("run", key, options.get("trigger")))
        with SessionLocal() as db:
            db.add(SyncRun(connector=key, trigger=options["trigger"], status=status, message=f"{key} ran.",
                           started_at=utcnow(), finished_at=utcnow()))
            db.commit()
        return True
    monkeypatch.setattr(scheduler, "request", request)
    monkeypatch.setattr(refresh, "POLL_SECONDS", 0.01)


def test_the_chain_asks_the_tools_then_runs_whoop_then_the_models(model, monkeypatch, whoop_connected):
    order = []
    _fake_runs(monkeypatch, order)
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_url", "http://tool.example/refresh")
    monkeypatch.setattr(refresh, "call_hook", lambda url, token: (order.append(("hook", url)), (True, "fetched"))[1])
    chain = refresh.Chain(model.key, refresh.plan(model))
    refresh._run_chain(chain)
    assert order == [("hook", "http://tool.example/refresh"), ("run", "whoop", "refresh"),
                     ("run", "whoop_model", "refresh")]
    assert [s.state for s in chain.steps] == ["done", "done", "done"]
    assert chain.steps[-1].message == "whoop_model ran." and chain.steps[-1].run_id
    assert not chain.running and not chain.failed


def test_a_failed_hook_does_not_stop_the_models(model, monkeypatch, tool):
    order = []
    _fake_runs(monkeypatch, order)
    tool.status = 500
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_url", tool.url)
    chain = refresh.Chain(model.key, refresh.plan(model))
    refresh._run_chain(chain)
    assert chain.steps[0].state == "failed" and chain.steps[0].message == "answered 500"
    assert ("run", "whoop_model", "refresh") in order and chain.steps[-1].state == "done"
    assert chain.failed and refresh.as_dict(model, chain)["state"] == "failed"


def test_a_failed_run_is_a_failed_step(model, monkeypatch):
    _fake_runs(monkeypatch, [], status="error")
    chain = refresh.Chain(model.key, refresh.plan(model))
    refresh._run_chain(chain)
    assert chain.steps[-1].state == "failed" and chain.steps[-1].message == "whoop_model ran."


def test_a_run_that_never_ends_fails_its_step_after_the_timeout(model, monkeypatch):
    monkeypatch.setattr(scheduler, "request", lambda key, **options: True)
    monkeypatch.setattr(settings, "refresh_timeout_seconds", 0.05)
    monkeypatch.setattr(refresh, "POLL_SECONDS", 0.01)
    chain = refresh.Chain(model.key, refresh.plan(model))
    refresh._run_chain(chain)
    assert chain.steps[-1].state == "failed" and "still not done" in chain.steps[-1].message


def test_its_runs_are_real_refresh_runs(model, monkeypatch, no_thread):
    """Through the real queue: trigger "refresh", which never moves the schedule."""
    assert scheduler.request("whoop_model", trigger=refresh.TRIGGER)
    assert ("whoop_model", (("trigger", "refresh"),)) in scheduler._pending


def test_a_second_request_joins_the_running_chain(model, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(refresh, "_run_chain", lambda chain: release.wait(5) and None)
    first = refresh.start(model)
    assert refresh.start(model) is first and first.running
    release.set()


# -- The pages and the API --------------------------------------------------------------------

def test_the_page_lists_the_steps_and_posts_from_a_button(client, model, monkeypatch, tool):
    monkeypatch.setattr(settings, "fitbit_grafana_refresh_url", tool.url)
    monkeypatch.setattr(settings, "grafana_url", "https://grafana.example.com/")
    page = client.get("/connectors/whoop_model/refresh")
    assert page.status_code == 200
    html = page.text
    assert 'action="/connectors/whoop_model/refresh"' in html and "Refresh and recompute" in html
    assert "Ask fitbit-grafana to fetch now" in html and "Run WHOOP models" in html
    assert 'href="https://grafana.example.com/d/tributary-sleep-planner"' in html
    assert 'data-poll="/partials/refresh/whoop_model"' in html
    assert tool.url not in html and not tool.requests  # opening the page starts nothing


def test_the_page_has_no_back_link_without_grafana(client, model, monkeypatch):
    monkeypatch.setattr(settings, "grafana_url", None)
    assert "Back to the" not in client.get("/connectors/whoop_model/refresh").text


def test_a_connector_with_nothing_to_refresh_has_no_refresh(client):
    for response in (client.get("/connectors/audiobookshelf/refresh"),
                     client.get("/partials/refresh/audiobookshelf"),
                     client.get("/api/connectors/audiobookshelf/refresh"),
                     client.post("/api/connectors/audiobookshelf/refresh")):
        assert response.status_code == 404
    assert client.get("/api/connectors/nope/refresh").status_code == 404


def test_cross_site_refreshes_are_refused(client, model, monkeypatch):
    started = []
    monkeypatch.setattr(refresh, "start", lambda connector: started.append(connector) or None)
    for site in ("cross-site", "same-site"):
        assert client.post("/connectors/whoop_model/refresh", headers={"sec-fetch-site": site}).status_code == 403
        assert client.post("/api/connectors/whoop_model/refresh",
                           headers={"sec-fetch-site": site}).status_code == 403
    assert client.post("/api/connectors/whoop_model/refresh",
                       headers={"origin": "https://evil.example"}).status_code == 403
    assert not started


def test_an_unconfigured_connector_is_not_refreshed(client):
    assert client.post("/api/connectors/whoop_model/refresh").status_code == 409
    answer = client.post("/connectors/whoop_model/refresh", follow_redirects=False)
    assert answer.status_code == 303 and "notice=" in answer.headers["location"]


def test_the_api_starts_a_refresh_and_reports_it(client, model, monkeypatch):
    monkeypatch.setattr(refresh, "_run_chain", lambda chain: setattr(chain, "finished_at", utcnow()))
    idle = client.get("/api/connectors/whoop_model/refresh").json()
    assert idle["state"] == "idle" and [s["key"] for s in idle["steps"]] == ["fitbit-grafana", "whoop", "whoop_model"]
    started = client.post("/api/connectors/whoop_model/refresh", headers={"sec-fetch-site": "same-origin"})
    assert started.status_code == 202 and started.json()["connector"] == "whoop_model"
    assert client.get("/api/connectors/whoop_model/refresh").json()["state"] == "done"


def test_the_form_starts_a_refresh_and_lands_on_its_page(client, model, monkeypatch):
    monkeypatch.setattr(refresh, "_run_chain", lambda chain: None)
    answer = client.post("/connectors/whoop_model/refresh", follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/connectors/whoop_model/refresh"
    fragment = client.get("/partials/refresh/whoop_model").text
    assert 'data-region="refresh:whoop_model"' in fragment and "Refreshing" in fragment and "disabled" in fragment


def test_the_connector_page_offers_the_refresh_only_when_it_has_one(client, model, abs_api):
    assert "Refresh inputs and run" in client.get("/connectors/whoop_model").text
    assert "Refresh inputs and run" not in client.get("/connectors/audiobookshelf").text


def test_a_refresh_run_is_labelled_so(client, model):
    with SessionLocal() as db:
        db.add(SyncRun(connector="whoop_model", trigger="refresh", status="ok", message="ok",
                       started_at=utcnow(), finished_at=utcnow()))
        db.commit()
    assert "<td>Refresh</td>" in client.get("/runs").text
