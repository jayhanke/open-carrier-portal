from __future__ import annotations

import json
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
