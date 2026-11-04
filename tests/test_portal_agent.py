from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from open_carrier_portal.portal_agent import (
    PortalConnection,
    pair_portal,
    run_agent,
    validate_portal_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "http://localhost.evil.example",
        "https://user:password@portal.example",
        "https://portal.example/path",
        "https://portal.example?token=secret",
        "https://portal.example#fragment",
        "file:///tmp/portal",
        "//portal.example",
    ],
)
def test_portal_url_rejects_unsafe_origins(url: str) -> None:
    with pytest.raises(ValueError):
        validate_portal_url(url)
    with pytest.raises(ValueError):
        validate_portal_url(url, allow_insecure_http=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "https://portal.example",
    ],
)
def test_portal_url_accepts_secure_or_loopback_origins(url: str) -> None:
    assert validate_portal_url(url + "/") == url


@pytest.mark.parametrize("host", ["10.210.4.224", "172.16.0.1", "192.168.1.1", "[fd00::1]"])
def test_portal_private_http_requires_its_own_opt_in(host):
    url = f"http://{host}:8080"
    with pytest.raises(ValueError, match="OCP_TEST_MODE"):
        validate_portal_url(url)
    assert validate_portal_url(url + "/", allow_insecure_http=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8080",
        "http://169.254.1.1:8080",
        "http://100.64.0.1:8080",
        "http://0.0.0.0:8080",
        "http://[::]:8080",
        "http://[fe80::1]:8080",
        "http://portal.internal:8080",
        "http://user:password@10.210.4.224:8080",
        "http://10.210.4.224:8080/portal",
        "http://10.210.4.224:8080?code=private",
        "http://10.210.4.224:8080#fragment",
        "http://10.210.4.224:invalid",
        "http://10.210.4.224:0",
        "10.210.4.224:8080",
    ],
)
def test_portal_http_opt_in_still_rejects_unsafe_origins(url):
    with pytest.raises(ValueError):
        validate_portal_url(url, allow_insecure_http=True)


def test_pair_persists_credentials_and_agent_uses_them(tmp_path: Path) -> None:
    identity, organization = str(uuid4()), str(uuid4())
    token, code = "t" * 64, "c" * 43
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/connectors/pair":
            assert json.loads(request.content) == {"code": code, "connector_id": identity}
            return httpx.Response(200, json={"organization_id": organization, "token": token})
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert json.loads(request.content) == {"connector_id": identity}
        return httpx.Response(200, json={"status": "connected"})

    path = tmp_path / "credentials.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        paired = pair_portal(
            client=client,
            portal_url="https://portal.example",
            connector_id=identity,
            code=code,
            connection_file=path,
        )
        assert PortalConnection.load(path) == paired
        run_agent(client, paired, once=True)
        with pytest.raises(FileExistsError):
            pair_portal(
                client=client,
                portal_url="https://portal.example",
                connector_id=identity,
                code=code,
                connection_file=path,
            )
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 500, 302])
def test_failed_pairing_does_not_leave_credentials(tmp_path: Path, status: int) -> None:
    path = tmp_path / "credentials.json"
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        pair_portal(
            client=client,
            portal_url="https://portal.example",
            connector_id=str(uuid4()),
            code="c" * 43,
            connection_file=path,
        )
    assert not path.exists()


def test_revoked_credential_stops_agent() -> None:
    connection = PortalConnection("https://portal.example", str(uuid4()), str(uuid4()), "t" * 64)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        run_agent(client, connection)


def test_malformed_heartbeat_response_is_not_success() -> None:
    connection = PortalConnection("https://portal.example", str(uuid4()), str(uuid4()), "t" * 64)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "unknown"})
            )
        ) as client,
        pytest.raises(ValueError, match="invalid heartbeat"),
    ):
        run_agent(client, connection, once=True)


def test_private_http_pairing_and_saved_heartbeat_need_current_opt_in(tmp_path, caplog):
    path = tmp_path / "credentials.json"
    organization, connector = str(uuid4()), str(uuid4())
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/pair"):
            return httpx.Response(200, json={"organization_id": organization, "token": "t" * 64})
        return httpx.Response(200, json={"status": "connected"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            pair_portal(
                client=client,
                portal_url="http://10.210.4.224:8080",
                connector_id=connector,
                code="c" * 43,
                connection_file=path,
            )
        assert not requests
        assert not path.exists()
        connection = pair_portal(
            client=client,
            portal_url="http://10.210.4.224:8080",
            connector_id=connector,
            code="c" * 43,
            connection_file=path,
            allow_insecure_http=True,
        )
        assert "without encryption" in caplog.text
        assert PortalConnection.load(path, allow_insecure_http=True) == connection
        saved = json.loads(path.read_text())
        assert set(saved) == {"portal_url", "organization_id", "connector_id", "token"}
        saved["allow_insecure_http"] = True
        path.write_text(json.dumps(saved))
        with pytest.raises(ValueError):
            PortalConnection.load(path)
        with pytest.raises(ValueError):
            run_agent(client, connection, once=True)
        assert len(requests) == 1
        run_agent(client, connection, once=True, allow_insecure_http=True)
        assert len(requests) == 2
        assert all(request.url.host == "10.210.4.224" for request in requests)
    assert "t" * 64 not in caplog.text
    assert "c" * 43 not in caplog.text


def test_cli_private_http_pairing_and_agent_honor_environment(tmp_path, monkeypatch, capsys):
    from open_carrier_portal.cli import main
    from open_carrier_portal.models import ConnectorIdentity
    from open_carrier_portal.repository import Repository

    metadata, path = tmp_path / "metadata.db", tmp_path / "connection.json"
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata))
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_ISSUER_URL", "http://localhost:8000")
    monkeypatch.setenv("OCP_PORTAL_URL", "http://10.210.4.224:8080")
    monkeypatch.setenv("OCP_TEST_MODE", "true")
    repo = Repository(metadata, "s" * 48)
    repo.initialize()
    repo.put_connector_identity(
        ConnectorIdentity(
            organization_id=str(uuid4()),
            organization_name="Test company",
            connector_id=str(uuid4()),
            issuer_url="http://localhost:8000",
        )
    )
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/pair"):
            return httpx.Response(200, json={"organization_id": str(uuid4()), "token": "t" * 64})
        return httpx.Response(200, json={"status": "connected"})

    client_factory = httpx.Client
    monkeypatch.setattr(
        "open_carrier_portal.cli.httpx.Client",
        lambda **kwargs: client_factory(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("open_carrier_portal.cli.getpass", lambda _: "c" * 43)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "pair-portal",
            "--portal-url",
            "http://10.210.4.224:8080",
            "--connection-file",
            str(path),
        ],
    )
    main()
    monkeypatch.setattr(
        sys, "argv", ["ocp", "run-portal-agent", "--connection-file", str(path), "--once"]
    )
    main()
    assert requests == ["/api/connectors/pair", "/api/connectors/heartbeat"]
    monkeypatch.delenv("OCP_PORTAL_URL")
    monkeypatch.setenv("OCP_TEST_MODE", "false")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert len(requests) == 2
    assert "OCP_TEST_MODE" in capsys.readouterr().err
