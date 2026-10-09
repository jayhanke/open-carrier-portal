from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from open_carrier_portal.adapters.base import MonitoringAdapter
from open_carrier_portal.config import Settings
from open_carrier_portal.main import create_app
from open_carrier_portal.models import (
    Alarm,
    CapabilitySet,
    ConnectorIdentity,
    Device,
    Interface,
    MetricSeries,
    Page,
)
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import hash_client_secret
from open_carrier_portal.source_database import (
    ResourceMapping,
    SourceDatabase,
    SourceMapping,
    SQLiteSourceDatabaseSettings,
)

pytestmark = pytest.mark.anyio


class FakeAdapter(MonitoringAdapter):
    async def health_check(self) -> None:
        return None

    async def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            adapter="fake",
            adapter_version="1",
            resources=["devices"],
            metrics=[],
        )

    async def list_devices(self, *, page: int, page_size: int) -> Page[Device]:
        return Page(items=[Device(id="device_allowed", name="router.example")])

    async def get_device(self, device_id: str) -> Device:
        return Device(id=device_id, name="router.example")

    async def list_interfaces(self, *, page: int, page_size: int) -> Page[Interface]:
        return Page(items=[])

    async def get_interface(self, interface_id: str) -> Interface:
        raise NotImplementedError

    async def list_alarms(self, *, page: int, page_size: int) -> Page[Alarm]:
        return Page(items=[])

    async def query_metrics(
        self,
        *,
        resource_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> MetricSeries:
        raise NotImplementedError


def _configured_app(tmp_path: Path) -> tuple[FastAPI, str, str]:
    settings = Settings(
        database_path=tmp_path / "test.db",
        issuer_url="https://connector.example",
        token_audience="ocp",
        token_signing_key="s" * 32,
    )
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    repository.put_connector_identity(
        ConnectorIdentity(
            organization_id="owner",
            organization_name="Owner",
            connector_id="connector",
            issuer_url=settings.issuer_url,
        )
    )
    partner_id = repository.create_partner("Partner")
    relationship_id = repository.create_active_relationship(partner_id)
    repository.create_grant(
        relationship_id,
        "observium:devices:read",
        {"device_allowed"},
    )
    client_secret = "partner-secret"
    client_id = repository.create_client(
        partner_id,
        hash_client_secret(client_secret),
        {"observium:devices:read", "observium:alarms:read"},
    )
    app = create_app(settings, repository=repository, adapter=FakeAdapter())
    return app, client_id, client_secret


def _configured_circuit_app(
    tmp_path: Path,
    *,
    visibility: str | None,
) -> tuple[FastAPI, str, str, Repository, str]:
    settings = Settings(
        database_path=tmp_path / "circuit-metadata.db",
        issuer_url="https://connector.example",
        token_audience="ocp",
        token_signing_key="s" * 32,
    )
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    partner_id = repository.create_partner("Circuit partner")
    relationship_id = repository.create_active_relationship(partner_id)
    repository.create_grant(relationship_id, "circuits:read")
    client_secret = "circuit-partner-secret"
    client_id = repository.create_client(
        partner_id,
        hash_client_secret(client_secret),
        {"circuits:read"},
    )

    source_path = tmp_path / "nettool-circuits.db"
    with sqlite3.connect(source_path) as connection:
        connection.executescript(
            """
            CREATE TABLE LitCircuit (LitCircuitID INTEGER, CircuitLayoutID INTEGER);
            CREATE TABLE CircuitLayout (
                CircuitLayoutID INTEGER, PortA INTEGER, PortZ INTEGER
            );
            CREATE TABLE CircuitLayoutItem (
                CircuitLayoutItemID INTEGER, CircuitLayoutID INTEGER,
                ItemIndex INTEGER, TableName TEXT, TablePK INTEGER
            );
            CREATE TABLE Port (PortID INTEGER, DeviceID INTEGER, Name TEXT);
            CREATE TABLE Patch (
                PatchID INTEGER, PatchType TEXT, PortID_A INTEGER, PortID_B INTEGER
            );
            CREATE TABLE PseudoWire (
                PseudoWireID INTEGER, PortAID INTEGER, PortBID INTEGER
            );
            INSERT INTO LitCircuit VALUES (926, 42);
            INSERT INTO CircuitLayout VALUES (42, 101, 202);
            INSERT INTO CircuitLayoutItem VALUES
                (1, 42, 10, 'Port', 101),
                (2, 42, 20, 'Patch', 501),
                (3, 42, 30, 'PW', 601),
                (4, 42, 40, 'Port', 202);
            INSERT INTO Port VALUES
                (101, 301, 'xe-0/0/1'),
                (202, 302, 'xe-0/0/2');
            INSERT INTO Patch VALUES (501, 'fiber', 101, 202);
            INSERT INTO PseudoWire VALUES (601, 101, 202);
            """
        )
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "LitCircuit",
                {"source_id": "LitCircuitID", "circuit_layout_id": "CircuitLayoutID"},
            ),
            "circuit_layouts": ResourceMapping(
                "CircuitLayout",
                {
                    "source_id": "CircuitLayoutID",
                    "port_a_id": "PortA",
                    "port_z_id": "PortZ",
                },
            ),
            "circuit_layout_items": ResourceMapping(
                "CircuitLayoutItem",
                {
                    "source_id": "CircuitLayoutItemID",
                    "circuit_layout_id": "CircuitLayoutID",
                    "position": "ItemIndex",
                    "item_type": "TableName",
                    "item_source_id": "TablePK",
                },
            ),
            "ports": ResourceMapping(
                "Port",
                {"source_id": "PortID", "device_id": "DeviceID", "name": "Name"},
            ),
            "patches": ResourceMapping(
                "Patch",
                {
                    "source_id": "PatchID",
                    "patch_type": "PatchType",
                    "port_a_id": "PortID_A",
                    "port_b_id": "PortID_B",
                },
            ),
            "pseudowires": ResourceMapping(
                "PseudoWire",
                {
                    "source_id": "PseudoWireID",
                    "port_a_id": "PortAID",
                    "port_b_id": "PortBID",
                },
            ),
        }
    )
    source = SourceDatabase(SQLiteSourceDatabaseSettings(source_path), mapping)
    public_circuit_id = repository.ocp_resource_id("nettool", "circuit", "926")
    if visibility is not None:
        public_circuit_id = repository.share_circuit(
            partner_id=partner_id,
            source_circuit_id="926",
            visibility=visibility,
        )
    app = create_app(
        settings,
        repository=repository,
        adapter=FakeAdapter(),
        source_database=source,
    )
    return app, client_id, client_secret, repository, public_circuit_id


async def test_partner_can_obtain_token_and_read_granted_devices(tmp_path: Path) -> None:
    app, client_id, client_secret = _configured_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "observium:devices:read",
            },
        )
        assert token_response.status_code == 200
        token = token_response.json()["access_token"]

        response = await client.get(
            "/v1/devices",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["id"] == "device_allowed"
    assert response.headers["X-Correlation-ID"]


async def test_scope_without_relationship_grant_is_denied(tmp_path: Path) -> None:
    app, client_id, client_secret = _configured_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "observium:alarms:read",
            },
        )
        token = token_response.json()["access_token"]
        response = await client.get(
            "/v1/alarms",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "access_denied"


async def test_unauthorized_individual_resource_is_hidden(tmp_path: Path) -> None:
    app, client_id, client_secret = _configured_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "observium:devices:read",
            },
        )
        token = token_response.json()["access_token"]
        response = await client.get(
            "/v1/devices/device_hidden",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "resource_not_found"


async def test_circuit_path_is_hidden_until_circuit_is_explicitly_shared(
    tmp_path: Path,
) -> None:
    app, client_id, client_secret, _, public_circuit_id = _configured_circuit_app(
        tmp_path,
        visibility=None,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "circuits:read",
            },
        )
        token = token_response.json()["access_token"]
        response = await client.get(
            f"/v1/circuits/{public_circuit_id}/path",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404, response.text


async def test_shared_circuit_path_preserves_order_and_uses_public_ids(
    tmp_path: Path,
) -> None:
    app, client_id, client_secret, _, public_circuit_id = _configured_circuit_app(
        tmp_path,
        visibility="sfn_path",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "circuits:read",
            },
        )
        token = token_response.json()["access_token"]
        response = await client.get(
            f"/v1/circuits/{public_circuit_id}/path",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["kind"] for item in body["items"]] == [
        "port",
        "patch",
        "pseudowire",
        "port",
    ]
    assert [item["position"] for item in body["items"]] == [10, 20, 30, 40]
    assert [item["name"] for item in (body["items"][0], body["items"][3])] == [
        "xe-0/0/1",
        "xe-0/0/2",
    ]
    assert all(item["resource_id"] not in {"101", "202", "501", "601"} for item in body["items"])
    assert "TablePK" not in response.text
    assert body["items"][1]["port_a_id"] != "101"
    assert body["items"][1]["port_b_id"] != "202"


async def test_endpoint_visibility_returns_only_shared_endpoint_ports(
    tmp_path: Path,
) -> None:
    app, client_id, client_secret, _, public_circuit_id = _configured_circuit_app(
        tmp_path,
        visibility="endpoints",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "circuits:read",
            },
        )
        token = token_response.json()["access_token"]
        response = await client.get(
            f"/v1/circuits/{public_circuit_id}/path",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert [item["kind"] for item in response.json()["items"]] == ["port", "port"]


async def test_selected_source_database_profile_is_loaded_into_app(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.db",
        issuer_url="https://connector.example",
        token_audience="ocp",
        token_signing_key="s" * 32,
        source_database_profile="nettool",
    )
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    repository.put_source_database_profile(
        name="nettool",
        driver="sqlite",
        host=None,
        port=None,
        database_name=str(tmp_path / "nettool.db"),
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=SourceMapping.nettool_defaults().to_json(),
    )

    app = create_app(settings, repository=repository, adapter=FakeAdapter())

    assert app.state.source_database is not None
    assert app.state.source_database.driver == "sqlite"
