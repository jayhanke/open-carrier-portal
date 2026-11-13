from __future__ import annotations

import json
import re
from uuid import uuid4

from fastapi.testclient import TestClient
from test_admin import BASE, csrf, login, post
from test_admin import admin as admin
from test_admin import company_link as company_link
from test_admin import company_source as company_source

from open_carrier_portal.source_database import SourceMapping


def navigation(browser):
    response = browser.get("/admin/account")
    assert response.status_code == 200
    utility = re.search(
        r'<nav class="utility-navigation".*?</nav>', response.text, re.DOTALL
    ).group()
    setup = re.search(r'<nav class="setup-navigation".*?</nav>', response.text, re.DOTALL).group()
    return utility, setup


def completed(markup, section):
    match = re.search(rf'href="/admin/{section}" data-setup-complete="(true|false)"', markup)
    assert match is not None
    return match.group(1) == "true"


def test_utility_navigation_is_above_setup_and_visits_do_not_complete_steps(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        utility, setup = navigation(browser)
        for section in ("setup", "logs", "account"):
            assert f'href="/admin/{section}"' in utility
            assert f'href="/admin/{section}"' not in setup
        assert 'action="/admin/logout"' in utility
        assert "data-setup-complete" not in utility
        sections = ("identity", "sources", "mapping", "portal", "partners", "remote")
        for section in sections:
            assert not completed(setup, section)
            assert browser.get("/admin/" + section).status_code == 200
        _, after = navigation(browser)
        assert all(not completed(after, section) for section in sections)
        page = browser.get("/admin/setup").text
        assert page.index(utility) < page.index('class="setup-navigation"')
        assert 'href="/admin/diagnostics"' not in page
        assert "Checks show configured setup, not live service health" in page


def test_configuration_checks_change_without_connecting_to_source(admin, tmp_path, monkeypatch):
    app, _, _ = admin
    repo = app.state.repository
    store = app.state.admin_store
    database = tmp_path / "not-opened.db"
    mapping = json.loads(SourceMapping.sharing_defaults().to_json())
    for resource in ("devices", "ports"):
        mapping["resources"][resource]["columns"].pop("observium_id", None)

    def forbid_source(*args, **kwargs):
        raise AssertionError("Navigation must not connect to source systems.")

    monkeypatch.setattr("open_carrier_portal.admin.configured_source_database", forbid_source)
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        assert post(browser, "identity", name="Example Carrier").status_code == 200
        repo.put_source_database_profile(
            name="selected",
            driver="sqlite",
            database_name=str(database),
            host=None,
            port=None,
            username=None,
            password_env_var=None,
            tls_ca_path=None,
            timeout_seconds=10,
            mapping_json=json.dumps(mapping),
        )
        assert store.get("source") == {}
        _, setup = navigation(browser)
        assert completed(setup, "identity")
        assert completed(setup, "sources")
        assert completed(setup, "mapping")
        assert not database.exists()
        dashboard = browser.get("/admin/setup").text.split("<h2>Setup</h2>", 1)[1]
        assert "legacy API-default selection is separate" in dashboard
        assert "After source setup" not in dashboard
        store.save("observium", {"url": "https://monitoring.example"})
        _, setup = navigation(browser)
        assert not completed(setup, "mapping")
        store.save("observium", {})
        repo.put_source_database_profile(
            name="selected",
            driver="sqlite",
            database_name=str(database),
            host=None,
            port=None,
            username=None,
            password_env_var=None,
            tls_ca_path=None,
            timeout_seconds=10,
            mapping_json=SourceMapping({}).to_json(),
            enabled=False,
        )
        _, setup = navigation(browser)
        assert not completed(setup, "sources") and not completed(setup, "mapping")
        store.save("source", {"name": "missing"})
        _, setup = navigation(browser)
        assert not completed(setup, "sources")
        assert not completed(setup, "mapping")
        assert not database.exists()
        store.save("remote:partner", {"url": "https://partner.example"})
        assert not completed(navigation(browser)[1], "remote")
        store.save(
            "remote:partner",
            {
                "url": "https://partner.example",
                "client": "client",
                "secret_env": "PARTNER_SECRET",
                "scope": "circuits:read",
            },
        )
        assert completed(navigation(browser)[1], "remote")
        store.save("remote:partner", {})
        assert not completed(navigation(browser)[1], "remote")
        with repo._connection() as db:
            db.execute(
                "UPDATE source_database_profiles SET enabled=1,mapping_json='{' "
                "WHERE name='selected'"
            )
        store.save("source", {"name": "selected"})
        _, setup = navigation(browser)
        assert not completed(setup, "mapping") and "Saved mapping is invalid" in setup
        assert any(
            row["event"].startswith("setup.mapping_invalid")
            for row in store.recent_events("database")
        )


def test_sharing_check_is_not_limited_to_first_hundred_rows(admin):
    app, _, _ = admin
    repo = app.state.repository
    partner = repo.create_partner("Partner")
    with repo._connection() as db:
        for number in range(102):
            db.execute(
                "INSERT INTO circuit_shares "
                "(share_id,circuit_source_id,partner_id,visibility,status,shared_at) "
                "VALUES (?,?,?,'endpoints',?,'now')",
                (str(uuid4()), str(number), partner, "active" if number == 101 else "revoked"),
            )
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        assert completed(navigation(browser)[1], "partners")
        repo.revoke_partner(partner)
        assert not completed(navigation(browser)[1], "partners")


def test_mapping_checks_all_enabled_connections_and_ignores_disabled_ones(admin, tmp_path):
    app, _, _ = admin
    repo = app.state.repository

    def profile(name, mapping, enabled=True):
        repo.put_source_database_profile(
            name=name, driver="sqlite", database_name=str(tmp_path / f"{name}.db"),
            host=None, port=None, username=None, password_env_var=None, tls_ca_path=None,
            timeout_seconds=10, mapping_json=mapping.to_json(), enabled=enabled,
        )

    profile("complete", SourceMapping.sharing_defaults())
    profile("incomplete", SourceMapping({}))
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        _, setup = navigation(browser)
        assert completed(setup, "sources") and not completed(setup, "mapping")
        profile("incomplete", SourceMapping({}), enabled=False)
        assert completed(navigation(browser)[1], "mapping")
        profile("incomplete", SourceMapping.sharing_defaults())
        assert completed(navigation(browser)[1], "mapping")
        assert app.state.admin_store.get("source") == {}


def test_readiness_checks_enabled_connections_without_selecting_api_default(
    admin, tmp_path, monkeypatch,
):
    app, _, _ = admin
    tested = []

    class Source:
        def health_check(self):
            pass

        def validate_mapping(self):
            pass

        def missing_fields_for(self, **kwargs):
            return {}

    def source_for(repository, name, metadata_path):
        tested.append(name)
        return Source()

    monkeypatch.setattr("open_carrier_portal.admin.source_for", source_for)
    for name in ("first", "second"):
        app.state.repository.put_source_database_profile(
            name=name, driver="sqlite", database_name=str(tmp_path / f"{name}.db"),
            host=None, port=None, username=None, password_env_var=None, tls_ca_path=None,
            timeout_seconds=10, mapping_json=SourceMapping.sharing_defaults().to_json(),
        )
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        post(browser, "identity", name="Example Carrier")
        response = post(browser, "readiness")
        assert response.status_code == 200
        assert "Source connectivity/mapping passed: first" in response.text
        assert "Source connectivity/mapping passed: second" in response.text
        assert "circuit retrieval is not ready" not in response.text
        assert tested == ["first", "second"]
        assert app.state.admin_store.get("source") == {}


def test_pairing_and_sharing_checks_follow_current_configuration(admin, company_link):
    app, _, connection_file = admin
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        _, setup = navigation(browser)
        assert completed(setup, "portal") and completed(setup, "partners")
        identity = app.state.repository.get_connector_identity()
        app.state.repository.put_connector_identity(identity.model_copy(update={"enabled": False}))
        _, setup = navigation(browser)
        assert not completed(setup, "portal") and not completed(setup, "partners")
        app.state.repository.put_connector_identity(identity)
        data = json.loads(connection_file.read_text())
        data["connector_id"] = str(uuid4())
        connection_file.write_text(json.dumps(data))
        _, setup = navigation(browser)
        assert not completed(setup, "portal")


def test_logs_preserve_all_histories_and_legacy_diagnostics_redirect(admin):
    app, _, _ = admin
    store = app.state.admin_store
    store.audit("operator", "preserved.admin.audit")
    app.state.repository.record_audit(
        partner_id=None,
        client_id=None,
        operation="preserved.partner.api",
        resource_summary="must-not-export-source-data",
        outcome="denied",
        correlation_id="preserved-api-reference",
    )
    store.record_event(
        level="ERROR",
        component="observium",
        event="preserved.observium.failure",
        message="Monitoring request failed.",
        request_id="preserved-monitoring-reference",
        details={},
    )
    with TestClient(app, base_url=BASE) as browser:
        assert browser.get("/admin/diagnostics", follow_redirects=False).headers["location"] == (
            "/admin/login"
        )
        login(browser)
        redirect = browser.get("/admin/diagnostics", follow_redirects=False)
        assert redirect.status_code == 303 and redirect.headers["location"] == "/admin/logs"
        page = browser.get("/admin/logs").text
        for value in (
            "preserved.admin.audit",
            "preserved.partner.api",
            "preserved-api-reference",
            "preserved.observium.failure",
            "preserved-monitoring-reference",
            "Administration audit",
            "Partner API requests",
            "Observium connection logs",
        ):
            assert value in page
        assert "must-not-export-source-data" not in page
        assert 'href="/admin/diagnostics"' not in page
        assert browser.get("/admin/logs?component=" + "a" * 65).status_code == 400
        response = browser.post("/admin/logout", data={"csrf": csrf(browser)})
        assert response.status_code == 200
        assert (
            browser.get("/admin/logs", follow_redirects=False).headers["location"] == "/admin/login"
        )


def test_component_filters_keep_pagination_and_action_links(admin):
    app, _, _ = admin
    store = app.state.admin_store
    for number in range(101):
        store.record_event(
            level="INFO",
            component="observium",
            event=f"monitoring.event.{number}",
            message="Monitoring event.",
            request_id=str(number),
            details={},
            action_id="connector.monitor.test",
        )
    store.record_event(
        level="INFO",
        component="database",
        event="database.not-selected",
        message="Database event.",
        request_id="db",
        details={},
    )
    with TestClient(app, base_url=BASE) as browser:
        login(browser)
        page = browser.get("/admin/logs?component=observium").text
        assert "monitoring.event.100" in page and "monitoring.event.0<" not in page
        assert "database.not-selected" not in page
        assert "component=observium" in page and "offset=100" in page
        assert "action_id=connector.monitor.test&amp;component=observium" in page
        older = browser.get("/admin/logs?component=observium&offset=100").text
        assert "monitoring.event.0<" in older
        assert "monitoring.event.100" not in older
        assert "Older events" not in older
