from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from test_live_transport import execute, job
from test_live_transport import live_source as live_source
from test_owner_browsing import enable, owner_job

from open_carrier_portal.live_contract import LiveJob, OwnerExploreRequest
from open_carrier_portal.owner_access import approve_owner_source
from open_carrier_portal.source_database import SourceDatabase


def approve(fixture):
    settings, repo, connection, _, _ = fixture
    approve_owner_source(
        repo,
        connection,
        metadata_path=settings.database_path,
        profile_name="inventory",
        approved_by="operator",
        explorer_enabled=True,
    )
    return repo.list_portal_owner_sources(connection)[0]


def request(fixture, source, resource="locations", **changes):
    _, _, connection, _, _ = fixture
    return OwnerExploreRequest(
        audience="owner_explore",
        request_id=uuid4(),
        connector_id=connection.connector_id,
        owner_organization_id=connection.organization_id,
        expires_at=time.time() + 20,
        source_profile_id=source.source_profile_id,
        grant_id=source.grant_id,
        resource=resource,
        **changes,
    )


def test_site_rack_device_port_and_related_circuit_navigation(live_source):
    source = approve(live_source)
    assert source.explorer_enabled
    sites = execute(live_source, request(live_source, source)).inventory
    assert len(sites.items) == 1 and "Main Street" in sites.items[0].name
    site = execute(
        live_source,
        request(
            live_source,
            source,
            record_id=sites.items[0].id,
        ),
    ).inventory
    assert site.selected.name == sites.items[0].name
    assert site.listing_resource == "racks" and len(site.items) == 2
    rack = execute(
        live_source,
        request(
            live_source,
            source,
            "racks",
            record_id=site.items[0].id,
        ),
    ).inventory
    assert rack.listing_resource == "devices"
    device_ref = next(item for item in rack.items if item.name == "edge-a")
    device = execute(
        live_source,
        request(
            live_source,
            source,
            "devices",
            record_id=device_ref.id,
        ),
    ).inventory
    assert [item.resource for item in device.selected.parents] == ["locations", "racks"]
    assert device.listing_resource == "ports" and device.items[0].name == "xe-0/0/1"
    port = execute(
        live_source,
        request(
            live_source,
            source,
            "ports",
            record_id=device.items[0].id,
        ),
    ).inventory
    assert [item.resource for item in port.selected.parents] == ["locations", "racks", "devices"]
    assert [item.name for item in port.items] == ["Example circuit", "Unshared circuit"]
    circuit = owner_job(live_source, source).model_copy(
        update={
            "operation": "circuits.read",
            "circuit_id": port.items[0].id,
        }
    )
    detail = execute(live_source, circuit).circuit
    assert detail.inventory_links
    assert detail.endpoints[0].port_id == port.selected.id
    assert detail.endpoints[0].device_id == device_ref.id
    assert detail.endpoints[0].rack.id == rack.selected.id
    assert detail.endpoints[0].location.id == site.selected.id
    serialized = port.model_dump_json() + detail.model_dump_json()
    for private in ("192.0.2.1", "198.51.100.1", "private-order", "api_token", "management_ip"):
        assert private not in serialized
    assert b"Example circuit" not in live_source[0].database_path.read_bytes()


def test_existing_circuit_consent_does_not_inherit_hierarchy_access(live_source):
    old = enable(live_source)
    assert not old.explorer_enabled
    assert execute(live_source, request(live_source, old)).error == "access_denied"
    assert execute(live_source, owner_job(live_source, old)).error is None
    current = approve(live_source)
    assert current.grant_id != old.grant_id
    assert execute(live_source, request(live_source, old)).error == "access_denied"
    assert execute(live_source, request(live_source, current)).inventory is not None
    partner = execute(live_source, job(live_source))
    assert len(partner.page.items) == 1
    detail = execute(
        live_source,
        job(
            live_source,
            operation="circuits.read",
            circuit_id=partner.page.items[0].id,
        ),
    ).circuit
    assert not detail.inventory_links
    assert detail.endpoints[0].rack is None and detail.endpoints[0].location is None


def test_explorer_list_projects_safe_service_company_status_and_address(live_source, monkeypatch):
    source = approve(live_source)
    calls = []
    fetch = SourceDatabase.fetch_by_ids

    def tracked(self, resource_type, identifiers):
        calls.append((resource_type, identifiers))
        return fetch(self, resource_type, identifiers)

    monkeypatch.setattr(SourceDatabase, "fetch_by_ids", tracked)
    view = execute(live_source, request(live_source, source, "circuits")).inventory
    assert len(view.items) == 2
    item = view.items[0]
    fields = {field.label: field.value for field in item.attributes}
    assert fields["Inventory status"] == "Active" and fields["Service"]
    assert item.parents[0].resource == "companies"
    assert [resource for resource, _ in calls] == ["companies", "services"]
    assert len(calls[0][1]) == 2  # Batched, not a per-circuit database query.
    locations = execute(live_source, request(live_source, source)).inventory
    assert any(field.label == "Address1" for field in locations.items[0].attributes)
    serialized = view.model_dump_json()
    for private in ("private-order", "service_order", "management_ip", "api_token"):
        assert private not in serialized
    assert b"Example circuit" not in live_source[0].database_path.read_bytes()


def test_unknown_inventory_state_is_not_reported_as_active(live_source):
    source = approve(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET Active=NULL WHERE LitCircuitID=926")
    item = execute(live_source, request(live_source, source, "circuits")).inventory.items[0]
    assert (
        next(field.value for field in item.attributes if field.label == "Inventory status")
        == "Unknown"
    )
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET Active='invalid' WHERE LitCircuitID=926")
    result = execute(live_source, request(live_source, source, "circuits"))
    assert result.error is None and len(result.inventory.items) == 2
    assert result.inventory.issues
    assert (
        next(
            field.value for field in result.inventory.items[0].attributes
            if field.label == "Inventory status"
        ) == "Unknown"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE Device SET CompanyLocationID=NULL WHERE DeviceID=301",
        "UPDATE Device SET CompanyLocationID=1842 WHERE DeviceID=301",
        "DELETE FROM CompanyLocation WHERE CompanyLocationID=1001",
        "DELETE FROM Location",
        "DROP TABLE Location",
    ],
)
def test_missing_ancestry_retains_selected_device_and_child_ports(live_source, statement):
    source = approve(live_source)
    devices = execute(live_source, request(live_source, source, "devices")).inventory
    selected = next(item for item in devices.items if item.name == "edge-a")
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(statement)
    result = execute(
        live_source, request(live_source, source, "devices", record_id=selected.id)
    )
    assert result.error is None
    assert result.inventory.selected.id == selected.id
    assert result.inventory.items[0].name == "xe-0/0/1"
    assert result.inventory.issues


def test_missing_child_table_keeps_selected_device_with_explicit_warning(live_source):
    source = approve(live_source)
    devices = execute(live_source, request(live_source, source, "devices")).inventory
    selected = next(item for item in devices.items if item.name == "edge-a")
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("DROP TABLE Port")
    result = execute(
        live_source, request(live_source, source, "devices", record_id=selected.id)
    )
    assert result.error is None
    assert result.inventory.selected.id == selected.id
    assert result.inventory.items == [] and result.inventory.issues
    assert execute(live_source, request(live_source, source, "ports")).error == "unavailable"


@pytest.mark.parametrize("statement", ["DELETE FROM Service", "DROP TABLE Service"])
def test_missing_service_enrichment_does_not_break_inventory_list(live_source, statement):
    source = approve(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(statement)
    result = execute(live_source, request(live_source, source, "circuits"))
    assert result.error is None and len(result.inventory.items) == 2
    assert result.inventory.issues
    assert all(
        field.label != "Service" for item in result.inventory.items for field in item.attributes
    )


def test_long_inventory_display_fields_are_shortened_with_explicit_warning(live_source):
    source = approve(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET LitCircuitName=?, Description=? WHERE LitCircuitID=926",
                   ("N" * 800, "D" * 5000))
    result = execute(live_source, request(live_source, source, "circuits"))
    assert result.error is None and len(result.inventory.items) == 2
    assert len(result.inventory.items[0].name) == 512
    assert any(len(field.value) == 4000 for field in result.inventory.items[0].attributes)
    assert result.inventory.issues


def test_search_pagination_and_company_relationships(live_source):
    source = approve(live_source)
    companies = execute(live_source, request(live_source, source, "companies", limit=1)).inventory
    assert len(companies.items) == 1 and companies.next_offset == 1
    company = execute(
        live_source,
        request(
            live_source,
            source,
            "companies",
            record_id=companies.items[0].id,
        ),
    ).inventory
    assert company.listing_resource == "racks" and len(company.items) == 1
    assert [relation.resource for relation in company.relations] == ["circuits", "devices"]
    circuits = execute(
        live_source,
        request(
            live_source,
            source,
            "circuits",
            parent_resource="companies",
            parent_id=companies.items[0].id,
        ),
    ).inventory
    assert [item.name for item in circuits.items] == ["Example circuit"]
    assert execute(
        live_source,
        request(
            live_source,
            source,
            query="Main",
        ),
    ).inventory.items
    assert not execute(
        live_source,
        request(
            live_source,
            source,
            query="not present",
        ),
    ).inventory.items
    for query in ("%' OR 1=1 --", "%", "_"):
        assert (
            execute(
                live_source,
                request(
                    live_source,
                    source,
                    "circuits",
                    query=query,
                ),
            ).inventory.items
            == []
        )
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET LitCircuitName='Circuit_100%!' WHERE LitCircuitID=926")
    assert (
        len(
            execute(
                live_source,
                request(
                    live_source,
                    source,
                    "circuits",
                    query="100%!",
                ),
            ).inventory.items
        )
        == 1
    )


def test_port_circuits_include_path_membership_and_deduplicate_endpoints(live_source):
    source = approve(live_source)
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("INSERT INTO CircuitLayout VALUES(43, 'Intermediate path', 202, 202)")
        db.execute("INSERT INTO CircuitLayoutItem VALUES(9,43,10,'Port',101)")
        db.execute(
            "INSERT INTO LitCircuit SELECT 928,'Intermediate circuit',7,ServiceID,"
            "Description,CompanyLocationA,CompanyLocationZ,ServiceOrderID,43,Active "
            "FROM LitCircuit WHERE LitCircuitID=926"
        )
    ports = execute(live_source, request(live_source, source, "ports", query="xe-0/0/1")).inventory
    related = execute(
        live_source,
        request(
            live_source,
            source,
            "ports",
            record_id=ports.items[0].id,
        ),
    ).inventory
    assert [item.name for item in related.items] == [
        "Example circuit",
        "Unshared circuit",
        "Intermediate circuit",
    ]


def test_stopping_explorer_during_source_query_discards_view(live_source, monkeypatch):
    source = approve(live_source)
    fetch = SourceDatabase.fetch_explorer_page
    _, repo, connection, _, _ = live_source

    def stop(database, *args, **kwargs):
        rows = fetch(database, *args, **kwargs)
        repo.stop_portal_owner_source(connection, source_profile_id=str(source.source_profile_id))
        return rows

    monkeypatch.setattr(SourceDatabase, "fetch_explorer_page", stop)
    result = execute(live_source, request(live_source, source))
    assert result.error == "access_denied" and result.inventory is None


def test_unknown_ids_foreign_grants_and_invalid_contracts_are_rejected(live_source):
    source = approve(live_source)
    base = request(live_source, source)
    for changes in (
        {"owner_organization_id": uuid4()},
        {"connector_id": uuid4()},
        {"grant_id": uuid4()},
        {"source_profile_id": uuid4()},
    ):
        assert execute(live_source, base.model_copy(update=changes)).error == "access_denied"
    missing = execute(
        live_source,
        request(
            live_source,
            source,
            record_id="location_" + "a" * 24,
        ),
    )
    assert missing.error == "not_found"
    for changes in (
        {"resource": "users"},
        {"query": "x" * 121},
        {"parent_resource": "locations"},
        {"owner_view": True},
        {"limit": 101},
        {"offset": -1},
    ):
        with pytest.raises(ValidationError):
            OwnerExploreRequest.model_validate({**base.model_dump(), **changes})
    partner = job(live_source).model_dump()
    with pytest.raises(ValidationError):
        TypeAdapter(LiveJob).validate_python({**partner, "audience": "owner_explore"})


def test_existing_owner_approval_migrates_without_broader_consent(live_source):
    _, repo, connection, _, _ = live_source
    source = enable(live_source)
    with closing(sqlite3.connect(live_source[0].database_path)) as db, db:
        db.execute("ALTER TABLE portal_owner_sources DROP COLUMN explorer_enabled")
    repo.initialize()
    restored = repo.list_portal_owner_sources(connection)[0]
    assert restored.grant_id == source.grant_id and not restored.explorer_enabled
    assert execute(live_source, request(live_source, restored)).error == "access_denied"
