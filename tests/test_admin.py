from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from open_carrier_portal.admin import create_admin_app
from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.config import Settings
from open_carrier_portal.errors import AuthenticationError, AuthorizationError
from open_carrier_portal.main import create_app
from open_carrier_portal.portal_agent import PortalConnection
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import verify_client_secret

BASE = "http://127.0.0.1:8000"


@pytest.fixture
def admin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "open_carrier_portal.admin.serve_live_request", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        "open_carrier_portal.admin.portal_access_requests", lambda *args, **kwargs: []
    )
    settings = Settings(
        database_path=tmp_path / "metadata.db",
        issuer_url="http://localhost:8000",
        token_audience="test",
        token_signing_key="s" * 48,
    )
    app = create_admin_app(settings, connection_file=tmp_path / "portal.json")
    app.state.admin_store.set_password("operator", "operator-password")
    return app, settings, tmp_path / "portal.json"


def csrf(client: TestClient, section: str = "setup") -> str:
    response = client.get("/admin/" + section)
    assert response.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def login(client: TestClient, password: str = "operator-password") -> None:
    response = client.post(
        "/admin/login",
        data={"username": "operator", "password": password, "csrf": csrf(client, "login")},
        headers={"Origin": str(client.base_url).rstrip("/")},
        follow_redirects=False,
    )
    assert response.status_code == 303


def post(browser: TestClient, action: str, **data):
    return browser.post("/admin/" + action, data={"csrf": csrf(browser), **data})


@pytest.fixture
def company_source(admin, tmp_path):
    app, _, _ = admin
    database = tmp_path / "customer-directory.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE customer_accounts (account_key INTEGER PRIMARY KEY, display_label TEXT)"
        )
        db.executemany(
            "INSERT INTO customer_accounts VALUES (?, ?)",
            [(42, "Example <Carrier>"), (77, "Second company")],
        )
    app.state.repository.put_source_database_profile(
        name="customer-db",
        driver="sqlite",
        database_name=str(database),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=json.dumps(
            {
                "resources": {
                    "companies": {
                        "table": "customer_accounts",
                        "columns": {"source_id": "account_key", "name": "display_label"},
                    },
                    "circuits": {
                        "table": "not_needed_for_company_directory",
                        "columns": {"source_id": "id"},
                    },
                }
            }
        ),
    )
    return database


@pytest.fixture
def company_link(admin, company_source, monkeypatch):
    from open_carrier_portal.company_access import approve_company_policy
    from open_carrier_portal.company_policies import CompanyPolicy, PolicySnapshot
    from open_carrier_portal.models import ConnectorIdentity
    from open_carrier_portal.portal_agent import portal_access_requests

    app, settings, connection_file = admin
    repo = app.state.repository
    identity = ConnectorIdentity(
        organization_id=str(uuid4()),
        organization_name="Owner",
        connector_id=str(uuid4()),
        issuer_url=settings.issuer_url,
    )
    repo.put_connector_identity(identity)
    portal = PortalConnection(
        "https://portal.example",
        identity.connector_id,
        identity.organization_id,
        "t" * 64,
    )
    connection_file.touch(mode=0o600)
    connection_file.write_text(
        json.dumps(
            {
                "portal_url": portal.portal_url,
                "connector_id": portal.connector_id,
                "organization_id": portal.organization_id,
                "token": portal.token,
            }
        )
    )
    profile = repo.get_source_database_profile("customer-db")
    mapping = json.loads(profile.mapping_json)
    mapping["resources"]["circuits"] = {
        "table": "circuits",
        "columns": {"source_id": "id", "billing_company_id": "customer"},
    }
    with sqlite3.connect(company_source) as db:
        db.execute("CREATE TABLE circuits (id INTEGER PRIMARY KEY, customer INTEGER)")
        db.executemany("INSERT INTO circuits VALUES (?, ?)", [(1, 42), (2, 77)])
    repo.put_source_database_profile(
        name=profile.name,
        driver="sqlite",
        host=None,
        port=None,
        database_name=str(company_source),
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=json.dumps(mapping),
    )
    other = CompanyPolicy(
        policy_id=uuid4(),
        revision=1,
        owner_organization_id=portal.organization_id,
        recipient_organization_id=uuid4(),
        recipient_name="Existing recipient",
        connector_id=portal.connector_id,
        state="pending",
    )
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[other]))
    approve_company_policy(
        repo,
        portal,
        metadata_path=settings.database_path,
        policy_id=str(other.policy_id),
        revision=1,
        profile_name=profile.name,
        company_id="77",
        approved_by="operator",
    )
    recipient = str(uuid4())
    state = {"fail_heartbeat": False, "policies": [other.model_dump(mode="json")], "requests": []}

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {portal.token}"
        state["requests"].append(request)
        current = next(
            (p for p in state["policies"] if p["recipient_organization_id"] == recipient), None
        )
        if request.url.path == "/api/connectors/access-requests":
            return httpx.Response(
                200,
                json={
                    "requests": [
                        {
                            "request_id": str(uuid4()),
                            "company_id": recipient,
                            "name": "Recipient <Carrier>",
                        }
                    ]
                },
            )
        if request.method == "GET":
            assert request.url.path == f"/api/connectors/companies/{recipient}"
            return httpx.Response(
                200,
                json={
                    "company_id": recipient,
                    "name": "Recipient <Carrier>",
                    "policy_id": current["policy_id"] if current else None,
                    "revision": current["revision"] if current else 0,
                    "visibility": current["visibility"] if current else "endpoints",
                },
            )
        payload = json.loads(request.content)
        if request.url.path == "/api/connectors/company-links":
            before_proposal = state.get("before_proposal")
            if before_proposal:
                before_proposal()
            assert payload["connector_id"] == portal.connector_id
            assert payload["recipient_company_id"] == recipient
            policy = CompanyPolicy(
                policy_id=current["policy_id"] if current else uuid4(),
                revision=payload["expected_revision"] + 1,
                owner_organization_id=portal.organization_id,
                recipient_organization_id=recipient,
                recipient_name="Recipient <Carrier>",
                connector_id=portal.connector_id,
                state="pending",
                visibility=payload["visibility"],
            ).model_dump(mode="json")
            state["policies"] = [
                p for p in state["policies"] if p["policy_id"] != policy["policy_id"]
            ] + [policy]
            return httpx.Response(
                200, json={"policy_id": policy["policy_id"], "policies": state["policies"]}
            )
        assert request.url.path == "/api/connectors/heartbeat"
        if state["fail_heartbeat"]:
            return httpx.Response(503)
        for accepted in payload["policy_receipts"]:
            for policy in state["policies"]:
                if (
                    accepted["policy_id"] == policy["policy_id"]
                    and accepted["revision"] == policy["revision"]
                ):
                    policy["state"] = (
                        "active" if accepted["state"] == "accepted" else "needs_approval"
                    )
        return httpx.Response(
            200,
            json={
                "status": "connected",
                "company_policies": {"version": 1, "policies": state["policies"]},
            },
        )

    monkeypatch.setattr(
        "open_carrier_portal.admin.http_client",
        lambda trust: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("open_carrier_portal.admin.portal_access_requests", portal_access_requests)
    return portal, recipient, state


def test_company_id_link_picker_approval_and_billing_scope(admin, company_link):
    from open_carrier_portal.company_access import require_company_circuit

    app, settings, _ = admin
    portal, recipient, state = company_link
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get(
            "/admin/partners",
            params={
                "profile": "customer-db",
                "portal_company_id": recipient,
                "q": "42",
            },
        )
        assert page.status_code == 200
        assert "Portal circuit access requests" in page.text
        assert (
            f'<option value="{recipient}" selected>Recipient &lt;Carrier&gt;</option>' in page.text
        )
        assert "Portal company: Recipient &lt;Carrier&gt;" in page.text
        assert 'type="radio" name="company" value="42"' in page.text
        assert 'type="radio" name="company" value="77"' not in page.text
        assert f'name="portal_company_id" value="{recipient}"' in page.text
        assert 'name="expected_revision" value="0"' in page.text
        assert "Advanced: direct connector access" in page.text
        data = {
            "profile": "customer-db",
            "portal_company_id": recipient,
            "expected_policy_id": "",
            "expected_revision": "0",
            "company": "42",
            "visibility": "endpoints",
            "consent": "yes",
        }
        result = post(client, "link-portal-company", **data)
        assert result.status_code == 200
        assert "Sharing is active" in result.text
        local = next(
            p
            for p in app.state.repository.list_portal_policies(portal)
            if str(p.policy.recipient_organization_id) == recipient
        )
        assert local.policy.state == "active" and local.receipt.source_company_id == "42"
        assert {
            p.source_company_id for p in app.state.repository.portal_policy_receipts(portal)
        } == {
            "42",
            "77",
        }
        kwargs = {
            "metadata_path": settings.database_path,
            "policy_id": str(local.policy.policy_id),
            "revision": 1,
            "recipient_organization_id": recipient,
        }
        require_company_circuit(app.state.repository, portal, circuit_source_id="1", **kwargs)
        with pytest.raises(AuthorizationError):
            require_company_circuit(app.state.repository, portal, circuit_source_id="2", **kwargs)
        assert (
            post(client, "stop-company-policy", policy=str(local.policy.policy_id)).status_code
            == 200
        )
        with pytest.raises(AuthorizationError):
            require_company_circuit(app.state.repository, portal, circuit_source_id="1", **kwargs)
        assert portal.token not in client.get("/admin/diagnostics").text
        assert any(r.url.path == "/api/connectors/company-links" for r in state["requests"])


@pytest.mark.parametrize("invalid", ["csrf", "consent", "company", "stale", "layout", "mapping"])
def test_company_link_rejects_before_pending_proposal(admin, company_link, invalid):
    app, _, _ = admin
    _, recipient, state = company_link
    with TestClient(app, base_url=BASE) as client:
        login(client)
        data = {
            "csrf": csrf(client),
            "profile": "customer-db",
            "portal_company_id": recipient,
            "expected_policy_id": "",
            "expected_revision": "0",
            "company": "42",
            "visibility": "endpoints",
            "consent": "yes",
        }
        if invalid == "csrf":
            data["csrf"] = "wrong"
        elif invalid == "consent":
            data["consent"] = ""
        elif invalid == "company":
            data["company"] = "missing"
        elif invalid == "stale":
            data["expected_revision"] = "1"
            data["expected_policy_id"] = str(uuid4())
        elif invalid == "layout":
            data["visibility"] = "full_shared_path"
        else:
            data["visibility"] = "full_shared_path"
            data["layout_consent"] = "yes"
        response = client.post("/admin/link-portal-company", data=data)
        assert response.status_code == (403 if invalid == "csrf" else 400)
        assert not any(r.url.path == "/api/connectors/company-links" for r in state["requests"])
        assert len(state["policies"]) == 1


def test_company_link_activation_failure_is_explicit_and_heartbeat_retries(admin, company_link):
    app, _, _ = admin
    portal, recipient, state = company_link
    with TestClient(app, base_url=BASE) as client:
        login(client)
        state["fail_heartbeat"] = True
        result = post(
            client,
            "link-portal-company",
            profile="customer-db",
            portal_company_id=recipient,
            expected_revision="0",
            company="42",
            visibility="endpoints",
            consent="yes",
        )
        assert result.status_code == 400
        assert "approval was saved, but portal activation is unconfirmed" in result.text
        assert "Sharing is active" not in result.text
        local = next(
            p
            for p in app.state.repository.list_portal_policies(portal)
            if str(p.policy.recipient_organization_id) == recipient
        )
        assert local.policy.state == "pending" and local.receipt.source_company_id == "42"
        assert "company_link.acknowledgement_failed" in client.get("/admin/diagnostics").text
        state["fail_heartbeat"] = False
        assert post(client, "heartbeat").status_code == 200
        local = next(
            p
            for p in app.state.repository.list_portal_policies(portal)
            if str(p.policy.recipient_organization_id) == recipient
        )
        assert local.policy.state == "active"


def test_invalid_portal_id_does_not_call_remote_lookup(admin, company_link):
    app, _, _ = admin
    _, _, state = company_link
    with TestClient(app, base_url=BASE) as client:
        login(client)
        result = client.get("/admin/partners?profile=customer-db&portal_company_id=invalid")
        assert result.status_code == 400 and "Invalid portal Company ID" in result.text
        assert not any(
            r.url.path.startswith("/api/connectors/companies/") for r in state["requests"]
        )


def test_request_list_failure_keeps_directory_visible_and_reports_diagnostics(
    admin, company_source, monkeypatch
):
    app, _, connection_file = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        post(client, "identity", name="Owner")
        connection_file.touch(mode=0o600)
        connection_file.write_text(
            json.dumps(
                {
                    "portal_url": "https://portal.example",
                    "connector_id": app.state.repository.get_connector_identity().connector_id,
                    "organization_id": str(uuid4()),
                    "token": "t" * 64,
                }
            )
        )

        def fail(*args, **kwargs):
            raise ValueError("invalid portal request metadata")

        monkeypatch.setattr("open_carrier_portal.admin.portal_access_requests", fail)
        response = client.get("/admin/partners?profile=customer-db")
        assert response.status_code == 200
        assert "Could not load portal access requests" in response.text
        assert "Example &lt;Carrier&gt;" in response.text
        assert "access_requests.load_failed" in client.get("/admin/diagnostics").text


def test_company_link_and_normal_heartbeat_are_serialized(admin, company_link):
    app, _, _ = admin
    _, recipient, state = company_link
    proposal_started, release_proposal, heartbeat_started = Event(), Event(), Event()

    def hold_proposal():
        proposal_started.set()
        assert release_proposal.wait(timeout=5)

    state["before_proposal"] = hold_proposal
    with TestClient(app, base_url=BASE) as client, ThreadPoolExecutor(max_workers=2) as pool:
        login(client)
        token = csrf(client)
        linking = pool.submit(
            client.post,
            "/admin/link-portal-company",
            data={
                "csrf": token,
                "profile": "customer-db",
                "portal_company_id": recipient,
                "expected_revision": "0",
                "company": "42",
                "visibility": "endpoints",
                "consent": "yes",
            },
        )
        assert proposal_started.wait(timeout=3)
        before = len(state["requests"])

        def concurrent_heartbeat():
            heartbeat_started.set()
            return client.post("/admin/heartbeat", data={"csrf": token})

        checking = pool.submit(concurrent_heartbeat)
        assert heartbeat_started.wait(timeout=3)
        try:
            with pytest.raises(TimeoutError):
                checking.result(timeout=0.2)
            assert len(state["requests"]) == before
        finally:
            release_proposal.set()
        assert linking.result(timeout=5).status_code == 200
        assert checking.result(timeout=5).status_code == 200


def test_company_lookup_is_preserved_across_directory_search_and_pages(
    admin, company_source, company_link
):
    app, _, _ = admin
    _, recipient, _ = company_link
    with sqlite3.connect(company_source) as db:
        db.executemany(
            "INSERT INTO customer_accounts VALUES (?, ?)",
            [(i, f"Customer {i}") for i in range(1000, 1150)],
        )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get(
            "/admin/partners",
            params={
                "profile": "customer-db",
                "portal_company_id": recipient,
                "q": "Customer",
            },
        )
        next_url = re.search(r'<a rel="next" href="([^"]+)"', page.text).group(1)
        assert recipient in next_url and "q=Customer" in next_url
        next_page = client.get(next_url.replace("&amp;", "&"))
        assert next_page.status_code == 200
        assert 'type="radio" name="company" value="1100"' in next_page.text
        assert 'type="radio" name="company" value="1000"' not in next_page.text
        assert f'name="portal_company_id" value="{recipient}"' in next_page.text
        assert not re.search(r'<input[^>]+name="company"[^>]+checked', next_page.text)


def test_portal_company_approval_requires_live_mapped_company_consent_and_csrf(
    admin, company_source
):
    from open_carrier_portal.company_policies import CompanyPolicy, PolicySnapshot

    app, _, connection_file = admin
    repo = app.state.repository
    with TestClient(app, base_url=BASE) as client:
        login(client)
        post(client, "identity", name="Owner")
        portal = PortalConnection(
            "https://portal.example",
            repo.get_connector_identity().connector_id,
            str(uuid4()),
            "t" * 64,
        )
        connection_file.touch(mode=0o600)
        connection_file.write_text(
            json.dumps(
                {
                    "portal_url": portal.portal_url,
                    "connector_id": portal.connector_id,
                    "organization_id": portal.organization_id,
                    "token": portal.token,
                }
            )
        )
        policy = CompanyPolicy(
            policy_id=uuid4(),
            revision=1,
            owner_organization_id=portal.organization_id,
            recipient_organization_id=uuid4(),
            recipient_name="Recipient <Carrier>",
            connector_id=portal.connector_id,
            state="pending",
        )
        repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[policy]))
        page = client.get("/admin/partners?profile=customer-db")
        assert "Recipient &lt;Carrier&gt;" in page.text
        assert "Recipient <Carrier>" not in page.text
        assert "Approve company link and circuit policy" not in page.text
        assert 'name="portal_company_id"' in page.text
        assert 'class="company-picker"' in page.text
        assert "<td>42</td>" in page.text
        assert 'type="text" name="company"' not in page.text
        assert "Search company name or ID" in page.text
        data = {
            "policy": f"{policy.policy_id}@1",
            "profile": "customer-db",
            "company": "42",
            "consent": "yes",
        }
        assert (
            client.post(
                "/admin/approve-company-policy",
                data={
                    **data,
                    "csrf": "invalid",
                },
            ).status_code
            == 403
        )
        assert post(client, "approve-company-policy", **(data | {"consent": ""})).status_code == 400
        assert post(client, "approve-company-policy", **data).status_code == 400
        profile = repo.get_source_database_profile("customer-db")
        mapping = json.loads(profile.mapping_json)
        mapping["resources"]["circuits"] = {
            "table": "circuits",
            "columns": {"source_id": "id", "billing_company_id": "customer"},
        }
        with sqlite3.connect(company_source) as db:
            db.execute("CREATE TABLE circuits (id INTEGER PRIMARY KEY, customer INTEGER)")
        repo.put_source_database_profile(
            name=profile.name,
            driver="sqlite",
            host=None,
            port=None,
            database_name=str(company_source),
            username=None,
            password_env_var=None,
            tls_ca_path=None,
            timeout_seconds=10,
            mapping_json=json.dumps(mapping),
        )
        assert (
            post(client, "approve-company-policy", **(data | {"company": "missing"})).status_code
            == 400
        )
        approved = post(client, "approve-company-policy", **data)
        assert approved.status_code == 200
        assert repo.portal_policy_receipts(portal)[0].source_company_id == "42"
        assert "Approved locally; waiting" in client.get("/admin/partners?profile=customer-db").text
        assert post(client, "stop-company-policy", policy=str(policy.policy_id)).status_code == 200
        assert repo.portal_policy_receipts(portal)[0].state == "needs_approval"
        assert repo.administration_snapshot()["partners"] == []
        assert repo.administration_snapshot()["grants"] == []
        audit = app.state.admin_store.recent_audit()
        assert any(str(policy.policy_id) in event["operation"] for event in audit)


def test_partners_displays_companies_from_selected_saved_mapping(admin, company_source):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/admin/partners?profile=customer-db").url.path == "/admin/login"
        login(client)
        content = client.get("/admin/partners?profile=customer-db").text
        assert "Companies from your database" in content
        assert "Company ID in your database" in content
        assert "<td>42</td>" in content
        assert "Example &lt;Carrier&gt;" in content
        assert "Example <Carrier>" not in content
        assert "Second company" in content
        assert "2 mapped companies." in content
        assert 'value="customer-db" selected' in content
        assert "Could not load mapped companies" not in content
        assert 'action="/admin/partner"' in content
        assert app.state.repository.administration_snapshot()["partners"] == []
        events = app.state.admin_store.recent_events("database")
        event = next(event for event in events if event["event"] == "companies.loaded")
        assert json.loads(event["details"]) == {"rows_shown": 2, "truncated": 0}
        assert "Example" not in json.dumps(events)
    with sqlite3.connect(company_source) as db:
        assert db.execute("SELECT COUNT(*) FROM customer_accounts").fetchone()[0] == 2


def test_broken_optional_portal_pairing_does_not_hide_company_directory(admin, company_source):
    app, _, connection_file = admin
    connection_file.write_text("invalid pairing file", encoding="utf-8")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = client.get("/admin/partners?profile=customer-db")
        assert response.status_code == 200
        assert "Cannot load sharing approvals" in response.text
        assert "Second company" in response.text and "<td>42</td>" in response.text
        assert 'action="/admin/partner"' in response.text
        assert 'action="/admin/approve-company-policy"' not in response.text
        assert any(
            event["event"] == "policies.load_failed.invalid_configuration"
            for event in app.state.admin_store.recent_events("portal")
        )


def test_partners_uses_selected_connection_and_does_not_guess_one(admin, company_source):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        content = client.get("/admin/partners").text
        assert "Select a database connection" in content
        assert "Second company" not in content
        app.state.admin_store.save("source", {"name": "customer-db"})
        content = client.get("/admin/partners").text
        assert "Second company" in content


@pytest.mark.parametrize("columns", [{}, {"source_id": "account_key"}, {"name": "display_label"}])
def test_partners_requires_only_the_companies_mapping(admin, company_source, columns):
    app, _, _ = admin
    app.state.repository.put_source_database_profile(
        name="customer-db",
        driver="sqlite",
        database_name=str(company_source),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=json.dumps(
            {"resources": {"companies": {"table": "customer_accounts", "columns": columns}}}
            if columns
            else {"resources": {}}
        ),
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = client.get("/admin/partners?profile=customer-db")
        assert response.status_code == 200
        assert "Save a Companies mapping" in response.text
        assert 'href="/admin/mapping?profile=customer-db"' in response.text
        assert "Second company" not in response.text


def test_partners_empty_company_directory_is_not_an_error(admin, company_source):
    app, _, _ = admin
    with sqlite3.connect(company_source) as db:
        db.execute("DELETE FROM customer_accounts")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        content = client.get("/admin/partners?profile=customer-db").text
        assert "No companies were returned" in content
        assert "Could not load" not in content


@pytest.mark.parametrize("failure", ["missing-profile", "query", "invalid-company"])
def test_partners_company_errors_are_visible_and_logged(admin, company_source, failure):
    app, _, _ = admin
    if failure == "query":
        with sqlite3.connect(company_source) as db:
            db.execute("DROP TABLE customer_accounts")
    elif failure == "invalid-company":
        with sqlite3.connect(company_source) as db:
            db.execute("UPDATE customer_accounts SET display_label = NULL WHERE account_key = 42")
    profile = "missing" if failure == "missing-profile" else "customer-db"
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = client.get("/admin/partners?profile=" + profile)
        assert response.status_code == 200
        assert "Could not load mapped companies" in response.text
        assert "Diagnostic reference:" in response.text
        assert 'href="/admin/logs"' in response.text
        assert 'action="/admin/partner"' in response.text
        events = app.state.admin_store.recent_events("database")
        event = next(
            event for event in events if event["event"].startswith("companies.load_failed.")
        )
        assert event["reference"] == response.headers["X-Request-ID"]


def test_partners_company_directory_has_explicit_row_limit(admin, company_source):
    app, _, _ = admin
    with sqlite3.connect(company_source) as db:
        db.executemany(
            "INSERT INTO customer_accounts VALUES (?, ?)",
            [(i, f"Company number {i}") for i in range(1000, 1200)],
        )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        content = client.get("/admin/partners?profile=customer-db").text
        assert "Showing companies 1&ndash;100" in content
        assert "Company number 1097" in content
        assert "Company number 1098" not in content
        assert "Next companies" in content
        second = client.get("/admin/partners?profile=customer-db&offset=100").text
        assert "Company number 1098" in second and "Company number 1197" in second
        assert "Company number 1198" not in second
        last = client.get("/admin/partners?profile=customer-db&offset=200").text
        assert "Company number 1199" in last and "Next companies" not in last
        assert "Previous companies" in last
        events = app.state.admin_store.recent_events("database")
        event = next(
            event
            for event in events
            if event["event"] == "companies.loaded" and json.loads(event["details"])["truncated"]
        )
        assert json.loads(event["details"]) == {"rows_shown": 100, "truncated": 1}


@pytest.mark.parametrize("query", ["Second", "77"])
def test_company_picker_searches_mapped_names_and_ids(admin, company_source, query):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/partners", params={"profile": "customer-db", "q": query})
        assert page.status_code == 200
        assert "Second company" in page.text and "Example &lt;Carrier&gt;" not in page.text


def test_company_picker_search_reaches_beyond_first_page(admin, company_source):
    app, _, _ = admin
    with sqlite3.connect(company_source) as db:
        db.executemany(
            "INSERT INTO customer_accounts VALUES (?, ?)",
            [(i, f"Customer {i}") for i in range(1000, 1200)],
        )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/partners", params={"profile": "customer-db", "q": "1199"})
        assert "Customer 1199" in page.text and "Customer 1198" not in page.text


@pytest.mark.parametrize(
    "params",
    [
        {"offset": "-1"},
        {"offset": "invalid"},
        {"offset": "1000000"},
        {"q": "x" * 121},
    ],
)
def test_company_picker_invalid_search_is_visible_and_logged(admin, company_source, params):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/partners", params={"profile": "customer-db", **params})
        assert page.status_code == 400 and 'role="alert"' in page.text
        assert "Invalid company search" in page.text
        assert any(
            event["event"] == "companies.invalid_search"
            for event in app.state.admin_store.recent_events("admin")
        )


def test_company_picker_search_is_literal_and_pagination_preserves_filter(admin, company_source):
    app, _, _ = admin
    with sqlite3.connect(company_source) as db:
        db.executemany(
            "INSERT INTO customer_accounts VALUES (?, ?)",
            [(i, f"Customer %_! {i}") for i in range(1000, 1200)],
        )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/partners", params={"profile": "customer-db", "q": "%_!"})
        assert "Customer %_! 1000" in page.text and "Second company" not in page.text
        assert "q=%25_%21&amp;offset=100" in page.text
        next_page = client.get(
            "/admin/partners", params={"profile": "customer-db", "q": "%_!", "offset": 100}
        )
        assert "Customer %_! 1199" in next_page.text and "Customer %_! 1000" not in next_page.text
        empty = client.get(
            "/admin/partners", params={"profile": "customer-db", "q": "no-matching-company"}
        )
        assert "No companies match this search." in empty.text


def test_authentication_csrf_host_and_reset(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/admin/setup").url.path == "/admin/login"
        assert client.post("/admin/identity", data={"name": "Unauthorized"}).status_code == 403
        assert client.get("/admin/login", headers={"Host": "attacker.test"}).status_code == 400
        login(client)
        for section in (
            "setup",
            "identity",
            "portal",
            "sources",
            "mapping",
            "partners",
            "remote",
            "diagnostics",
            "account",
        ):
            response = client.get("/admin/" + section)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "same-origin"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "operator-password" not in response.text
        assert (
            client.post(
                "/admin/identity",
                data={"csrf": csrf(client), "name": "Attack"},
                headers={"Origin": "https://attacker.test"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/admin/identity",
                data={"csrf": csrf(client), "name": "Attack"},
                headers={"Origin": "null"},
            ).status_code
            == 403
        )
        app.state.admin_store.set_password("operator", "replacement-password", reset=True)
        assert client.get("/admin/setup").url.path == "/admin/login"
        login(client, "replacement-password")
        assert (
            post(
                client,
                "password",
                old="replacement-password",
                password="new-password-10",
                confirm="new-password-10",
            ).url.path
            == "/admin/login"
        )
        login(client, "new-password-10")
        assert post(client, "logout").url.path == "/admin/login"


def test_page_css_is_inside_style_element(admin):
    from html.parser import HTMLParser

    class PageMarkup(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_style = False
            self.styles = []
            self.visible = []

        def handle_starttag(self, tag, attrs):
            if tag == "style":
                self.in_style = True

        def handle_endtag(self, tag):
            if tag == "style":
                self.in_style = False

        def handle_data(self, data):
            (self.styles if self.in_style else self.visible).append(data)

    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        responses = [client.get("/admin/login")]
        responses.append(
            client.post(
                "/admin/login",
                data={"username": "operator", "password": "wrong", "csrf": csrf(client, "login")},
                headers={"Origin": BASE},
            )
        )
        assert responses[-1].status_code == 401
        login(client)
        responses.append(client.get("/admin/setup"))
    for response in responses:
        markup = PageMarkup()
        markup.feed(response.text)
        assert "body{margin:0" in "".join(markup.styles)
        assert "body{margin:0" not in "".join(markup.visible)


@pytest.mark.parametrize(
    ("source_state", "expected_status"),
    [
        ("unselected", "Not selected"),
        ("missing", "Profile missing"),
        ("disabled", "Profile disabled"),
        ("selected", "Selected"),
    ],
)
def test_setup_dashboard_status_and_actions(admin, tmp_path, source_state, expected_status):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        initial = client.get("/admin/setup").text.split("<h2>Setup</h2>", 1)[1]
        assert "Get started" in initial
        assert initial.count('<li class="setup-card') == 6
        assert "setup-hero" not in initial
        assert "<aside" not in initial
        assert "Get connected. Stay in control." not in initial
        assert "Set up your connections" in initial
        assert initial.index("Carrier Portal (optional)") < initial.index("Verify your setup")
        assert initial.index("Verify your setup") < initial.index("Choose what to share")
        assert initial.index("/admin/partners") > initial.index('action="/admin/readiness"')
        assert post(client, "identity", name="Carrier <script>").status_code == 200
        if source_state in {"selected", "disabled"}:
            assert (
                post(
                    client,
                    "source",
                    name="company-db",
                    driver="sqlite",
                    database=str(tmp_path / "company.db"),
                    enabled="false" if source_state == "disabled" else "true",
                ).status_code
                == 200
            )
        if source_state != "unselected":
            app.state.admin_store.save("source", {"name": "company-db"})
        response = client.get("/admin/setup")
        content = response.text.split("<h2>Setup</h2>", 1)[1]
        assert response.status_code == 200
        assert expected_status in content
        assert "Carrier &lt;script&gt;" in content
        assert "Carrier <script>" not in content
        assert "Configured" in content
        assert 'action="/admin/readiness"' in content
        assert "saved source profile is not a health check" in content
        assert "Review field mapping" in content
        assert content.index("/admin/mapping") < content.index("/admin/portal")
        assert 'href="/admin/partners"' in content
        assert "Connect to Carrier Portal" in content
        assert "Not required for direct partner sharing" in content
        assert 'href="/admin/sources"' in content
        assert 'href="/admin/remote"' in content
        assert "NetTool" not in content
        assert content.index("/admin/portal") < content.index('action="/admin/readiness"')
        assert ">Ready<" not in content


def test_admin_database_wording_and_new_profile_default(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        for section in ("setup", "sources", "mapping", "portal"):
            response = client.get("/admin/" + section)
            assert response.status_code == 200
            assert "nettool" not in response.text.lower()
        connections = client.get("/admin/sources")
        assert "<h2>Connections</h2>" in connections.text
        assert 'name="name" type="text" value="database"' in connections.text


def test_database_form_secret_save_reload_and_environment_precedence(
    admin, monkeypatch, tmp_path, capsys
):
    from open_carrier_portal import cli
    from open_carrier_portal.source_credentials import credential_path, read_password
    from open_carrier_portal.source_database import SourceDatabase

    app, settings, _ = admin
    monkeypatch.delenv("OCP_SOURCE_DB_PASSWORD", raising=False)
    data = {
        "name": "database",
        "driver": "mysql",
        "database": "company",
        "host": "localhost",
        "port": "3306",
        "username": "reader",
        "password": " private-password $ ",
        "connection_form": "true",
    }
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/sources")
        assert 'name="password"' in page.text
        assert "Help creating a read-only account" not in page.text
        assert "CREATE USER" not in page.text
        assert 'formaction="/admin/test-connection"' in page.text
        assert post(client, "source", **data).status_code == 200
        profile = app.state.repository.get_source_database_profile("database")
        variable = profile.password_env_var
        assert read_password(settings.database_path, variable) == data["password"]
        assert credential_path(settings.database_path, variable).exists()
        assert data["password"] not in client.get("/admin/sources").text
        assert data["password"] not in client.get("/admin/diagnostics").text
        assert data["password"].encode() not in settings.database_path.read_bytes()
        data["password"] = ""
        assert post(client, "source", **data).status_code == 200
        assert read_password(settings.database_path, variable) == " private-password $ "
        restarted = create_app(settings, admin_base_url=BASE, connection_file=tmp_path / "portal")
        assert restarted.state.source_database is None
        app.state.admin_store.save("source", {"name": "database"})
        restarted = create_app(settings, admin_base_url=BASE, connection_file=tmp_path / "portal")
        assert restarted.state.source_database._settings.password == " private-password $ "
        monkeypatch.setenv(variable, "environment-secret")
        restarted = create_app(settings, admin_base_url=BASE, connection_file=tmp_path / "portal")
        assert restarted.state.source_database._settings.password == "environment-secret"
        page = client.get("/admin/sources").text
        assert "Password managed by server configuration" in page
        assert 'name="password"' not in page
        assert "environment-secret" not in page
        data["password"] = "attempted-override"
        assert post(client, "source", **data).status_code == 400
        assert read_password(settings.database_path, variable) == " private-password $ "
        monkeypatch.delenv(variable)
        calls = []
        monkeypatch.setattr(SourceDatabase, "health_check", lambda self: calls.append(self))
        data["password"] = "test-only-secret"
        assert post(client, "test-connection", **data).status_code == 200
        assert calls[0]._settings.password == "test-only-secret"
        assert read_password(settings.database_path, variable) == " private-password $ "
        monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", settings.token_signing_key)
        monkeypatch.setenv("OCP_DATABASE_PATH", str(settings.database_path))
        monkeypatch.setattr("sys.argv", ["ocp", "test-source", "--name", "database"])
        monkeypatch.setattr(SourceDatabase, "validate_mapping", lambda self: None)
        cli.main()
        assert calls[-1]._settings.password == " private-password $ "
        assert '"status": "ok"' in capsys.readouterr().out


def test_database_form_failure_does_not_save_password_or_profile(admin):
    from open_carrier_portal.source_credentials import credential_path

    app, settings, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = post(
            client,
            "source",
            name="invalid",
            driver="mysql",
            database="company",
            host="10.1.1.1",
            port="3306",
            username="reader",
            password="private-secret",
            connection_form="true",
        )
        assert response.status_code == 400
        assert app.state.repository.get_source_database_profile("invalid") is None
        assert not credential_path(settings.database_path, "unused").parent.exists()


def test_setup_dashboard_does_not_treat_stale_portal_status_as_connected(admin):
    app, _, _ = admin
    app.state.admin_store.save("heartbeat", {"seen": str(time.time())})
    with TestClient(app, base_url=BASE) as client:
        login(client)
        content = client.get("/admin/setup").text.split("<h2>Setup</h2>", 1)[1]
        assert "Not connected" in content
        assert ">Connected<" not in content


def test_cli_password_reset_applies_to_running_private_http_app(tmp_path, monkeypatch, capsys):
    from open_carrier_portal import cli

    origin = "http://10.210.4.223:8000"
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setenv("OCP_ISSUER_URL", origin)
    monkeypatch.setenv("OCP_TEST_MODE", "true")
    monkeypatch.setattr(cli, "getpass", lambda _: "original-password")
    monkeypatch.setattr("sys.argv", ["ocp", "create-admin", "--username", "admin"])
    cli.main()
    settings = Settings.from_env()
    app = create_app(settings, admin_base_url=origin, connection_file=tmp_path / "portal.json")
    with TestClient(app, base_url=origin) as client:
        monkeypatch.setattr(cli, "getpass", lambda _: "replacement-password")
        monkeypatch.setattr("sys.argv", ["ocp", "reset-admin-password", "--username", "admin"])
        cli.main()
        result = client.post(
            "/admin/login",
            data={
                "username": "admin",
                "password": "replacement-password",
                "csrf": csrf(client, "login"),
            },
            headers={"Origin": origin},
            follow_redirects=False,
        )
        assert result.status_code == 303
        assert client.get("/admin/setup").status_code == 200
    output = capsys.readouterr().out
    assert "saved" in output
    assert str(settings.database_path.resolve()) in output


def test_login_throttled_and_no_public_bootstrap(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        for _ in range(5):
            response = client.post(
                "/admin/login",
                data={
                    "username": "operator",
                    "password": "bad",
                    "csrf": csrf(client, "login"),
                },
            )
            assert response.status_code == 401
        response = client.post(
            "/admin/login",
            data={
                "username": "operator",
                "password": "bad",
                "csrf": csrf(client, "login"),
            },
        )
        assert response.status_code == 400
        assert "Too many" in response.text
        assert client.post("/admin/create-admin", data={}).status_code == 403


def test_identity_and_direct_sharing_without_portal(admin):
    app, settings, _ = admin
    repo: Repository = app.state.repository
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert post(client, "identity", name="Carrier <script>").status_code == 200
        identity = repo.get_connector_identity()
        assert post(client, "identity", name="Carrier").status_code == 200
        assert repo.get_connector_identity().connector_id == identity.connector_id
        assert (
            post(
                client, "partner", name="Partner", scope="circuits:read", consent="yes"
            ).status_code
            == 400
        )
        response = post(
            client,
            "partner",
            name="Partner",
            scope="circuits:read",
            all_resources="yes",
            consent="yes",
        )
        assert response.status_code == 200
        repo_state = repo.administration_snapshot()
        partner_id = repo_state["partners"][0]["partner_id"]
        client_id = repo_state["clients"][0]["client_id"]
        credential = re.search(r"<td>([A-Za-z0-9_-]{43})</td>", response.text).group(1)
        assert verify_client_secret(credential, repo.get_active_client(client_id).secret_hash)
        api = create_app(settings)
        with TestClient(api) as partner_api:
            token_response = partner_api.post(
                "/oauth/token",
                auth=(client_id, credential),
                data={"grant_type": "client_credentials", "scope": "circuits:read"},
            )
            assert token_response.status_code == 200
            access_token = token_response.json()["access_token"]
        assert credential not in client.get("/admin/partners").text
        assert credential not in client.get("/admin/diagnostics").text
        assert (
            post(
                client, "share", partner=partner_id, circuit="123", visibility="endpoints"
            ).status_code
            == 200
        )
        assert (
            repo.circuit_share_visibility(partner_id=partner_id, source_circuit_id="123")
            == "endpoints"
        )
        assert post(client, "revoke-partner", partner=partner_id).status_code == 200
        with TestClient(api) as partner_api:
            assert (
                partner_api.post(
                    "/oauth/token",
                    auth=(client_id, credential),
                    data={"grant_type": "client_credentials", "scope": "circuits:read"},
                ).status_code
                == 401
            )
            assert (
                partner_api.get(
                    "/v1/circuits/circuit_" + "a" * 24 + "/path",
                    headers={"Authorization": f"Bearer {access_token}"},
                ).status_code
                == 401
            )
        with pytest.raises(AuthenticationError):
            repo.get_active_client(client_id)
        with pytest.raises(AuthorizationError):
            repo.authorize(
                client_id=client_id,
                partner_id=partner_id,
                token_scopes=frozenset({"circuits:read"}),
                required_scope="circuits:read",
            )


def test_sources_mapping_preview_and_api_activation(admin, tmp_path: Path):
    app, settings, _ = admin
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE circuits (id TEXT, name TEXT)")
        db.executemany(
            "INSERT INTO circuits VALUES (?, ?)", [(str(i), "<script>") for i in range(10)]
        )
    mapping = json.dumps(
        {
            "resources": {
                "circuits": {
                    "table": "circuits",
                    "columns": {"source_id": "id", "description": "name"},
                }
            }
        }
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert (
            post(client, "source", name="custom", driver="sqlite", database=str(source)).status_code
            == 200
        )
        assert json.loads(
            app.state.repository.get_source_database_profile("custom").mapping_json
        ) == {"resources": {}}
        catalog = client.get("/admin/mapping/data?profile=custom").json()
        assert catalog["mapping"] == {"resources": {}}
        assert len(catalog["target_tables"]) == 13
        discovered = post(client, "schema", name="custom")
        assert discovered.status_code == 200
        assert "circuits" in discovered.text
        assert post(client, "mapping", name="custom", mapping=mapping).status_code == 200
        assert "Source table" in client.get("/admin/mapping?profile=custom").text
        assert (
            post(
                client,
                "mapping-fields",
                name="custom",
                table__circuits="circuits",
                column__circuits__source_id="id",
                column__circuits__description="name",
            ).status_code
            == 200
        )
        assert post(client, "test-source", name="custom").status_code == 200
        preview = post(client, "preview", name="custom", resource="circuits")
        assert preview.status_code == 200
        assert preview.text.count("&lt;script&gt;") == 5
        assert (
            post(
                client, "mapping", name="custom", mapping=mapping.replace("circuits", "missing")
            ).status_code
            == 400
        )
        assert json.loads(
            app.state.repository.get_source_database_profile("custom").mapping_json
        ) == json.loads(mapping)
        assert (
            post(
                client,
                "source",
                name="custom",
                driver="sqlite",
                database=str(source),
                enabled="false",
            ).status_code
            == 200
        )
        assert (
            post(client, "source", name="custom", driver="sqlite", database=str(source)).status_code
            == 200
        )
        assert not app.state.repository.get_source_database_profile("custom").enabled
        assert json.loads(
            app.state.repository.get_source_database_profile("custom").mapping_json
        ) == json.loads(mapping)
        assert "Profile disabled" in client.get("/admin/sources?profile=custom").text
        assert post(client, "test-source", name="custom").status_code == 400
        assert (
            post(
                client,
                "source",
                name="custom",
                driver="sqlite",
                database=str(source),
                enabled="true",
            ).status_code
            == 200
        )
        assert post(client, "activate", name="custom").status_code == 200
        api = create_app(settings, admin_base_url=BASE)
        assert api.state.source_database is not None
        with TestClient(api, base_url=BASE) as api_client:
            assert api_client.get("/admin/setup").url.path == "/admin/login"
            login(api_client)
            assert api_client.get("/admin/mapping").status_code == 200
            assert api_client.get("/health/live").json() == {"status": "ok"}


@pytest.mark.parametrize("monitoring", [False, True])
def test_mapper_and_readiness_agree_on_required_relationships(admin, tmp_path, monitoring):
    from npdb_fixture import create_npdb

    from open_carrier_portal.source_database import SourceMapping

    app, _, _ = admin
    database = tmp_path / "inventory.db"
    create_npdb(database)
    mapping = json.loads(SourceMapping.sharing_defaults().to_json())
    for resource in ("devices", "ports"):
        del mapping["resources"][resource]["columns"]["observium_id"]
    if monitoring:
        app.state.admin_store.save(
            "observium", {"url": "https://monitoring.example", "token_env": "MISSING_TEST_TOKEN"}
        )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert post(client, "identity", name="Carrier").status_code == 200
        assert (
            post(
                client, "source", name="inventory", driver="sqlite", database=str(database)
            ).status_code
            == 200
        )
        saved = post(
            client,
            "mapping/workspace",
            name="inventory",
            mapping=json.dumps(mapping),
            operation="save",
        )
        assert saved.status_code == 200, saved.text
        expected_missing = (
            {"devices": ["observium_id"], "ports": ["observium_id"]} if monitoring else {}
        )
        assert saved.json()["missing_fields"] == expected_missing
        catalog = client.get("/admin/mapping/data?profile=inventory").json()
        assert catalog["monitoring_enabled"] is monitoring
        for resource in ("devices", "ports"):
            assert "observium_id" in catalog["basic_fields"][resource]
            assert ("observium_id" in catalog["required_fields"][resource]) is monitoring
        for resource, field in (
            ("ports", "device_id"),
            ("devices", "rack_id"),
            ("racks", "location_id"),
            ("circuits", "circuit_layout_id"),
        ):
            assert field in catalog["required_fields"][resource]
            assert field in catalog["basic_fields"][resource]
        checked = post(client, "test-source", name="inventory")
        assert checked.status_code == 200
        assert ("ports.observium_id" in checked.text) is monitoring
        assert post(client, "activate", name="inventory").status_code == 200
        readiness = post(client, "readiness")
        assert readiness.status_code == (400 if monitoring else 200)
        if monitoring:
            assert "devices.observium_id" in readiness.text
            assert "ports.observium_id" in readiness.text


def test_background_live_reader_uses_service_pairing_and_stops_with_app(admin, monkeypatch):
    import threading

    from open_carrier_portal.models import ConnectorIdentity

    app, _, connection_file = admin
    identity = ConnectorIdentity(
        organization_id=str(uuid4()),
        organization_name="Local company",
        connector_id=str(uuid4()),
        issuer_url=BASE,
    )
    app.state.repository.put_connector_identity(identity)
    connection_file.touch(mode=0o600)
    connection_file.write_text(
        json.dumps(
            {
                "portal_url": "https://portal.example",
                "organization_id": str(uuid4()),
                "connector_id": identity.connector_id,
                "token": "t" * 64,
            }
        )
    )
    calls = []
    seen = threading.Event()
    monkeypatch.setattr("open_carrier_portal.admin.send_heartbeat", lambda *args, **kwargs: None)

    def serve(client, connection, **kwargs):
        assert connection.connector_id == identity.connector_id
        assert kwargs["repository"] is app.state.repository
        assert kwargs["settings_loader"]().database_path == app.state.repository._database_path
        calls.append(connection.connector_id)
        seen.set()
        return False

    monkeypatch.setattr("open_carrier_portal.admin.serve_live_request", serve)
    with TestClient(app, base_url=BASE):
        assert seen.wait(2)
    count = len(calls)
    time.sleep(0.3)
    assert len(calls) == count


def test_combined_connector_admin_api_and_lifespan(admin, monkeypatch):
    import threading
    from unittest.mock import AsyncMock, MagicMock

    from open_carrier_portal.adapters.observium import ObserviumAdapter

    _, settings, connection_file = admin
    heartbeat_seen = threading.Event()
    closed = AsyncMock()

    adapter = MagicMock(spec=ObserviumAdapter)
    adapter.close = closed

    monkeypatch.setattr(
        "open_carrier_portal.admin.send_heartbeat",
        lambda client, connection, **kwargs: heartbeat_seen.set(),
    )
    app = create_app(
        settings, admin_base_url=BASE, connection_file=connection_file, adapter=adapter
    )
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/").url.path == "/admin/login"
        assert client.get("/admin/login", headers={"Host": "attacker.test"}).status_code == 400
        assert client.get("/health/live", headers={"Host": "partner.example"}).status_code == 200
        assert client.get("/v1/devices").status_code == 401
        assert client.post("/admin/identity", data={"name": "Attack"}).status_code == 403
        login(client)
        assert post(client, "identity", name="Carrier").status_code == 200
        identity = app.state.repository.get_connector_identity()
    closed.assert_awaited_once()
    connection_file.touch(mode=0o600)
    connection_file.write_text(
        json.dumps(
            {
                "portal_url": "https://portal.example",
                "organization_id": str(uuid4()),
                "connector_id": identity.connector_id,
                "token": "t" * 64,
            }
        ),
        encoding="utf-8",
    )
    # A restart must start the mounted UI's heartbeat lifespan as well as the API.
    with TestClient(app, base_url=BASE):
        assert heartbeat_seen.wait(timeout=5)
    assert closed.await_count == 2


@pytest.mark.parametrize("failure", ["missing-profile", "missing-observium-token", "missing-ca"])
def test_combined_connector_configuration_failure_keeps_admin_available(admin, failure, caplog):
    _, settings, connection_file = admin
    store = AdminStore(settings.database_path)
    if failure == "missing-profile":
        store.save("source", {"name": "nonexistent"})
    elif failure == "missing-observium-token":
        store.save(
            "observium",
            {"url": "https://monitoring.example", "token_env": "MISSING_TEST_OBSERVIUM_TOKEN"},
        )
    else:
        from dataclasses import replace

        settings = replace(
            settings,
            observium_base_url="https://monitoring.example",
            observium_api_token="private-monitoring-token",
            observium_ca_bundle=str(settings.database_path.parent / "missing-ca.pem"),
        )
    app = create_app(settings, admin_base_url=BASE, connection_file=connection_file)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = client.get("/admin/setup")
        assert response.status_code == 200
        assert "Integration configuration is invalid" in response.text
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
    assert "Integration configuration is invalid" in caplog.text


@pytest.mark.parametrize(
    ("portal_url", "insecure"),
    [("https://portal.example", False), ("http://10.210.4.224:8080", True)],
)
def test_pairing_heartbeat_and_local_disconnect(admin, monkeypatch, portal_url, insecure):
    from dataclasses import replace

    _, settings, connection_file = admin
    app = create_admin_app(
        replace(
            settings,
            portal_url=portal_url,
            portal_ca_bundle="trusted-portal-ca.pem",
            allow_insecure_http=insecure,
        ),
        connection_file=connection_file,
    )
    organization = str(uuid4())
    requests = []
    cas = []

    def handler(request: httpx.Request):
        assert str(request.url).startswith(portal_url + "/api/connectors/")
        requests.append(request.url.path)
        if request.url.path.endswith("/pair"):
            return httpx.Response(200, json={"organization_id": organization, "token": "t" * 64})
        assert json.loads(request.content)["policy_version"] == 1
        return httpx.Response(
            200, json={"status": "connected", "company_policies": {"version": 1, "policies": []}}
        )

    def client_for(ca=""):
        cas.append(ca)
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("open_carrier_portal.admin.http_client", client_for)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        post(client, "identity", name="Carrier")
        page = client.get("/admin/portal")
        assert "Connect to Carrier Portal" in page.text
        assert f'href="{portal_url}/portal/connector"' in page.text
        assert ("Private-network HTTP testing enabled" in page.text) == insecure
        assert 'name="url"' not in page.text
        assert 'name="ca"' not in page.text
        assert "15 minutes" in page.text
        assert 'name="code"' in page.text
        assert (
            post(
                client, "pair", url="https://attacker.test", ca="untrusted.pem", code="c" * 43
            ).status_code
            == 200
        )
        assert (
            PortalConnection.load(connection_file, allow_insecure_http=insecure).organization_id
            == organization
        )
        assert "Paired &mdash; waiting for a heartbeat" in client.get("/admin/portal").text
        assert 'name="code"' not in client.get("/admin/portal").text
        assert post(client, "heartbeat").status_code == 200
        assert "seen" in app.state.admin_store.get("heartbeat")
        connected = client.get("/admin/portal")
        assert "Connected" in connected.text
        assert "t" * 64 not in connected.text
        assert "trusted-portal-ca.pem" not in connected.text
        assert post(client, "disconnect").status_code == 200
        assert not connection_file.exists()
        assert post(client, "heartbeat").status_code == 400
        assert "Not connected" in client.get("/admin/portal").text
    assert "/api/connectors/heartbeat" in requests
    assert cas and all(ca == "trusted-portal-ca.pem" for ca in cas)


def test_portal_pairing_requires_installer_configuration_and_identity(admin):
    from dataclasses import replace

    app, settings, connection_file = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        setup = client.get("/admin/setup").text.split("<h2>Setup</h2>", 1)[1]
        assert setup.index("/admin/mapping") < setup.index("/admin/portal")
        assert "Connect to Carrier Portal" in setup
        assert post(client, "identity", name="Carrier").status_code == 200
        page = client.get("/admin/portal").text
        assert "Portal connection is not configured yet" in page
        assert 'name="code"' not in page
        assert post(client, "pair", url="https://attacker.test", code="c" * 43).status_code == 400
        assert not connection_file.exists()
    configured = create_admin_app(
        replace(settings, portal_url="https://portal.example"),
        connection_file=connection_file,
    )
    repo = configured.state.repository
    repo.put_connector_identity(repo.get_connector_identity().model_copy(update={"enabled": False}))
    with TestClient(configured, base_url=BASE) as client:
        login(client)
        assert "Set up your company identity" in client.get("/admin/portal").text
        assert post(client, "pair", code="c" * 43).status_code == 400


def test_remote_partner_oauth_and_circuit_query(admin, monkeypatch):
    app, _, _ = admin
    monkeypatch.setenv("PARTNER_TEST_SECRET", "private-secret")

    def handler(request: httpx.Request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "remote-access-token"})
        assert request.headers["Authorization"] == "Bearer remote-access-token"
        return httpx.Response(200, json={"result": "authorized"})

    monkeypatch.setattr(
        "open_carrier_portal.admin.http_client",
        lambda ca="": httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert (
            post(
                client,
                "remote",
                url="https://partner.example",
                client="ocp_client",
                secret_env="PARTNER_TEST_SECRET",
                scope="circuits:read",
            ).status_code
            == 200
        )
        assert post(client, "remote-test").status_code == 200
        response = post(client, "remote-circuit", circuit="circuit_" + "a" * 24)
        assert response.status_code == 200
        assert "authorized" in response.text
        assert "private-secret" not in response.text
        assert "remote-access-token" not in response.text
        assert post(client, "remote-circuit", circuit="../../admin").status_code == 400


def test_store_duplicate_and_reset_missing(tmp_path: Path):
    store = AdminStore(tmp_path / "test.db")
    store.initialize()
    with pytest.raises(ValueError, match="not found"):
        store.set_password("missing", "long-enough-password", reset=True)
    store.set_password("admin", "long-enough-password")
    with pytest.raises(ValueError, match="already exists"):
        store.set_password("admin", "long-enough-password")
    token, _ = store.login("admin", "long-enough-password", "test-client")
    assert store.session(token) is not None
    with store.connection() as db:
        db.execute("UPDATE local_admin_sessions SET expires = 0")
    assert store.session(token) is None


def test_authenticated_navigation_renews_server_session_and_cookie(admin, monkeypatch):
    from types import SimpleNamespace

    app, _, _ = admin
    clock = [time.time()]
    monkeypatch.setattr(
        "open_carrier_portal.admin_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        clock[0] += 3500
        response = client.get("/admin/mapping")
        assert response.status_code == 200
        assert "Max-Age=3600" in response.headers.get("set-cookie", "")
        with app.state.admin_store.connection() as db:
            assert db.execute("SELECT expires FROM local_admin_sessions").fetchone()[0] == (
                clock[0] + 3600
            )
        clock[0] += 3500
        assert client.get("/admin/setup", follow_redirects=False).status_code == 200


def test_session_activity_requires_valid_csrf_and_never_revives_expired_sessions(
    admin, monkeypatch
):
    from types import SimpleNamespace

    app, _, _ = admin
    clock = [time.time()]
    monkeypatch.setattr(
        "open_carrier_portal.admin_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        value = csrf(client)
        clock[0] += 3500
        response = client.post("/admin/session", headers={"Origin": BASE, "X-CSRF-Token": value})
        assert response.status_code == 200, response.text
        assert response.json()["idle_timeout_seconds"] == 3600
        assert "Max-Age=3600" in response.headers["set-cookie"]
        clock[0] += 3500
        with app.state.admin_store.connection() as db:
            expires = db.execute("SELECT expires FROM local_admin_sessions").fetchone()[0]
        for headers in (
            {"Origin": BASE},
            {"Origin": BASE, "X-CSRF-Token": "invalid"},
            {"Origin": "https://attacker.test", "X-CSRF-Token": value},
        ):
            rejected = client.post("/admin/session", headers=headers)
            assert rejected.status_code == 403
            assert "set-cookie" not in rejected.headers
        # A status check and asset polling are not user activity.
        status = client.get("/admin/session")
        assert status.status_code == 200
        assert status.json()["csrf"] == value
        assert status.headers["cache-control"] == "no-store"
        assert "set-cookie" not in status.headers
        client.get("/admin/assets/session.js")
        with app.state.admin_store.connection() as db:
            assert db.execute("SELECT expires FROM local_admin_sessions").fetchone()[0] == expires
        clock[0] = expires
        expired = client.post("/admin/session", headers={"Origin": BASE, "X-CSRF-Token": value})
        assert expired.status_code == 401
        assert "set-cookie" not in expired.headers
        assert client.get("/admin/session").status_code == 401
        assert client.get("/admin/setup", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("revoke", ["logout", "password"])
def test_activity_does_not_restore_revoked_sessions(admin, revoke):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        value = csrf(client)
        token = client.cookies.get("ocp_admin")
        if revoke == "logout":
            response = client.post("/admin/logout", data={"csrf": value}, follow_redirects=False)
            assert "Max-Age=0" in response.headers["set-cookie"]
        else:
            app.state.admin_store.set_password("operator", "replacement-password", reset=True)
        client.cookies.set("ocp_admin", token)
        response = client.post("/admin/session", headers={"Origin": BASE, "X-CSRF-Token": value})
        assert response.status_code == 401
        assert app.state.admin_store.session(token) is None


def test_session_activity_script_only_appears_on_signed_in_pages(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        assert "/admin/assets/session.js" not in client.get("/admin/login").text
        login(client)
        response = client.get("/admin/mapping")
        assert 'src="/admin/assets/session.js"' in response.text
        assert 'name="ocp-admin-csrf"' in response.text
        assert 'name="ocp-admin-username" content="operator"' in response.text
        assert 'id="admin-session-notice"' in response.text
        script = client.get("/admin/assets/session.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == "no-cache"


def test_mapping_preview_and_save_renew_only_after_session_verification(admin, tmp_path):
    app, _, _ = admin
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE circuit (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO circuit VALUES (1, 'Shared')")
    mapping = json.dumps(
        {
            "resources": {
                "circuits": {
                    "table": "circuit",
                    "columns": {"source_id": "id", "name": "name"},
                }
            }
        }
    )
    app.state.repository.put_source_database_profile(
        name="sample",
        driver="sqlite",
        database_name=str(database),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=mapping,
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        value = csrf(client)
        for operation in ("preview", "save"):
            result = client.post(
                "/admin/mapping/workspace",
                data={
                    "csrf": value,
                    "name": "sample",
                    "operation": operation,
                    "resource": "circuits",
                    "mapping": mapping,
                },
            )
            assert result.status_code == 200
            assert "Max-Age=3600" in result.headers["set-cookie"]
        rejected = client.post(
            "/admin/mapping/workspace",
            data={
                "csrf": "invalid",
                "name": "sample",
                "operation": "save",
                "mapping": mapping,
            },
        )
        assert rejected.status_code == 403
        assert "set-cookie" not in rejected.headers


def test_session_status_allows_explicit_resume_without_reusing_old_csrf(admin):
    app, _, _ = admin
    with TestClient(app, base_url=BASE) as client:
        login(client)
        old_csrf = csrf(client)
        app.state.admin_store.set_password("operator", "replacement-password", reset=True)
        login(client, "replacement-password")
        status = client.get("/admin/session")
        assert status.status_code == 200
        session = status.json()
        assert session["username"] == "operator"
        assert session["csrf"] != old_csrf
        assert "set-cookie" not in status.headers
        assert (
            client.post(
                "/admin/session", headers={"Origin": BASE, "X-CSRF-Token": old_csrf}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/admin/session", headers={"Origin": BASE, "X-CSRF-Token": session["csrf"]}
            ).status_code
            == 200
        )


def test_admin_cli_bootstrap_and_bind_restrictions(tmp_path: Path, monkeypatch, capsys):
    from open_carrier_portal import cli

    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr("sys.argv", ["ocp", "create-admin", "--username", "operator"])
    monkeypatch.setattr(cli, "getpass", lambda _: "operator-password")
    cli.main()
    assert "saved" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["ocp", "serve-admin", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["serve", "serve-admin"])
def test_cli_serves_api_and_admin_together(tmp_path: Path, monkeypatch, command):
    from open_carrier_portal import cli

    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setattr("sys.argv", ["ocp", command])
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append((app, options)))
    cli.main()
    assert len(started) == 1
    app, options = started[0]
    assert options == {"host": "127.0.0.1", "port": 8000, "access_log": False}
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/admin/setup").url.path == "/admin/login"
        assert client.get("/v1/devices").status_code == 401


@pytest.mark.parametrize(
    ("host", "origin", "allowed"),
    [
        ("10.210.4.223", "http://10.210.4.223:8000", True),
        ("10.210.4.223", "http://10.210.4.224:8000", False),
        ("0.0.0.0", "http://10.210.4.223:8000", False),
        ("8.8.8.8", "http://8.8.8.8:8000", False),
    ],
)
def test_cli_private_network_testing_bind(tmp_path, monkeypatch, host, origin, allowed):
    from open_carrier_portal import cli

    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setenv("OCP_TEST_MODE", "true")
    monkeypatch.setattr("sys.argv", ["ocp", "serve", "--host", host, "--base-url", origin])
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append((app, options)))
    if not allowed:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert not started
        return
    cli.main()
    assert len(started) == 1
    assert started[0][1]["host"] == host


def test_combined_private_http_login_and_api(admin, caplog):
    from dataclasses import replace

    from open_carrier_portal.portal_agent import validate_portal_url

    _, settings, connection_file = admin
    origin = "http://10.210.4.223:8000"
    settings = replace(settings, issuer_url=origin, allow_insecure_http=True)
    app = create_app(settings, admin_base_url=origin, connection_file=connection_file)
    with TestClient(app, base_url=origin) as client:
        login(client)
        assert client.get("/admin/setup").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert client.cookies.get("ocp_admin") is not None
        assert not next(
            cookie for cookie in client.cookies.jar if cookie.name == "ocp_admin"
        ).secure
        assert client.get("/admin/setup", headers={"Host": "attacker.test"}).status_code == 400
        assert (
            client.post(
                "/admin/identity",
                data={"csrf": csrf(client), "name": "Attack"},
                headers={"Origin": "http://attacker.test"},
            ).status_code
            == 403
        )
    assert "not encrypted" in caplog.text
    with pytest.raises(ValueError):
        validate_portal_url("http://10.210.4.224:8080")


def test_observium_config_environment_precedence(admin, monkeypatch):
    from dataclasses import replace

    from open_carrier_portal.admin import configured_settings

    app, settings, _ = admin
    monkeypatch.setenv("UI_OBSERVIUM_TOKEN", "private-monitoring-token")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert (
            post(
                client,
                "observium",
                url="https://monitoring.example/observium",
                token_env="UI_OBSERVIUM_TOKEN",
            ).status_code
            == 200
        )
        assert "private-monitoring-token" not in client.get("/admin/sources").text
        assert (
            post(
                client,
                "observium",
                url="https://user:password@monitoring.example",
                token_env="UI_OBSERVIUM_TOKEN",
            ).status_code
            == 400
        )
    effective = configured_settings(settings, app.state.admin_store)
    assert effective.observium_base_url == "https://monitoring.example/observium"
    assert effective.observium_api_token == "private-monitoring-token"
    explicit = replace(
        settings,
        observium_base_url="https://explicit.example",
        observium_api_token="explicit-token",
    )
    assert configured_settings(explicit, app.state.admin_store) == explicit


def test_observium_page_explains_environment_setup_without_token_input(admin, monkeypatch):
    app, _, _ = admin
    monkeypatch.delenv("OCP_OBSERVIUM_API_TOKEN", raising=False)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        page = client.get("/admin/sources").text
        assert "variable NAME only, not the token" in page
        assert "Token environment-variable NAME (not the API token)" in page
        assert 'name="api_token"' not in page
        assert 'name="token_source"' not in page
        assert "Named token variable: missing" in page
        assert "systemd or Docker" in page
        assert "A .env file is not loaded automatically" in page
        monkeypatch.setenv("OCP_OBSERVIUM_API_TOKEN", "private-test-token")
        page = client.get("/admin/sources").text
        assert "Named token variable: available" in page
        assert "private-test-token" not in page


def test_observium_failures_are_visible_in_diagnostics_with_request_reference(admin, monkeypatch):
    app, _, _ = admin
    monkeypatch.delenv("MISSING_OBSERVIUM_LOG_TOKEN", raising=False)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = post(
            client,
            "observium",
            url="https://monitoring.example",
            token_env="MISSING_OBSERVIUM_LOG_TOKEN",
        )
        assert response.status_code == 400
        reference = response.headers["X-Request-ID"]
        assert reference in response.text
        assert "View Logs" in response.text
        events = app.state.admin_store.recent_events("observium")
        assert events[0]["event"] == "configuration.token_missing"
        assert events[0]["reference"] == reference
        page = client.get("/admin/diagnostics").text
        assert "Observium connection logs" in page
        assert "configuration.token_missing" in page
        assert reference in page


def test_observium_session_block_is_logged_before_any_upstream_attempt(admin, monkeypatch):
    from unittest.mock import MagicMock

    app, _, _ = admin
    factory = MagicMock()
    monkeypatch.setattr("open_carrier_portal.adapters.observium.ObserviumAdapter", factory)
    with TestClient(app, base_url=BASE) as client:
        response = client.post("/admin/test-observium", data={"csrf": "private-stale-csrf"})
    assert response.status_code == 403
    events = app.state.admin_store.recent_events("observium")
    assert events[0]["event"] == "connection.blocked_by_admin_session"
    assert events[0]["reference"] == response.headers["X-Request-ID"]
    assert "private-stale-csrf" not in json.dumps(events)
    factory.assert_not_called()


def test_observium_missing_environment_error_explains_restart_and_never_saves(admin, monkeypatch):
    from open_carrier_portal.source_credentials import credential_path

    app, settings, _ = admin
    monkeypatch.delenv("MISSING_OBSERVIUM_TOKEN", raising=False)
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = post(
            client,
            "observium",
            url="https://monitoring.example",
            token_env="MISSING_OBSERVIUM_TOKEN",
        )
    assert response.status_code == 400
    assert "service&#x27;s environment and restart" in response.text
    assert "Do not paste the token into this form" in response.text
    assert not app.state.admin_store.get("observium")
    assert not credential_path(settings.database_path, "unused").parent.exists()


@pytest.mark.parametrize("action", ["observium", "test-observium"])
def test_observium_stale_token_forms_are_rejected_without_saving(admin, monkeypatch, action):
    from open_carrier_portal.source_credentials import credential_path

    app, settings, _ = admin
    monkeypatch.setenv("OCP_OBSERVIUM_API_TOKEN", "environment-test-token")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = post(
            client,
            action,
            url="https://monitoring.example",
            api_token="submitted-test-token",
            token_env="OCP_OBSERVIUM_API_TOKEN",
            token_source="saved",
        )
    assert response.status_code == 400
    assert "submitted-test-token" not in response.text
    assert not app.state.admin_store.get("observium")
    assert not credential_path(settings.database_path, "unused").parent.exists()


def test_observium_environment_save_never_writes_credentials(admin, monkeypatch):
    from unittest.mock import MagicMock

    app, _, _ = admin
    writer = MagicMock(side_effect=AssertionError("Observium must not save credential files"))
    monkeypatch.setattr("open_carrier_portal.admin.save_password", writer)
    monkeypatch.setenv("OBSERVIUM_TEST_TOKEN", "environment-test-token")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        response = post(
            client,
            "observium",
            url="https://monitoring.example",
            token_env="OBSERVIUM_TEST_TOKEN",
        )
    assert response.status_code == 200, response.text
    assert "No API token was stored" in response.text
    assert app.state.admin_store.get("observium") == {
        "url": "https://monitoring.example",
        "token_env": "OBSERVIUM_TEST_TOKEN",
        "ca": "",
    }
    writer.assert_not_called()


def test_observium_never_falls_back_to_previously_saved_token_file(admin, monkeypatch):
    from open_carrier_portal.admin import configured_settings
    from open_carrier_portal.source_credentials import save_password

    app, settings, _ = admin
    save_password(
        settings.database_path,
        "open-carrier-portal-observium-api-token",
        "old-file-test-token",
    )
    app.state.admin_store.save(
        "observium",
        {
            "url": "https://monitoring.example",
            "token_env": "OBSERVIUM_TEST_TOKEN",
            "token_source": "saved",
            "ca": "",
        },
    )
    monkeypatch.delenv("OBSERVIUM_TEST_TOKEN", raising=False)
    with pytest.raises(ValueError, match="service's environment"):
        configured_settings(settings, app.state.admin_store)
    monkeypatch.setenv("OBSERVIUM_TEST_TOKEN", "environment-test-token")
    assert configured_settings(settings, app.state.admin_store).observium_api_token == (
        "environment-test-token"
    )


def test_observium_environment_restart_and_effective_test_see_ui_setting_changes(
    admin, monkeypatch
):
    from unittest.mock import AsyncMock, MagicMock

    app, settings, connection_file = admin
    adapter = MagicMock()
    adapter.health_check = AsyncMock()
    adapter.close = AsyncMock()
    factory = MagicMock(return_value=adapter)
    monkeypatch.setattr("open_carrier_portal.adapters.observium.ObserviumAdapter", factory)
    monkeypatch.setattr("open_carrier_portal.main.ObserviumAdapter", factory)
    monkeypatch.setenv("FIRST_OBSERVIUM_TOKEN", "first-environment-token")
    monkeypatch.setenv("SECOND_OBSERVIUM_TOKEN", "second-environment-token")
    with TestClient(app, base_url=BASE) as client:
        login(client)
        assert (
            post(
                client,
                "observium",
                url="https://first.example",
                token_env="FIRST_OBSERVIUM_TOKEN",
            ).status_code
            == 200
        )
    restarted = create_app(settings, admin_base_url=BASE, connection_file=connection_file)
    assert restarted.state.settings.observium_api_token == "first-environment-token"
    assert factory.call_args.kwargs["api_token"] == "first-environment-token"
    with TestClient(restarted, base_url=BASE) as client:
        login(client)
        assert (
            "The running API uses environment-configured" not in client.get("/admin/sources").text
        )
        assert (
            post(
                client,
                "observium",
                url="https://second.example",
                token_env="SECOND_OBSERVIUM_TOKEN",
            ).status_code
            == 200
        )
        assert post(client, "test-observium").status_code == 200
        assert factory.call_args.kwargs["api_token"] == "second-environment-token"
        assert factory.call_args.kwargs["base_url"] == "https://second.example"
        assert restarted.state.settings.observium_api_token == "first-environment-token"
