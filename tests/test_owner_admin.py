from __future__ import annotations

import json
from dataclasses import asdict

from fastapi.testclient import TestClient
from test_admin import BASE, csrf, login, post
from test_admin import admin as admin
from test_live_transport import live_source as live_source


def test_owner_inventory_opt_in_is_local_explicit_and_can_be_stopped(
    admin,
    live_source,
    monkeypatch,
):
    app, _, path = admin
    _, source_repo, connection, _, _ = live_source
    repo = app.state.repository
    repo.put_connector_identity(source_repo.get_connector_identity())
    values = asdict(source_repo.get_source_database_profile("inventory"))
    for key in ("profile_id", "created_at", "updated_at"):
        values.pop(key)
    profile_id = repo.put_source_database_profile(**values)
    repo.put_source_database_profile(**{**values, "name": "Disabled", "enabled": False})
    path.write_text(json.dumps(asdict(connection)), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr("open_carrier_portal.admin.send_heartbeat", lambda *args, **kwargs: None)
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        page = browser.get("/admin/portal")
        assert "My company inventory in Carrier Portal" in page.text
        assert '<option value="Disabled">' not in page.text
        assert repo.list_portal_owner_sources(connection) == []
        assert (
            browser.post(
                "/admin/enable-owner-source",
                data={
                    "profile": "inventory",
                    "consent": "yes",
                    "csrf": "wrong",
                },
            ).status_code
            == 403
        )
        denied = post(browser, "enable-owner-source", profile="inventory")
        assert denied.status_code == 400
        assert "explicit owner approval" in denied.text
        assert repo.list_portal_owner_sources(connection) == []
        approved = post(browser, "enable-owner-source", profile="inventory", consent="yes")
        assert approved.status_code == 200
        assert len(repo.list_portal_owner_sources(connection)) == 1
        original = repo.list_portal_owner_sources(connection)[0]
        assert not original.explorer_enabled
        assert not original.layout_enabled
        expanded = post(
            browser,
            "enable-owner-source",
            profile="inventory",
            consent="yes",
            explorer_consent="yes",
            layout_consent="yes",
        )
        assert expanded.status_code == 200
        current = repo.list_portal_owner_sources(connection)[0]
        assert current.explorer_enabled and current.grant_id != original.grant_id
        assert current.layout_enabled
        page = browser.get("/admin/portal")
        assert "Stop own-company browsing" in page.text
        assert (
            browser.post(
                "/admin/stop-owner-source",
                data={
                    "profile_id": profile_id,
                    "csrf": csrf(browser, "portal"),
                },
            ).status_code
            == 200
        )
        assert repo.list_portal_owner_sources(connection) == []
