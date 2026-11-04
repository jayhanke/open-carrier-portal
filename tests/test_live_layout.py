from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from contextlib import closing

import httpx
import pytest
from test_live_transport import execute, job
from test_live_transport import live_source as live_source
from test_owner_browsing import enable, owner_job

from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.company_access import approve_company_policy
from open_carrier_portal.company_policies import PolicySnapshot
from open_carrier_portal.live_reader import execute_live_request
from open_carrier_portal.owner_access import approve_owner_source


def owner_layout(fixture):
    settings, repo, connection, _, _ = fixture
    approve_owner_source(
        repo,
        connection,
        metadata_path=settings.database_path,
        profile_name="inventory",
        approved_by="operator",
        explorer_enabled=True,
        layout_enabled=True,
    )
    source = repo.list_portal_owner_sources(connection)[0]
    circuit = execute(fixture, owner_job(fixture, source)).page.items[0]
    return owner_job(fixture, source).model_copy(
        update={
            "operation": "circuits.read",
            "circuit_id": circuit.id,
        }
    )


def partner_layout(fixture):
    settings, repo, connection, policy, _ = fixture
    proposal = policy.model_copy(
        update={
            "revision": 2,
            "state": "pending",
            "visibility": "full_shared_path",
        }
    )
    repo.synchronize_portal_policies(connection, PolicySnapshot(version=1, policies=[proposal]))
    with pytest.raises(ValueError, match="explicit full layout"):
        approve_company_policy(
            repo,
            connection,
            metadata_path=settings.database_path,
            policy_id=str(policy.policy_id),
            revision=2,
            profile_name="inventory",
            company_id="7",
            approved_by="operator",
        )
    approve_company_policy(
        repo,
        connection,
        metadata_path=settings.database_path,
        policy_id=str(policy.policy_id),
        revision=2,
        profile_name="inventory",
        company_id="7",
        approved_by="operator",
        layout_consent=True,
    )
    proposal = proposal.model_copy(update={"state": "active"})
    repo.synchronize_portal_policies(connection, PolicySnapshot(version=1, policies=[proposal]))
    listed = execute(fixture, job(fixture, revision=2, visibility="full_shared_path")).page
    assert len(listed.items) == 1
    return job(
        fixture,
        revision=2,
        visibility="full_shared_path",
        operation="circuits.read",
        circuit_id=listed.items[0].id,
    )


@pytest.mark.parametrize("audience", ["owner", "partner"])
def test_full_layout_has_ordered_components_and_linked_observium_status(live_source, audience):
    settings, repo, connection, _, database = live_source
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO CircuitLayoutItem VALUES(5,42,25,'Port',999)")
        db.execute("UPDATE Patch SET PortID_B_sec=999 WHERE PatchID=501")
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        number = int(request.url.path.rstrip("/").rsplit("/", 1)[1])
        if "/devices/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "device_id": number,
                            "hostname": "private-management-address",
                            "status": 1,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "ports": [
                    {
                        "port_id": number,
                        "device_id": {1101: 1301, 1202: 1302, 1999: 1999}[number],
                        "ifName": "monitoring-name",
                        "ifAdminStatus": "up",
                        "ifOperStatus": "down" if number == 1999 else "up",
                        "ifSpeed": 10000000000,
                        "last_polled": "2026-11-04T20:30:00Z",
                    }
                ]
            },
        )

    async def read():
        monitoring = ObserviumAdapter(
            base_url="https://monitoring.example",
            api_token="test-private-token",
            repository=repo,
            transport=httpx.MockTransport(upstream),
        )
        try:
            return await execute_live_request(
                repo,
                connection,
                request,
                metadata_path=settings.database_path,
                monitoring=monitoring,
            )
        finally:
            await monitoring.close()

    result = asyncio.run(read())
    assert result.error is None
    layout = result.circuit.layout
    assert [item.position for item in layout.items] == [10, 20, 25, 30, 40]
    assert [item.kind for item in layout.items] == ["port", "patch", "port", "pseudowire", "port"]
    assert layout.items[1].patch_type == "fiber" and layout.items[3].name == "65000"
    assert [port.role for port in layout.items[1].ports] == ["a", "z", "z_secondary"]
    assert layout.items[2].ports[0].port_status.operational_status == "down"
    assert layout.items[0].ports[0].port_status.speed_bps == 10000000000
    assert all(port.rack and port.location for item in layout.items for port in item.ports)
    assert all(count == 1 for count in Counter(calls).values())
    assert set(calls) == {
        "/api/v0/devices/1301/",
        "/api/v0/devices/1302/",
        "/api/v0/devices/1999/",
        "/api/v0/ports/1101/",
        "/api/v0/ports/1202/",
        "/api/v0/ports/1999/",
    }
    assert result.circuit.inventory_links == (audience == "owner")
    serialized = result.model_dump_json()
    for private in (
        "test-private-token",
        "private-management-address",
        "198.51.100.",
        "private-order",
        "private-asset",
        "private-mac",
    ):
        assert private not in serialized
    assert b"Example circuit" not in settings.database_path.read_bytes()


@pytest.mark.parametrize("audience", ["owner", "partner"])
@pytest.mark.parametrize(
    "secondary_a,secondary_z,roles",
    [
        (0, 0, ["a", "z"]),
        (0, 101, ["a", "z", "z_secondary"]),
        (202, 0, ["a", "z", "a_secondary"]),
    ],
)
def test_zero_secondary_patch_ports_are_absent(
    live_source, audience, secondary_a, secondary_z, roles
):
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(
            "UPDATE Patch SET PortID_A_sec=?, PortID_B_sec=? WHERE PatchID=501",
            (secondary_a, secondary_z),
        )
    result = execute(live_source, request)
    assert result.error is None
    assert [item.kind for item in result.circuit.layout.items] == [
        "port",
        "patch",
        "pseudowire",
        "port",
    ]
    assert [port.role for port in result.circuit.layout.items[1].ports] == roles
    assert [endpoint.position for endpoint in result.circuit.endpoints] == ["a", "z"]


def test_existing_grants_do_not_gain_layouts_and_new_scope_cannot_be_requested(live_source):
    source = enable(live_source)
    listed = execute(live_source, owner_job(live_source, source)).page
    old = owner_job(live_source, source).model_copy(
        update={
            "operation": "circuits.read",
            "circuit_id": listed.items[0].id,
        }
    )
    assert execute(live_source, old).circuit.layout is None
    assert (
        execute(live_source, job(live_source, visibility="full_shared_path")).error
        == "access_denied"
    )
    current = owner_layout(live_source)
    assert execute(live_source, old).error == "access_denied"
    assert execute(live_source, current).circuit.layout is not None
    partner = partner_layout(live_source)
    assert (
        execute(live_source, partner.model_copy(update={"visibility": "endpoints"})).error
        == "access_denied"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM CircuitLayoutItem",
        "UPDATE CircuitLayoutItem SET ItemIndex=10",
        "UPDATE CircuitLayoutItem SET TableName='Vault' WHERE CircuitLayoutItemID=2",
        "UPDATE Patch SET PortID_A=NULL",
        "UPDATE Patch SET PortID_A=0",
        "UPDATE Patch SET PortID_A_sec=99999",
        "UPDATE Patch SET PortID_B_sec=99999",
    ],
)
def test_invalid_or_unsupported_layout_is_explicitly_unavailable(live_source, statement):
    request = owner_layout(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(statement)
    result = execute(live_source, request)
    assert result.error == "unavailable" and result.circuit is None


def test_layout_partner_access_is_lost_after_billing_transfer(live_source):
    request = partner_layout(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET BillingCompanyID=8 WHERE LitCircuitID=926")
    assert execute(live_source, request).error == "not_found"


def test_owner_layout_column_migration_keeps_old_approval_endpoint_only(live_source):
    source = enable(live_source)
    settings, repo, connection, _, _ = live_source
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute("ALTER TABLE portal_owner_sources DROP COLUMN layout_enabled")
    repo.initialize()
    migrated = repo.list_portal_owner_sources(connection)[0]
    assert migrated.grant_id == source.grant_id and not migrated.layout_enabled


@pytest.mark.parametrize("behavior", ["offline", "missing_port", "wrong_device", "stop"])
def test_full_layout_monitoring_failures_are_explicit_and_never_healthy(live_source, behavior):
    settings, repo, connection, _, _ = live_source
    request = owner_layout(live_source)
    calls = []

    def upstream(http_request):
        calls.append(http_request.url.path)
        if behavior == "offline":
            return httpx.Response(503)
        if behavior == "stop" and len(calls) == 1:
            repo.stop_portal_owner_source(
                connection, source_profile_id=str(request.source_profile_id)
            )
        number = int(http_request.url.path.rstrip("/").rsplit("/", 1)[1])
        if "/devices/" in http_request.url.path:
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "device_id": number,
                            "hostname": "private-host",
                            "status": 1,
                        }
                    ]
                },
            )
        if behavior == "missing_port" and number == 1101:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "ports": [
                    {
                        "port_id": number,
                        "device_id": 1302
                        if behavior == "wrong_device"
                        else 1301
                        if number == 1101
                        else 1302,
                        "ifName": "ethernet",
                        "ifOperStatus": "up",
                        "ifAdminStatus": "up",
                    }
                ]
            },
        )

    async def read():
        monitoring = ObserviumAdapter(
            base_url="https://monitoring.example",
            api_token="test-private-token",
            repository=repo,
            transport=httpx.MockTransport(upstream),
        )
        try:
            return await execute_live_request(
                repo,
                connection,
                request,
                metadata_path=settings.database_path,
                monitoring=monitoring,
            )
        finally:
            await monitoring.close()

    result = asyncio.run(read())
    if behavior in {"wrong_device", "stop"}:
        assert result.circuit is None
        assert result.error == ("access_denied" if behavior == "stop" else "unavailable")
    else:
        assert result.error is None and result.circuit.layout is not None
        unavailable = result.circuit.endpoints[0].port_status
        assert unavailable.state == "unavailable" and unavailable.operational_status is None
        if behavior == "offline":
            assert len(calls) == 1
            assert all(
                port.port_status.state == "unavailable"
                for item in result.circuit.layout.items
                for port in item.ports
            )
        else:
            assert result.circuit.endpoints[1].port_status.operational_status == "up"
