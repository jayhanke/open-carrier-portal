from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_admin import BASE, csrf, login
from test_admin import admin as admin
from test_live_transport import job
from test_live_transport import live_source as live_source

from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.diagnostics import log_context, log_event, request_reference
from open_carrier_portal.interactions import (
    Interaction,
    current_interaction,
    interaction_context,
)
from open_carrier_portal.portal_agent import serve_live_request


def headers(action="connector.test.button"):
    return {"X-Action-ID": action, "X-Interaction-ID": str(uuid4())}


def test_connector_logs_require_local_admin_and_use_own_store(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as browser:
        assert browser.get("/admin/logs", follow_redirects=False).headers["location"] == (
            "/admin/login"
        )
        login(browser)
        response = browser.get("/admin/logs")
        assert response.status_code == 200 and "Connector logs only" in response.text
        assert 'href="/admin/logs"' in response.text
        assert "ocp-page-id" in response.text
        assert "/admin/assets/interactions.js" in response.text
        assert browser.get("/admin/assets/interactions.js").status_code == 200
        assert browser.get("/admin/logs?offset=-1").status_code == 400
        assert browser.get("/admin/logs?interaction_id=not-a-uuid").status_code == 400
        assert "Portal logs only" not in response.text


def test_connector_button_press_groups_actions_and_requests(admin):
    app, _, _ = admin
    trace = headers()
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        token = csrf(browser)
        activated = browser.post(
            "/admin/interactions", json={},
            headers={**trace, "X-CSRF-Token": token},
        )
        assert activated.status_code == 204
        first = browser.get("/admin/account", headers=trace)
        second = browser.get("/admin/diagnostics", headers=trace)
        assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
        assert first.headers["X-Action-ID"] == trace["X-Action-ID"]
        assert second.headers["X-Interaction-ID"] == trace["X-Interaction-ID"]
        assert browser.post("/admin/interactions", headers=trace).status_code == 403
        grouped = browser.get("/admin/logs", params={
            "interaction_id": trace["X-Interaction-ID"],
        })
        assert grouped.status_code == 200
        assert "ui.activated" in grouped.text
    events = app.state.admin_store.recent_events(interaction_id=trace["X-Interaction-ID"])
    assert {"ui.activated", "request.completed"} <= {row["event"] for row in events}
    assert {row["action_id"] for row in events} == {trace["X-Action-ID"]}
    assert app.state.admin_store.recent_events(action_id="connector.other") == []


def test_separate_presses_on_same_button_do_not_mix(admin):
    app, _, _ = admin
    first, second = headers(), headers()
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        browser.get("/admin/account", headers=first)
        browser.get("/admin/account", headers=second)
    store = app.state.admin_store
    assert len(store.recent_events(action_id=first["X-Action-ID"])) == 2
    assert len(store.recent_events(interaction_id=first["X-Interaction-ID"])) == 1
    assert len(store.recent_events(interaction_id=second["X-Interaction-ID"])) == 1


def test_explicit_session_checks_are_logged_but_background_polling_stays_quiet(admin):
    app, _, _ = admin
    explicit = headers("connector.session.resume")
    background = headers("connector.admin-account.background")
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        assert browser.get("/admin/session", headers=explicit).status_code == 200
        assert browser.get("/admin/session", headers=background).status_code == 200
    store = app.state.admin_store
    assert any(
        row["event"] == "request.completed"
        for row in store.recent_events(interaction_id=explicit["X-Interaction-ID"])
    )
    assert store.recent_events(interaction_id=background["X-Interaction-ID"]) == []

@pytest.mark.parametrize("trace", [
    {"X-Action-ID": "connector.test", "X-Interaction-ID": "private-invalid-id"},
    {"X-Action-ID": "private invalid action", "X-Interaction-ID": str(uuid4())},
    {"X-Action-ID": "connector.test"},
])
def test_invalid_trace_is_explicit_and_never_stored_as_supplied(admin, trace):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as browser:
        assert browser.get("/admin/login", headers=trace).status_code == 400
    row = app.state.admin_store.recent_events(action_id="connector.invalid-trace")[0]
    assert row["action_id"] == "connector.invalid-trace"
    assert "private" not in json.dumps(row)


def test_existing_log_database_migrates_without_losing_events(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE local_operational_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "occurred REAL NOT NULL, level TEXT NOT NULL, component TEXT NOT NULL, "
            "event TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL, "
            "details TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO local_operational_logs "
            "(occurred,level,component,event,message,request_id,details) "
            "VALUES (1,'INFO','admin','old.event','Old message','old','{}')"
        )
    store = AdminStore(path)
    store.initialize()
    store.initialize()
    assert store.recent_events()[0]["event"] == "old.event"
    assert store.recent_events()[0]["interaction_id"] == ""


def test_live_job_inherits_portal_press_for_reads_and_result_upload(live_source):
    settings, repo, connection, _, _ = live_source
    store = AdminStore(settings.database_path)
    store.initialize()
    trace = headers("portal.circuits.open")
    request = job(live_source)
    sent = []

    def handle(http_request):
        if http_request.url.path.endswith("/poll"):
            return httpx.Response(
                200, json={"request": request.model_dump(mode="json")}, headers=trace,
            )
        assert http_request.headers["X-Action-ID"] == trace["X-Action-ID"]
        assert http_request.headers["X-Interaction-ID"] == trace["X-Interaction-ID"]
        sent.append(json.loads(http_request.content))
        return httpx.Response(200, json={"status": "accepted"})

    with log_context(store) as outer, httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert serve_live_request(client, connection, repository=repo, settings=settings)
        assert request_reference() == outer.request_id
        assert current_interaction() is None
    events = store.recent_events(interaction_id=trace["X-Interaction-ID"])
    assert events
    assert {event["reference"] for event in events} == {str(request.request_id)}
    assert {"live.started", "live.read_completed", "live.result_accepted"} <= {
        event["event"] for event in events
    }
    assert sent[0]["page"]["items"][0]["name"] == "Example circuit"
    assert "Example circuit" not in json.dumps(events)
    assert "action_id" not in sent[0] and "interaction_id" not in sent[0]


def test_context_restoration_preserves_parent_diagnostics_store(tmp_path):
    store = AdminStore(tmp_path / "logs.db")
    store.initialize()
    first = Interaction("connector.first", str(uuid4()))
    second = Interaction("connector.second", str(uuid4()))
    with log_context(store), interaction_context(first):
        log_event("admin", "first", "First.")
        with log_context(request_id=str(uuid4())), interaction_context(second):
            log_event("admin", "second", "Second.")
        assert current_interaction() == first
    assert current_interaction() is None
    assert {row["action_id"] for row in store.recent_events()} == {
        "connector.first", "connector.second",
    }
