import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from npdb_fixture import create_npdb

from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.circuits import _observium_id
from open_carrier_portal.config import Settings
from open_carrier_portal.main import create_app
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import hash_client_secret
from open_carrier_portal.source_contract import SOURCE_RESOURCES
from open_carrier_portal.source_database import (
    ResourceMapping,
    SourceDatabase,
    SourceMapping,
    SQLiteSourceDatabaseSettings,
)


@pytest.fixture
def circuit_app(tmp_path):
    source_path = tmp_path / "source.db"
    create_npdb(source_path)
    settings = Settings(tmp_path / "metadata.db", "http://localhost:8000", "test", "s" * 48)
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    partner = repository.create_partner("Example partner")
    relationship = repository.create_active_relationship(partner)
    repository.create_grant(relationship, "circuits:read")
    client_id = repository.create_client(
        partner, hash_client_secret("partner-secret"), {"circuits:read"}
    )
    circuit_id = repository.share_circuit(
        partner_id=partner,
        source_circuit_id="926",
        visibility="full_shared_path",
    )
    requests: list[str] = []
    behavior = {"fail": False, "wrong_device": False}

    def upstream(request: httpx.Request):
        requests.append(request.url.path)
        if behavior["fail"]:
            return httpx.Response(503)
        number = int(request.url.path.rstrip("/").rsplit("/", 1)[1])
        if "/devices/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "device_id": number,
                            "hostname": "192.0.2.1",
                            "vendor": "Example vendor",
                            "hardware": "Example model",
                            "os": "Example OS",
                            "status": 1,
                            "last_polled": "2026-10-21T12:00:00Z",
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
                        "device_id": 1999
                        if behavior["wrong_device"]
                        else (1301 if number == 1101 else 1302),
                        "ifName": "ethernet1",
                        "ifAlias": "Shared handoff",
                        "ifSpeed": 10000000000,
                        "ifAdminStatus": "up",
                        "ifOperStatus": "up",
                        "last_polled": "2026-10-21T12:00:00Z",
                    }
                ]
            },
        )

    source = SourceDatabase(
        SQLiteSourceDatabaseSettings(source_path), SourceMapping.nettool_defaults()
    )
    monitoring = ObserviumAdapter(
        base_url="https://monitoring.example",
        api_token="private-token",
        repository=repository,
        transport=httpx.MockTransport(upstream),
    )
    app = create_app(settings, repository=repository, adapter=monitoring, source_database=source)
    return app, client_id, partner, circuit_id, source_path, requests, behavior


def token(client, client_id):
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "partner-secret",
            "scope": "circuits:read",
        },
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_corrected_defaults_validate_against_supplied_schema(tmp_path: Path):
    database = tmp_path / "source.db"
    create_npdb(database)
    mapping = SourceMapping.nettool_defaults()
    source = SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping)
    source.validate_mapping()
    assert source.missing_fields == {}
    assert set(mapping.resources) == set(SOURCE_RESOURCES)
    assert mapping.resources["devices"].columns["name"] == "DeviceName"
    assert mapping.resources["ports"].columns["name"] == "PortName"
    assert mapping.resources["circuits"].columns["a_company_location_id"] == "CompanyLocationA"
    assert "name" not in mapping.resources["locations"].columns
    assert "management_ip" not in mapping.resources["devices"].columns
    assert {
        "source_table": "CompanyLocation",
        "source_column": "Company_ID",
        "target_table": "company",
        "target_column": "Company_ID",
    } in source.describe_relationships()


@pytest.mark.parametrize(
    "resource,field",
    [
        ("circuits", "circuit_layout_id"),
        ("ports", "device_id"),
        ("devices", "rack_id"),
        ("racks", "location_id"),
        ("devices", "observium_id"),
        ("ports", "observium_id"),
    ],
)
def test_required_link_mapping_is_validated_before_reading_inventory(circuit_app, resource, field):
    app, client_id, _, circuit_id, _, requests, _ = circuit_app
    source = app.state.source_database
    original = source._mapping.resources[resource]
    source._mapping = SourceMapping(
        {
            **source._mapping.resources,
            resource: ResourceMapping(
                original.table,
                {key: value for key, value in original.columns.items() if key != field},
                original.joins,
            ),
        }
    )
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502, response.text
    assert resource + "." + field in response.json()["error"]["message"]
    assert not requests


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE Port SET DeviceID = NULL WHERE PortID = 101",
        "UPDATE Port SET DeviceID = 99999 WHERE PortID = 101",
        "UPDATE Device SET CompanyLocationID = NULL WHERE DeviceID = 301",
        "UPDATE Device SET CompanyLocationID = 99999 WHERE DeviceID = 301",
        "UPDATE CompanyLocation SET LocationID = NULL",
        "UPDATE CompanyLocation SET LocationID = 99999",
        "UPDATE CircuitLayout SET PortA = NULL",
        "UPDATE CircuitLayout SET PortZ = NULL",
        "UPDATE Patch SET PortID_A = NULL",
        "UPDATE PseudoWire SET PortBID = NULL",
        "DELETE FROM CircuitLayoutItem",
    ],
)
@pytest.mark.parametrize("suffix", ["", "/path"])
def test_broken_required_relationship_is_not_a_successful_partial_path(
    circuit_app, statement, suffix
):
    app, client_id, _, circuit_id, database, _, _ = circuit_app
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    with TestClient(app) as client:
        response = client.get(
            "/v1/circuits/" + circuit_id + suffix, headers=token(client, client_id)
        )
    assert response.status_code == 502, response.text
    with sqlite3.connect(app.state.settings.database_path) as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM audit_events ORDER BY occurred_at DESC LIMIT 1"
            ).fetchone()[0]
            == "error"
        )


def test_companies_and_racks_are_optional_enrichment(circuit_app):
    app, client_id, _, circuit_id, _, _, _ = circuit_app
    source = app.state.source_database
    source._mapping = SourceMapping(
        {
            key: value
            for key, value in source._mapping.resources.items()
            if key not in {"companies", "rack_positions"}
        }
    )
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["omitted_enrichment"]) == {"companies", "rack_units"}
    assert body["path"]["items"][0]["port"]["device"]["site"]["location"]["id"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None), (0, None), (0.0, None), (Decimal("0.00"), None),
        ("0", None), (" 0 ", None), ("", None), (" ", None),
        (1301, "1301"), (" 1301 ", "1301"),
    ],
)
def test_observium_link_ids_normalize_only_unlinked_sentinels(value, expected):
    assert _observium_id(value) == expected


@pytest.mark.parametrize(
    "device_id,port_id",
    [(None, None), (None, 0), (0, 0), ("0", "0"), ("", ""), (" ", " ")],
)
def test_unlinked_monitoring_ids_allow_unmonitored_inventory(circuit_app, device_id, port_id):
    app, client_id, _, circuit_id, database, requests, _ = circuit_app
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE Device SET ObserviumID = ?", (device_id,))
        connection.execute("UPDATE Port SET ObserviumID = ?", (port_id,))
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    port = response.json()["path"]["items"][0]["port"]
    assert port["monitoring_state"] == "not_linked"
    assert port["device"]["monitoring_state"] == "not_linked"
    assert port["device"]["site"]["location"]["id"]
    assert not requests


def test_monitoring_mappings_can_be_omitted_without_a_monitoring_integration(circuit_app):
    app, client_id, _, circuit_id, _, requests, _ = circuit_app
    source = app.state.source_database
    source._mapping = SourceMapping(
        {
            key: ResourceMapping(
                value.table,
                {
                    field: column
                    for field, column in value.columns.items()
                    if field != "observium_id"
                },
                value.joins,
            )
            for key, value in source._mapping.resources.items()
        }
    )
    unmonitored = create_app(
        app.state.settings, repository=app.state.repository, source_database=source
    )
    with TestClient(unmonitored) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    port = response.json()["path"]["items"][0]["port"]
    assert port["device"]["site"]["location"]["id"]
    assert port["monitoring_state"] == "not_linked"
    assert not requests


@pytest.mark.parametrize("device_id", [None, 0, ""])
def test_monitored_port_requires_its_devices_monitoring_link(circuit_app, device_id):
    app, client_id, _, circuit_id, database, requests, _ = circuit_app
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE Device SET ObserviumID = ? WHERE DeviceID = 301", (device_id,)
        )
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502, response.text
    assert "device's Observium ID" in response.json()["error"]["message"]
    assert not requests


def test_circuit_details_follow_real_relationships_and_include_live_status(circuit_app):
    app, client_id, _, circuit_id, _, requests, _ = circuit_app
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == circuit_id
    assert body["name"] == "Example circuit"
    assert body["active"] is True
    assert body["service_type"] == "Ethernet"
    assert body["bandwidth"] == "10G"
    a, z = body["a_location"], body["z_location"]
    assert a["id"] != z["id"]
    assert a["location"]["id"] == z["location"]["id"]
    assert a["company"]["name"] == "Example Carrier"
    assert z["company"]["name"] == "Example Partner"
    assert a["rack_name"] == "Rack A"
    assert a["name"] == "Rack A"
    assert z["name"] == "Rack Z"
    assert body["path"]["mapping_state"] == "complete"
    assert [item["position"] for item in body["path"]["items"]] == [10, 20, 30, 40]
    first = body["path"]["items"][0]["port"]
    assert first["device"]["name"] == "edge-a"
    assert [rack["name"] for rack in first["device"]["rack_units"]] == ["U20", "U21"]
    assert first["device"]["monitoring"]["vendor"] == "Example vendor"
    assert first["monitoring_state"] == "available"
    assert first["monitoring"]["speed_bps"] == 10000000000
    assert first["monitoring"]["operational_status"] == "up"
    assert first["monitoring"]["optical_state"] == "not_reported"
    assert body["path"]["items"][1]["port_a"]["id"] == first["id"]
    assert body["path"]["items"][2]["port_b"]["device"]["name"] == "edge-z"
    assert sorted(requests) == [
        "/api/v0/devices/1301/",
        "/api/v0/devices/1302/",
        "/api/v0/ports/1101/",
        "/api/v0/ports/1202/",
    ]
    for private in (
        "192.0.2.",
        "198.51.100.",
        "private-mac",
        "private-asset",
        "private-order",
        "MgmtIP",
        "IPAddress",
        "MACAddress",
        "AssetID",
        "ServiceOrderID",
        "private-token",
    ):
        assert private not in response.text
    with sqlite3.connect(app.state.settings.database_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables.intersection(
            {
                "company",
                "Location",
                "CompanyLocation",
                "Device",
                "Port",
                "LitCircuit",
                "RackUnit",
            }
        )
        assert (
            db.execute(
                "SELECT outcome FROM audit_events ORDER BY occurred_at DESC LIMIT 1"
            ).fetchone()[0]
            == "success"
        )


@pytest.mark.parametrize("visibility", ["endpoints", "full_shared_path"])
@pytest.mark.parametrize("legacy_fields", [False, True])
@pytest.mark.parametrize("reverse_endpoints", [False, True])
def test_circuit_locations_follow_endpoint_racks_not_separate_circuit_fields(
    circuit_app, visibility, legacy_fields, reverse_endpoints
):
    app, client_id, partner, circuit_id, database, _, _ = circuit_app
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE LitCircuit SET CompanyLocationA = 99999, CompanyLocationZ = NULL"
        )
        if reverse_endpoints:
            connection.execute("UPDATE CircuitLayout SET PortA = 202, PortZ = 101")
    source = app.state.source_database
    if not legacy_fields:
        original = source._mapping.resources["circuits"]
        source._mapping = SourceMapping(
            {
                **source._mapping.resources,
                "circuits": ResourceMapping(
                    original.table,
                    {
                        key: value
                        for key, value in original.columns.items()
                        if key not in {"active", "a_company_location_id", "z_company_location_id"}
                    },
                    original.joins,
                ),
            }
        )
    app.state.repository.share_circuit(
        partner_id=partner, source_circuit_id="926", visibility=visibility
    )
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["a_location"]["name"] == ("Rack Z" if reverse_endpoints else "Rack A")
    assert body["z_location"]["name"] == ("Rack A" if reverse_endpoints else "Rack Z")
    assert body["a_location"]["id"] == app.state.repository.ocp_resource_id(
        "nettool", "company_location", "1002" if reverse_endpoints else "1001"
    )
    assert body["active"] == (True if legacy_fields else None)
    assert body["a_location"]["location"]["address1"] == "100 Main Street"


def test_circuit_share_does_not_authorize_independent_resources(circuit_app):
    app, client_id, _, circuit_id, _, requests, _ = circuit_app
    repo = app.state.repository
    with TestClient(app) as client:
        headers = token(client, client_id)
        assert client.get("/v1/circuits/" + circuit_id, headers=headers).status_code == 200
        count = len(requests)
        interface = repo.ocp_resource_id("observium", "interface", "1101")
        device = repo.ocp_resource_id("observium", "device", "1301")
        assert client.get("/v1/interfaces/" + interface, headers=headers).status_code == 404
        assert client.get("/v1/devices/" + device, headers=headers).status_code == 404
        assert client.get("/v1/interfaces", headers=headers).status_code == 403
        assert len(requests) == count


def test_unshared_and_revoked_circuits_never_query_monitoring(circuit_app):
    app, client_id, partner, circuit_id, _, requests, _ = circuit_app
    with TestClient(app) as client:
        headers = token(client, client_id)
        other = app.state.repository.ocp_resource_id("nettool", "circuit", "927")
        assert client.get("/v1/circuits/" + other, headers=headers).status_code == 404
        app.state.repository.revoke_circuit_share(partner_id=partner, source_circuit_id="926")
        assert client.get("/v1/circuits/" + circuit_id, headers=headers).status_code == 404
        assert requests == []


def test_summary_visibility_never_expands_to_path_or_monitoring(circuit_app):
    app, client_id, partner, circuit_id, _, requests, _ = circuit_app
    app.state.repository.share_circuit(
        partner_id=partner,
        source_circuit_id="926",
        visibility="summary",
    )
    with TestClient(app) as client:
        headers = token(client, client_id)
        response = client.get("/v1/circuits/" + circuit_id, headers=headers)
        assert response.status_code == 200
        assert response.json()["path"] is None
        assert (
            client.get("/v1/circuits/" + circuit_id + "/path", headers=headers).status_code == 404
        )
        assert requests == []


@pytest.mark.parametrize("behavior,status", [("fail", 503), ("wrong_device", 502)])
def test_monitoring_failures_are_not_success_shaped(circuit_app, behavior, status):
    app, client_id, _, circuit_id, _, _, settings = circuit_app
    settings[behavior] = True
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == status
    assert "path" not in response.json()
    with sqlite3.connect(app.state.settings.database_path) as db:
        assert (
            db.execute(
                "SELECT outcome FROM audit_events ORDER BY occurred_at DESC LIMIT 1"
            ).fetchone()[0]
            == "error"
        )


def test_missing_site_mapping_is_not_a_successful_partial_circuit(circuit_app):
    app, client_id, _, circuit_id, _, _, _ = circuit_app
    source = app.state.source_database
    del source._mapping.resources["racks"]
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502
    assert "racks" in response.json()["error"]["message"]


def test_broken_configured_location_is_still_an_error(circuit_app):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("DELETE FROM CompanyLocation WHERE CompanyLocationID=1001")
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502
    assert "missing racks" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "resource",
    [
        "circuits",
        "circuit_layouts",
        "ports",
        "devices",
        "racks",
        "locations",
        "circuit_layout_items",
    ],
)
def test_required_path_mappings_cannot_be_omitted(circuit_app, resource):
    app, client_id, _, circuit_id, _, _, _ = circuit_app
    del app.state.source_database._mapping.resources[resource]
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502


def test_secondary_patch_ports_are_parent_authorized_and_opaque(circuit_app):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("UPDATE Patch SET PortID_A_sec=202, PortID_B_sec=101 WHERE PatchID=501")
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200
    patch = response.json()["path"]["items"][1]
    assert patch["port_a_secondary_id"] == patch["port_b"]["id"]
    assert patch["port_b_secondary_id"] == patch["port_a"]["id"]
    assert patch["port_a_secondary"]["device"]["name"] == "edge-z"


@pytest.mark.parametrize("suffix", ["", "/path"])
@pytest.mark.parametrize("as_text", [False, True])
def test_zero_secondary_patch_ports_do_not_break_direct_circuit_reads(
    circuit_app, suffix, as_text, monkeypatch
):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("UPDATE Patch SET PortID_A_sec=0, PortID_B_sec=0 WHERE PatchID=501")
    if as_text:
        source = app.state.source_database
        fetch_matching = source.fetch_matching

        def text_secondary_ids(resource, *args, **kwargs):
            rows = fetch_matching(resource, *args, **kwargs)
            if resource == "patches":
                for row in rows:
                    row["port_a_secondary_id"] = "0"
                    row["port_b_secondary_id"] = "0"
            return rows

        monkeypatch.setattr(source, "fetch_matching", text_secondary_ids)
    with TestClient(app) as client:
        response = client.get(
            "/v1/circuits/" + circuit_id + suffix, headers=token(client, client_id)
        )
    assert response.status_code == 200, response.text
    path = response.json()["path"] if not suffix else response.json()
    patch = path["items"][1]
    assert patch["port_a_secondary"] is None
    assert patch["port_b_secondary"] is None
    assert patch["port_a_secondary_id"] is None
    assert patch["port_b_secondary_id"] is None


@pytest.mark.parametrize("suffix", ["", "/path"])
def test_dark_fiber_segment_is_resolved_inside_the_authorized_path(circuit_app, suffix):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("INSERT INTO CircuitLayoutItem VALUES(5,42,25,'NewDarkFiberCircuit',701)")
    with TestClient(app) as client:
        response = client.get(
            "/v1/circuits/" + circuit_id + suffix, headers=token(client, client_id)
        )
    assert response.status_code == 200, response.text
    path = response.json()["path"] if not suffix else response.json()
    segment = path["items"][2]
    assert segment["kind"] == "dark_fiber"
    assert segment["name"] == "Example dark fiber segment"
    assert segment["resource_id"].startswith("dark_fiber_")
    assert segment["port"] is None and segment["port_a"] is None


def test_unmapped_dark_fiber_segment_is_an_explicit_error(circuit_app):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("INSERT INTO CircuitLayoutItem VALUES(5,42,25,'NewDarkFiberCircuit',701)")
    del app.state.source_database._mapping.resources["new_dark_fiber_circuits"]
    with TestClient(app) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 502
    assert "source mapping for new_dark_fiber_circuits" in response.json()["error"]["message"]


@pytest.mark.parametrize("suffix", ["", "/path"])
def test_direct_api_remains_strict_for_unknown_path_items(circuit_app, suffix):
    app, client_id, _, circuit_id, source_path, _, _ = circuit_app
    with sqlite3.connect(source_path) as db:
        db.execute("UPDATE CircuitLayoutItem SET TableName='FutureTransport'")
    with TestClient(app) as client:
        response = client.get(
            "/v1/circuits/" + circuit_id + suffix, headers=token(client, client_id)
        )
    assert response.status_code == 502
    assert "unsupported item type" in response.json()["error"]["message"]


def test_mapper_catalog_and_legacy_mapping_preservation(circuit_app):
    from test_admin import login

    app, _, _, _, source_path, _, _ = circuit_app
    app.state.admin_store.set_password("operator", "operator-password")
    legacy = json.dumps(
        {
            "resources": {
                "circuits": {"table": "LitCircuit", "columns": {"source_id": "LitCircuitID"}},
                "company_locations": {
                    "table": "CompanyLocation",
                    "columns": {"source_id": "CompanyLocationID", "name": "RackName"},
                },
                "devices": {
                    "table": "Device",
                    "columns": {
                        "source_id": "DeviceID",
                        "company_location_id": "CompanyLocationID",
                    },
                },
            }
        }
    )
    repo = app.state.repository
    repo.put_source_database_profile(
        name="legacy",
        driver="sqlite",
        database_name=str(source_path),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=legacy,
    )
    with TestClient(app, base_url="http://localhost:8000") as client:
        login(client)
        response = client.get("/admin/mapping/data?profile=legacy")
        assert response.status_code == 200
        body = response.json()
        assert set(body["fields"]) == set(SOURCE_RESOURCES)
        assert "address1" in body["fields"]["locations"]
        assert "name" not in body["fields"]["locations"]
        assert "rack_id" in body["required_fields"]["devices"]
        assert "rack_id" in body["fields"]["devices"]
        assert set(body["groups"]) == {"location", "circuit", "path"}
        assert "devices" in body["target_tables"]
        assert "ports" in body["target_tables"]
        assert "alarms" not in body["target_tables"]
        assert "metrics" not in body["target_tables"]
        assert "optical_rx_dbm" in body["target_tables"]["ports"]["api_fields"]
        assert "management_ip" in body["private_fields"]["devices"]
        assert "ip_address" in body["private_fields"]["ports"]
        assert body["template"]["resources"]["devices"]["columns"]["name"] == "DeviceName"
        template = body["template"]["resources"]
        assert "rack_positions" not in template
        assert "management_ip" not in template["devices"]["columns"]
        assert "service_order_id" not in template["circuits"]["columns"]
        assert "billing_company_id" not in template["circuits"]["columns"]
        assert "ip_address" not in template["ports"]["columns"]
        assert repo.get_source_database_profile("legacy").mapping_json == legacy
        assert body["mapping"]["resources"]["circuits"]["columns"] == {"source_id": "LitCircuitID"}
        assert "racks" in body["mapping"]["resources"]
        assert "company_locations" not in body["mapping"]["resources"]
        assert body["mapping"]["resources"]["devices"]["columns"]["rack_id"] == "CompanyLocationID"
        assert set(body["hidden_fields"]["circuits"]) == {
            "active",
            "a_company_location_id",
            "z_company_location_id",
        }
        assert not set(body["hidden_fields"]["circuits"]) & set(body["fields"]["circuits"])
        assert not set(body["hidden_fields"]["circuits"]) & set(template["circuits"]["columns"])
        assert template["racks"]["columns"]["name"] == "RackName"


def test_ocp_catalog_covers_inventory_and_device_port_monitoring_only():
    from open_carrier_portal.models import (
        CircuitDeviceStatus,
        CircuitPortStatus,
    )
    from open_carrier_portal.source_contract import OCP_TABLES

    assert set(OCP_TABLES) == {
        "circuits",
        "devices",
        "ports",
        "locations",
        "companies",
        "racks",
        "rack_positions",
        "services",
        "paths",
        "path_steps",
        "patches",
        "pseudowires",
        "new_dark_fiber_circuits",
    }
    inventory = [resource for table in OCP_TABLES.values() for resource in table["resources"]]
    assert sorted(inventory) == sorted(SOURCE_RESOURCES)
    assert OCP_TABLES["locations"]["resources"] == ("locations",)
    assert OCP_TABLES["racks"]["resources"] == ("racks",)
    assert OCP_TABLES["rack_positions"]["resources"] == ("rack_positions",)
    assert set(OCP_TABLES["devices"]["api_fields"]) == {
        "monitoring_state",
        *CircuitDeviceStatus.model_fields,
    }
    assert set(OCP_TABLES["ports"]["api_fields"]) == {
        "monitoring_state",
        *CircuitPortStatus.model_fields,
    }
    assert all(table["resources"] for table in OCP_TABLES.values())


def test_ocp_table_explainers_cover_catalog_and_match_example_mapping():
    from open_carrier_portal.source_contract import OCP_TABLES

    template = SourceMapping.nettool_defaults()
    for table in OCP_TABLES.values():
        assert table["help"]
        assert table["examples"]
        for source_column, target in table["examples"]:
            resource, field = target.split(".")
            assert resource in table["resources"]
            assert field in SOURCE_RESOURCES[resource].fields
            mapping = template.resources[resource]
            assert source_column == mapping.table + "." + mapping.columns[field]
    assert dict(OCP_TABLES["rack_positions"]["examples"]) == {
        "RackUnit.RackUnitID": "rack_positions.source_id",
        "RackUnit.RackUnitName": "rack_positions.name",
        "RackUnit.DeviceID": "rack_positions.device_id",
        "RackUnit.CompanyLocationID": "rack_positions.rack_id",
    }


@pytest.mark.parametrize("visibility", ["summary", "endpoints", "full_shared_path"])
def test_minimal_circuit_sharing_does_not_need_database_mirror(circuit_app, tmp_path, visibility):
    from open_carrier_portal.source_database import ResourceMapping

    app, client_id, partner, circuit_id, _, requests, _ = circuit_app
    database = tmp_path / "minimal.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE circuits (id INTEGER PRIMARY KEY, name TEXT, layout INTEGER,
                                   a_site INTEGER, z_site INTEGER);
            INSERT INTO circuits VALUES (926, 'Minimal circuit', 42, 11, 22);
        """)
        if visibility != "summary":
            db.executescript("""
                CREATE TABLE routes (id INTEGER PRIMARY KEY, a INTEGER, z INTEGER);
                CREATE TABLE ports (id INTEGER PRIMARY KEY, name TEXT, device INTEGER);
                CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT, site INTEGER);
                CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT, address INTEGER);
                CREATE TABLE addresses (id INTEGER PRIMARY KEY, street TEXT);
                INSERT INTO routes VALUES (42, 101, 202);
                INSERT INTO ports VALUES (101, 'Port A', 1), (202, 'Port Z', 2);
                INSERT INTO devices VALUES (1, 'Device A', 11), (2, 'Device Z', 22);
                INSERT INTO sites VALUES (11, 'Site A', 100), (22, 'Site Z', 200);
                INSERT INTO addresses VALUES (100, 'A Street'), (200, 'Z Street');
            """)
        if visibility == "full_shared_path":
            db.executescript("""
                CREATE TABLE steps (id INTEGER, route INTEGER, position INTEGER,
                                    type TEXT, component INTEGER);
                INSERT INTO steps VALUES (1, 42, 10, 'Port', 101), (2, 42, 20, 'Port', 202);
            """)
    resources = {"circuits": ResourceMapping("circuits", {"source_id": "id", "name": "name"})}
    if visibility != "summary":
        resources["circuits"] = ResourceMapping(
            "circuits",
            {
                "source_id": "id",
                "name": "name",
                "circuit_layout_id": "layout",
            },
        )
        resources["circuit_layouts"] = ResourceMapping(
            "routes", {"source_id": "id", "port_a_id": "a", "port_z_id": "z"}
        )
        resources["ports"] = ResourceMapping(
            "ports", {"source_id": "id", "name": "name", "device_id": "device"}
        )
        resources["devices"] = ResourceMapping(
            "devices", {"source_id": "id", "name": "name", "rack_id": "site"}
        )
        resources["racks"] = ResourceMapping(
            "sites", {"source_id": "id", "name": "name", "location_id": "address"}
        )
        resources["locations"] = ResourceMapping(
            "addresses", {"source_id": "id", "address1": "street"}
        )
    if visibility == "full_shared_path":
        resources["circuit_layout_items"] = ResourceMapping(
            "steps",
            {
                "source_id": "id",
                "circuit_layout_id": "route",
                "position": "position",
                "item_type": "type",
                "item_source_id": "component",
            },
        )
    app.state.repository.share_circuit(
        partner_id=partner,
        source_circuit_id="926",
        visibility=visibility,
    )
    minimal = create_app(
        app.state.settings,
        repository=app.state.repository,
        adapter=None,
        source_database=SourceDatabase(
            SQLiteSourceDatabaseSettings(database), SourceMapping(resources)
        ),
    )
    with TestClient(minimal) as client:
        response = client.get("/v1/circuits/" + circuit_id, headers=token(client, client_id))
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Minimal circuit"
    if visibility == "summary":
        assert response.json()["path"] is None
    else:
        assert [item["name"] for item in response.json()["path"]["items"]] == ["Port A", "Port Z"]
        first = response.json()["path"]["items"][0]["port"]
        assert first["device"]["site"]["location"]["address1"] == "A Street"
        assert first["device"]["site"]["rack_name"] == "Site A"
        assert first["monitoring_state"] == "not_linked"
    assert requests == []
