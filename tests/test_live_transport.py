from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from uuid import uuid4

import httpx
import pytest
from npdb_fixture import create_npdb

from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.company_access import approve_company_policy
from open_carrier_portal.company_policies import CompanyPolicy, PolicySnapshot
from open_carrier_portal.config import Settings
from open_carrier_portal.live_contract import LiveRequest, LiveResult
from open_carrier_portal.live_reader import execute_live_request
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.portal_agent import PortalConnection, run_agent, serve_live_request
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import SourceMapping


@pytest.fixture
def live_source(tmp_path):
    database = tmp_path / "source.db"
    create_npdb(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE LitCircuit SET BillingCompanyID=8 WHERE LitCircuitID=927")
    settings = Settings(tmp_path / "metadata.db", "http://localhost:8000", "test", "s" * 48)
    repo = Repository(settings.database_path, settings.token_signing_key)
    repo.initialize()
    connector = str(uuid4())
    repo.put_connector_identity(
        ConnectorIdentity(
            organization_id=str(uuid4()),
            organization_name="Local company",
            connector_id=connector,
            issuer_url=settings.issuer_url,
        )
    )
    connection = PortalConnection("https://portal.example", connector, str(uuid4()), "t" * 64)
    repo.put_source_database_profile(
        name="inventory",
        driver="sqlite",
        database_name=str(database),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=SourceMapping.nettool_defaults().to_json(),
    )
    policy = CompanyPolicy(
        policy_id=uuid4(),
        revision=1,
        owner_organization_id=connection.organization_id,
        recipient_organization_id=uuid4(),
        recipient_name="Recipient",
        connector_id=connector,
        state="pending",
    )
    repo.synchronize_portal_policies(connection, PolicySnapshot(version=1, policies=[policy]))
    approve_company_policy(
        repo,
        connection,
        metadata_path=settings.database_path,
        policy_id=str(policy.policy_id),
        revision=1,
        profile_name="inventory",
        company_id="7",
        approved_by="owner",
    )
    policy = policy.model_copy(update={"state": "active"})
    repo.synchronize_portal_policies(connection, PolicySnapshot(version=1, policies=[policy]))
    return settings, repo, connection, policy, database


def job(fixture, **changes):
    _, _, connection, policy, _ = fixture
    return LiveRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "connector_id": connection.connector_id,
            "owner_organization_id": connection.organization_id,
            "recipient_organization_id": str(policy.recipient_organization_id),
            "policy_id": str(policy.policy_id),
            "revision": 1,
            "operation": "circuits.list",
            "expires_at": time.time() + 20,
            **changes,
        }
    )


def execute(fixture, request, monitoring=None):
    settings, repo, connection, _, _ = fixture
    return asyncio.run(
        execute_live_request(
            repo,
            connection,
            request,
            metadata_path=settings.database_path,
            monitoring=monitoring,
        )
    )


@pytest.mark.parametrize("order,expected", [
    ("name_asc", ["Alpha circuit", "Zebra circuit"]),
    ("name_desc", ["Zebra circuit", "Alpha circuit"]),
])
def test_live_search_sorts_before_pagination_and_keeps_company_scope(
    live_source, order, expected
):
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET LitCircuitName='Zebra circuit' WHERE LitCircuitID=926")
        db.execute(
            "INSERT INTO LitCircuit (LitCircuitID, LitCircuitName, BillingCompanyID) "
            "VALUES (999, 'Alpha circuit', 7)"
        )
        db.execute(
            "UPDATE LitCircuit SET LitCircuitName='AAA secret circuit' WHERE LitCircuitID=927"
        )
    first = execute(live_source, job(live_source, query="circuit", order=order, limit=1))
    assert first.error is None
    assert [item.name for item in first.page.items] == expected[:1]
    assert first.page.next_offset == 1
    second = execute(
        live_source, job(live_source, query="circuit", order=order, limit=1, offset=1)
    )
    assert [item.name for item in second.page.items] == expected[1:]
    assert second.page.next_offset is None
    by_id = execute(live_source, job(live_source, query="999", order=order))
    assert [item.name for item in by_id.page.items] == ["Alpha circuit"]
    forbidden = execute(live_source, job(live_source, query="AAA", order=order))
    assert forbidden.error is None and forbidden.page.items == []
    assert b"Zebra circuit" not in live_source[0].database_path.read_bytes()


def test_live_list_and_endpoint_read_are_filtered_and_no_records_are_persisted(live_source):
    settings, repo, _, _, _ = live_source
    result = execute(live_source, job(live_source))
    assert result.error is None
    assert len(result.page.items) == 1
    circuit = result.page.items[0]
    assert circuit.name == "Example circuit"
    request = job(live_source, operation="circuits.read", circuit_id=circuit.id)
    read = execute(live_source, request)
    assert read.error is None
    assert read.circuit.id == circuit.id
    assert [endpoint.position for endpoint in read.circuit.endpoints] == ["a", "z"]
    assert all(endpoint.port_status.state == "unavailable" for endpoint in read.circuit.endpoints)
    assert all(
        endpoint.port_status.operational_status is None for endpoint in read.circuit.endpoints
    )
    serialized = read.model_dump_json()
    for private in ("management_ip", "ServiceOrderID", "BillingCompanyID", "api_token", "password"):
        assert private not in serialized
    with closing(sqlite3.connect(settings.database_path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "LitCircuit" not in tables
        assert b"Example circuit" not in settings.database_path.read_bytes()
        assert db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
    assert repo.administration_snapshot()["partners"] == []


def test_pagination_is_billing_filtered_and_parameterized(live_source):
    _, _, _, _, database = live_source
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO LitCircuit SELECT 928, 'Second circuit', BillingCompanyID, ServiceID, "
            "Description, CompanyLocationA, CompanyLocationZ, ServiceOrderID, CircuitLayoutID, "
            "Active FROM LitCircuit WHERE LitCircuitID=926"
        )
        db.execute(
            "INSERT INTO LitCircuit SELECT 929, 'Must not be visible', 8, ServiceID, "
            "Description, CompanyLocationA, CompanyLocationZ, ServiceOrderID, CircuitLayoutID, "
            "Active FROM LitCircuit WHERE LitCircuitID=926"
        )
    first = execute(live_source, job(live_source, limit=1))
    assert first.page.next_offset == 1
    second = execute(live_source, job(live_source, limit=1, offset=first.page.next_offset))
    assert second.page.items[0].name == "Second circuit"
    assert second.page.next_offset is None


@pytest.mark.parametrize(
    "change",
    [
        {"recipient_organization_id": str(uuid4())},
        {"owner_organization_id": str(uuid4())},
        {"connector_id": str(uuid4())},
        {"revision": 2},
        {"expires_at": time.time() - 1},
        {"expires_at": time.time() + 1000},
    ],
)
def test_wrong_tenant_revision_connector_or_expiry_never_reads_data(live_source, change):
    result = execute(live_source, job(live_source, **change))
    assert result.error == "access_denied"
    assert result.circuit is None and result.page is None


def test_transferred_ownership_and_stopped_policy_cannot_return_circuit(live_source):
    _, repo, connection, policy, database = live_source
    circuit_id = execute(live_source, job(live_source)).page.items[0].id
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE LitCircuit SET BillingCompanyID=8 WHERE LitCircuitID=926")
    result = execute(
        live_source, job(live_source, operation="circuits.read", circuit_id=circuit_id)
    )
    assert result.error == "not_found"
    assert execute(live_source, job(live_source)).page.items == []
    repo.stop_portal_policy(connection, policy_id=str(policy.policy_id))
    assert execute(live_source, job(live_source)).error == "access_denied"


def test_profile_scoped_opaque_ids_do_not_accept_legacy_ids(live_source):
    _, repo, _, _, _ = live_source
    legacy_id = repo.ocp_resource_id("nettool", "circuit", "926")
    result = execute(live_source, job(live_source, operation="circuits.read", circuit_id=legacy_id))
    assert result.error == "not_found"


def test_missing_source_is_unavailable_not_an_empty_success(live_source):
    _, _, _, _, database = live_source
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP TABLE LitCircuit")
    result = execute(live_source, job(live_source))
    assert result.error == "unavailable"
    assert result.page is None


def test_outbound_poll_upload_and_obsolete_result_are_handled(live_source):
    settings, repo, connection, _, _ = live_source
    request = job(live_source)
    sent = []

    def handler(http_request):
        assert http_request.headers["Authorization"] == f"Bearer {connection.token}"
        if http_request.url.path.endswith("/poll"):
            return httpx.Response(200, json={"request": request.model_dump(mode="json")})
        sent.append(LiveResult.model_validate_json(http_request.content))
        assert http_request.url.params["connector_id"] == connection.connector_id
        return httpx.Response(409, json={"error": "obsolete"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert serve_live_request(client, connection, repository=repo, settings=settings)
    assert len(sent) == 1 and sent[0].page.items[0].name == "Example circuit"
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"request": None}))
    ) as client:
        assert not serve_live_request(client, connection, repository=repo, settings=settings)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"request": {"owner_view": True}})
            )
        ) as client,
        pytest.raises(ValueError, match="invalid live request"),
    ):
        serve_live_request(client, connection, repository=repo, settings=settings)


def test_standalone_agent_services_live_requests_after_heartbeat(live_source, monkeypatch):
    settings, repo, connection, _, _ = live_source
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append("heartbeat")

    def serve(client, portal, **kwargs):
        assert kwargs["settings"] == settings
        assert kwargs["repository"] is repo
        calls.append("live")
        response = httpx.Response(401, request=httpx.Request("POST", connection.portal_url))
        response.raise_for_status()

    monkeypatch.setattr("open_carrier_portal.portal_agent.send_heartbeat", heartbeat)
    monkeypatch.setattr("open_carrier_portal.portal_agent.serve_live_request", serve)
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        run_agent(client, connection, repository=repo, settings=settings)
    assert calls == ["heartbeat", "live"]


def test_linked_monitoring_reports_verified_status_and_upstream_failure_is_unavailable(live_source):
    settings, repo, connection, policy, _ = live_source
    circuit_id = execute(live_source, job(live_source)).page.items[0].id
    fail = [False]
    stop = [False]

    def upstream(request):
        if fail[0]:
            return httpx.Response(503)
        if stop[0]:
            stop[0] = False
            repo.stop_portal_policy(connection, policy_id=str(policy.policy_id))
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
                        "device_id": 1301 if number == 1101 else 1302,
                        "ifName": "ethernet1",
                        "ifAdminStatus": "up",
                        "ifOperStatus": "down",
                    }
                ]
            },
        )

    async def read():
        monitoring = ObserviumAdapter(
            base_url="https://monitoring.example",
            api_token="test-monitoring-secret",
            repository=repo,
            transport=httpx.MockTransport(upstream),
        )
        try:
            return await execute_live_request(
                repo,
                connection,
                job(live_source, operation="circuits.read", circuit_id=circuit_id),
                metadata_path=settings.database_path,
                monitoring=monitoring,
            )
        finally:
            await monitoring.close()

    result = asyncio.run(read())
    assert result.error is None
    assert result.circuit.endpoints[0].port_status.operational_status == "down"
    assert "private-management-address" not in result.model_dump_json()
    assert "test-monitoring-secret" not in result.model_dump_json()
    fail[0] = True
    assert asyncio.run(read()).error == "unavailable"
    fail[0] = False
    stop[0] = True
    assert asyncio.run(read()).error == "access_denied"
