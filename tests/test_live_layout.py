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
from open_carrier_portal.live_contract import LivePathItem
from open_carrier_portal.live_reader import execute_live_request
from open_carrier_portal.owner_access import approve_owner_source
from open_carrier_portal.source_database import SourceMapping


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


def unmap_layout_resource(fixture, resource):
    _, repo, _, _, database = fixture
    mapping = SourceMapping.nettool_defaults()
    del mapping.resources[resource]
    repo.put_source_database_profile(
        name="inventory", driver="sqlite", database_name=str(database),
        host=None, port=None, username=None, password_env_var=None, tls_ca_path=None,
        timeout_seconds=10, mapping_json=mapping.to_json(),
    )


@pytest.mark.parametrize("audience", ["owner", "partner"])
def test_full_layout_has_ordered_components_and_linked_observium_status(live_source, audience):
    settings, repo, connection, _, database = live_source
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO CircuitLayoutItem VALUES(5,42,25,'Port',999)")
        db.execute("INSERT INTO CircuitLayoutItem VALUES(6,42,27,'NewDarkFiberCircuit',701)")
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
    assert [item.position for item in layout.items] == [10, 20, 25, 27, 30, 40]
    assert [item.kind for item in layout.items] == [
        "port", "patch", "port", "dark_fiber", "pseudowire", "port"
    ]
    assert layout.items[1].patch_type == "fiber" and layout.items[4].name == "65000"
    assert layout.items[3].name == "Example dark fiber segment"
    assert layout.items[3].ports == []
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


@pytest.mark.parametrize("audience", ["owner", "partner"])
@pytest.mark.parametrize("mapped", [True, False])
def test_nettool_seven_step_dark_fiber_layout_keeps_all_steps(live_source, audience, mapped):
    if not mapped:
        unmap_layout_resource(live_source, "new_dark_fiber_circuits")
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("DELETE FROM CircuitLayoutItem")
        db.executemany(
            "INSERT INTO CircuitLayoutItem VALUES(?,42,?,?,?)",
            [
                (1, 1, "Port", 101),
                (2, 11, "Patch", 501),
                (3, 21, "Port", 202),
                (4, 32, "NewDarkFiberCircuit", 701),
                (5, 39, "Port", 101),
                (6, 49, "Patch", 501),
                (7, 59, "Port", 202),
            ],
        )
        db.execute("UPDATE Patch SET PortID_A_sec=0, PortID_B_sec=0")
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    result = execute(live_source, request)
    assert result.error is None
    items = result.circuit.layout.items
    assert [item.position for item in items] == [1, 11, 21, 32, 39, 49, 59]
    assert [item.kind for item in items] == [
        "port", "patch", "port", "dark_fiber" if mapped else "unresolved", "port", "patch", "port"
    ]
    assert items[3].name == ("Example dark fiber segment" if mapped else "NewDarkFiberCircuit")
    assert items[3].ports == []
    assert result.circuit.inventory_links == (audience == "owner")
    assert "NewDarkFiberCircuitID" not in result.model_dump_json()
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("DELETE FROM NewDarkFiberCircuit")
    missing = execute(live_source, request)
    assert missing.error is None
    assert [item.position for item in missing.circuit.layout.items] == [1, 11, 21, 32, 39, 49, 59]
    assert missing.circuit.layout.items[3].kind == "unresolved"
    assert missing.circuit.layout.items[3].name == "NewDarkFiberCircuit"
    assert missing.circuit.layout.items[3].issue == (
        "missing_reference" if mapped else "mapping_missing"
    )


@pytest.mark.parametrize("kind", ["port", "patch", "pseudowire"])
def test_only_description_segments_may_have_no_connected_ports(kind):
    with pytest.raises(ValueError, match="path nodes require"):
        LivePathItem(position=1, kind=kind, ports=[])
    assert LivePathItem(position=1, kind="dark_fiber", ports=[]).ports == []


@pytest.mark.parametrize("audience", ["owner", "partner"])
@pytest.mark.parametrize(
    "item_type,unmapped",
    [
        ("FutureTransport", None),
        ("Vault", None),
        ("<script>alert(1)</script>", None),
        ("NewDarkFiberCircuit", "new_dark_fiber_circuits"),
        ("Patch", "patches"),
        ("PW", "pseudowires"),
    ],
)
def test_unknown_or_unmapped_path_items_keep_their_declared_label(
    live_source, audience, item_type, unmapped, caplog
):
    settings, _, _, _, database = live_source
    if unmapped:
        unmap_layout_resource(live_source, unmapped)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "UPDATE CircuitLayoutItem SET TableName=? WHERE CircuitLayoutItemID=2",
            (item_type,),
        )
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    result = execute(live_source, request)
    assert result.error is None
    items = result.circuit.layout.items
    assert [item.position for item in items] == [10, 20, 30, 40]
    assert items[1].kind == "unresolved" and items[1].name == item_type
    assert items[1].issue == ("mapping_missing" if unmapped else "unsupported_type")
    assert items[1].ports == []
    assert items[0].kind == "port" and items[-1].kind == "port"
    assert result.circuit.inventory_links == (audience == "owner")
    assert "path.item_unresolved" in caplog.text
    assert b"FutureTransport" not in settings.database_path.read_bytes()


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM CircuitLayoutItem",
        "UPDATE CircuitLayoutItem SET ItemIndex=10",
        "UPDATE Patch SET PortID_A=NULL",
        "UPDATE Patch SET PortID_A=0",
        "UPDATE Patch SET PortID_A_sec=99999",
        "UPDATE Patch SET PortID_B_sec=99999",
    ],
)
def test_invalid_layout_is_explicitly_unavailable(live_source, statement):
    request = owner_layout(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(statement)
    result = execute(live_source, request)
    assert result.error == "unavailable" and result.circuit is None


def test_layout_partner_access_is_lost_after_billing_transfer(live_source):
    request = partner_layout(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE CircuitLayoutItem SET TableName='Vault' WHERE CircuitLayoutItemID=2")
    assert execute(live_source, request).circuit.layout.items[1].kind == "unresolved"
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET BillingCompanyID=8 WHERE LitCircuitID=926")
    assert execute(live_source, request).error == "not_found"


@pytest.mark.parametrize("item_type", ["FutureTransport", "", "X" * 800])
def test_default_item_labels_are_bounded_and_do_not_read_arbitrary_tables(live_source, item_type):
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("CREATE TABLE FutureTransport (id INTEGER, private_data TEXT)")
        db.execute("INSERT INTO FutureTransport VALUES(501,'do-not-expose-this-source-value')")
        db.execute(
            "UPDATE CircuitLayoutItem SET TableName=? WHERE CircuitLayoutItemID=2", (item_type,)
        )
    result = execute(live_source, owner_layout(live_source))
    assert result.error is None
    item = result.circuit.layout.items[1]
    assert item.kind == "unresolved" and item.issue == "unsupported_type"
    assert item.name == (item_type[:512] or "Unknown component")
    assert "do-not-expose-this-source-value" not in result.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"issue": None},
        {"name": None},
        {"patch_type": "fiber"},
        {"kind": "dark_fiber"},
        {"kind": "port"},
    ],
)
def test_unresolved_wire_nodes_cannot_claim_resolved_details(changes):
    with pytest.raises(ValueError):
        LivePathItem.model_validate(
            {
                "position": 20, "kind": "unresolved", "name": "FutureTransport",
                "issue": "unsupported_type", "ports": [], **changes,
            }
        )


def test_owner_layout_column_migration_keeps_old_approval_endpoint_only(live_source):
    source = enable(live_source)
    settings, repo, connection, _, _ = live_source
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute("ALTER TABLE portal_owner_sources DROP COLUMN layout_enabled")
    repo.initialize()
    migrated = repo.list_portal_owner_sources(connection)[0]
    assert migrated.grant_id == source.grant_id and not migrated.layout_enabled


@pytest.mark.parametrize("audience", ["owner", "partner"])
@pytest.mark.parametrize(
    "behavior", ["null_zero", "zero_zero", "null_linked", "zero_linked", "mixed"]
)
def test_unlinked_monitoring_never_breaks_full_layouts(live_source, audience, behavior, caplog):
    settings, repo, connection, _, database = live_source
    with closing(sqlite3.connect(database)) as db, db:
        statements = {
            "null_zero": ["UPDATE Device SET ObserviumID=NULL", "UPDATE Port SET ObserviumID=0"],
            "zero_zero": ["UPDATE Device SET ObserviumID=0", "UPDATE Port SET ObserviumID=0"],
            "null_linked": ["UPDATE Device SET ObserviumID=NULL"],
            "zero_linked": ["UPDATE Device SET ObserviumID=0"],
            "mixed": ["UPDATE Device SET ObserviumID=NULL WHERE DeviceID=301"],
        }
        for statement in statements[behavior]:
            db.execute(statement)
    request = owner_layout(live_source) if audience == "owner" else partner_layout(live_source)
    calls = []

    def upstream(http_request):
        calls.append(http_request.url.path)
        number = int(http_request.url.path.rstrip("/").rsplit("/", 1)[1])
        assert number > 0
        if "/devices/" in http_request.url.path:
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {"device_id": number, "hostname": "private-host", "status": 1}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "ports": [{
                    "port_id": number, "device_id": {1101: 1301, 1202: 1302}[number],
                    "ifOperStatus": "up", "ifAdminStatus": "up",
                }]
            },
        )

    async def read():
        monitoring = ObserviumAdapter(
            base_url="https://monitoring.example", api_token="test-private-token",
            repository=repo, transport=httpx.MockTransport(upstream),
        )
        try:
            return await execute_live_request(
                repo, connection, request, metadata_path=settings.database_path,
                monitoring=monitoring,
            )
        finally:
            await monitoring.close()

    result = asyncio.run(read())
    assert result.error is None and len(result.circuit.layout.items) == 4
    assert result.circuit.inventory_links == (audience == "owner")
    statuses = [endpoint.port_status for endpoint in result.circuit.endpoints]
    if behavior == "mixed":
        assert statuses[0].state == "unavailable"
        assert statuses[0].operational_status is None
        assert statuses[1].state == "available" and statuses[1].operational_status == "up"
        assert set(calls) == {"/api/v0/devices/1302/", "/api/v0/ports/1202/"}
    else:
        assert not calls
        expected = "not_linked" if behavior.endswith("zero") else "unavailable"
        assert all(
            status.state == expected and status.operational_status is None for status in statuses
        )
    if behavior in {"null_linked", "zero_linked", "mixed"}:
        assert "circuit.device_link_missing" in caplog.text


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
