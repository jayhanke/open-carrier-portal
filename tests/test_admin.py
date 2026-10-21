from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
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
def admin(tmp_path: Path):
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


def test_cli_password_reset_applies_to_running_private_http_app(tmp_path, monkeypatch, capsys):
    from open_carrier_portal import cli

    origin = "http://10.210.4.223:8000"
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setenv("OCP_ISSUER_URL", origin)
    monkeypatch.setenv("OCP_ALLOW_INSECURE_HTTP", "true")
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
        lambda client, connection: heartbeat_seen.set(),
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


def test_pairing_heartbeat_and_local_disconnect(admin, monkeypatch):
    app, _, connection_file = admin
    organization = str(uuid4())
    requests = []

    def handler(request: httpx.Request):
        requests.append(request.url.path)
        if request.url.path.endswith("/pair"):
            return httpx.Response(200, json={"organization_id": organization, "token": "t" * 64})
        return httpx.Response(200, json={"status": "connected"})

    monkeypatch.setattr(
        "open_carrier_portal.admin.http_client",
        lambda ca="": httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with TestClient(app, base_url=BASE) as client:
        login(client)
        post(client, "identity", name="Carrier")
        assert post(client, "pair", url="https://portal.example", code="c" * 43).status_code == 200
        assert PortalConnection.load(connection_file).organization_id == organization
        assert post(client, "heartbeat").status_code == 200
        assert "seen" in app.state.admin_store.get("heartbeat")
        assert "t" * 64 not in client.get("/admin/portal").text
        assert post(client, "disconnect").status_code == 200
        assert not connection_file.exists()
        assert post(client, "heartbeat").status_code == 400
        assert (
            post(client, "pair", url="http://10.210.4.224:8080", code="c" * 43).status_code == 400
        )
    assert "/api/connectors/heartbeat" in requests


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
    assert options == {"host": "127.0.0.1", "port": 8000}
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
    monkeypatch.setenv("OCP_ALLOW_INSECURE_HTTP", "true")
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
