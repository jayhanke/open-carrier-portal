from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from test_live_transport import execute, job
from test_live_transport import live_source as live_source

from open_carrier_portal.circuits import CircuitReader
from open_carrier_portal.errors import (
    AuthorizationError,
    ConfigurationError,
    ConnectorError,
    SourceDatabaseQueryError,
)
from open_carrier_portal.live_contract import LiveJob, OwnerLiveRequest, OwnerSourcesRequest
from open_carrier_portal.owner_access import approve_owner_source
from open_carrier_portal.source_database import (
    SourceDatabase,
    SourceMapping,
    configured_profile_database,
)


def enable(fixture):
    settings, repo, connection, _, _ = fixture
    approve_owner_source(
        repo,
        connection,
        metadata_path=settings.database_path,
        profile_name="inventory",
        approved_by="operator",
    )
    return repo.list_portal_owner_sources(connection)[0]


def owner_job(fixture, source=None, **changes):
    _, _, connection, _, _ = fixture
    common = {
        "request_id": str(uuid4()),
        "connector_id": connection.connector_id,
        "owner_organization_id": connection.organization_id,
        "expires_at": time.time() + 20,
        **changes,
    }
    if source is None:
        return OwnerSourcesRequest(audience="owner_sources", **common)
    return OwnerLiveRequest(
        audience="owner",
        source_profile_id=source.source_profile_id,
        grant_id=source.grant_id,
        operation="circuits.list",
        **common,
    )


def test_full_inventory_is_explicit_and_never_relaxes_partner_filter(live_source):
    catalog = execute(live_source, owner_job(live_source))
    assert catalog.sources.items == []
    source = enable(live_source)
    assert execute(live_source, owner_job(live_source)).sources.items == [source]
    request = owner_job(live_source, source)
    page = execute(live_source, request).page
    assert [item.name for item in page.items] == ["Example circuit", "Unshared circuit"]
    assert [item.description for item in page.items] == ["Read-only shared circuit", "Private"]
    assert [item.billing_company_name for item in page.items] == [
        "Example Carrier", "Example Partner",
    ]
    first = execute(live_source, request.model_copy(update={"limit": 1})).page
    assert first.next_offset == 1
    second = execute(live_source, request.model_copy(update={"limit": 1, "offset": 1})).page
    assert second.items[0].name == "Unshared circuit" and second.next_offset is None
    partner = execute(live_source, job(live_source)).page
    assert [item.name for item in partner.items] == ["Example circuit"]
    assert partner.items[0].id != page.items[0].id
    detail = execute(
        live_source,
        request.model_copy(
            update={
                "operation": "circuits.read",
                "circuit_id": page.items[0].id,
            }
        ),
    )
    assert detail.error is None
    assert [item.position for item in detail.circuit.endpoints] == ["a", "z"]
    assert all(item.port_status.state == "unavailable" for item in detail.circuit.endpoints)
    assert b"Example circuit" not in live_source[0].database_path.read_bytes()


def test_circuit_only_owner_grant_allows_scoped_search_not_hierarchy(live_source):
    source = enable(live_source)
    assert not source.explorer_enabled
    request = owner_job(live_source, source, query="927", order="name_desc")
    result = execute(live_source, request)
    assert result.error is None
    assert [item.name for item in result.page.items] == ["Unshared circuit"]
    partner = execute(live_source, job(live_source, query="927", order="name_desc"))
    assert partner.error is None and partner.page.items == []
    live_source[1].stop_portal_owner_source(
        live_source[2], source_profile_id=str(source.source_profile_id)
    )
    assert execute(live_source, request).error == "access_denied"

def test_owner_and_partner_resource_namespaces_cannot_be_interchanged(live_source):
    source = enable(live_source)
    own = owner_job(live_source, source)
    own_id = execute(live_source, own).page.items[0].id
    partner_id = execute(live_source, job(live_source)).page.items[0].id
    assert (
        execute(
            live_source,
            job(
                live_source,
                operation="circuits.read",
                circuit_id=own_id,
            ),
        ).error
        == "not_found"
    )
    assert (
        execute(
            live_source,
            own.model_copy(
                update={
                    "operation": "circuits.read",
                    "circuit_id": partner_id,
                }
            ),
        ).error
        == "not_found"
    )


def test_stop_reenable_and_pairing_changes_do_not_reuse_grants(live_source):
    _, repo, connection, _, _ = live_source
    source = enable(live_source)
    request = owner_job(live_source, source)
    previous_id = execute(live_source, request).page.items[0].id
    repo.stop_portal_owner_source(connection, source_profile_id=str(source.source_profile_id))
    assert execute(live_source, request).error == "access_denied"
    replacement = enable(live_source)
    assert replacement.grant_id != source.grant_id
    assert execute(live_source, request).error == "access_denied"
    current = owner_job(live_source, replacement)
    assert (
        execute(
            live_source,
            current.model_copy(
                update={
                    "operation": "circuits.read",
                    "circuit_id": previous_id,
                }
            ),
        ).error
        == "not_found"
    )
    new_connection = replace(connection, token="r" * 64)
    assert repo.list_portal_owner_sources(new_connection) == []
    with pytest.raises(AuthorizationError, match="owner inventory access is not enabled"):
        repo.require_portal_owner_source(
            new_connection,
            source_profile_id=str(replacement.source_profile_id),
            grant_id=str(replacement.grant_id),
        )


@pytest.mark.parametrize("change", ["mapping", "database", "disabled"])
def test_source_changes_require_fresh_explicit_approval(live_source, change):
    _, repo, connection, _, _ = live_source
    source = enable(live_source)
    profile = repo.get_source_database_profile("inventory")
    values = asdict(profile)
    for key in ("profile_id", "created_at", "updated_at"):
        values.pop(key)
    if change == "mapping":
        mapping = json.loads(profile.mapping_json)
        mapping["resources"]["circuits"]["columns"]["name"] = "ChangedName"
        values["mapping_json"] = json.dumps(mapping)
    elif change == "database":
        values["database_name"] += ".changed"
    else:
        values["enabled"] = False
    repo.put_source_database_profile(**values)
    assert repo.list_portal_owner_sources(connection) == []
    assert execute(live_source, owner_job(live_source, source)).error == "access_denied"
    values.update(
        mapping_json=profile.mapping_json, database_name=profile.database_name, enabled=True
    )
    repo.put_source_database_profile(**values)
    assert repo.list_portal_owner_sources(connection) == []


def test_stopping_owner_access_during_enrichment_discards_data(live_source, monkeypatch):
    _, repo, connection, _, _ = live_source
    source = enable(live_source)
    request = owner_job(live_source, source)
    circuit_id = execute(live_source, request).page.items[0].id
    details = CircuitReader.details

    async def stop(reader, *args, **kwargs):
        result = await details(reader, *args, **kwargs)
        repo.stop_portal_owner_source(connection, source_profile_id=str(source.source_profile_id))
        return result

    monkeypatch.setattr(CircuitReader, "details", stop)
    result = execute(
        live_source,
        request.model_copy(
            update={
                "operation": "circuits.read",
                "circuit_id": circuit_id,
            }
        ),
    )
    assert result.error == "access_denied" and result.circuit is None


def test_owner_cannot_select_an_unapproved_profile_or_foreign_tenant(live_source):
    source = enable(live_source)
    request = owner_job(live_source, source)
    for change in (
        {"source_profile_id": uuid4()},
        {"grant_id": uuid4()},
        {"owner_organization_id": uuid4()},
        {"connector_id": uuid4()},
    ):
        assert execute(live_source, request.model_copy(update=change)).error == "access_denied"
    _, repo, _, _, _ = live_source
    repo.put_connector_identity(repo.get_connector_identity().model_copy(update={"enabled": False}))
    assert execute(live_source, request).error == "access_denied"


def test_owner_billing_company_lookup_is_one_batch_for_only_visible_references(
    live_source, monkeypatch
):
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute(
            "INSERT INTO LitCircuit (LitCircuitID, LitCircuitName, BillingCompanyID) "
            "VALUES (999, 'Another circuit', 7)"
        )
    source = enable(live_source)
    original = SourceDatabase.fetch_by_ids
    calls = []

    def fetch(database, resource, identifiers):
        calls.append((resource, identifiers))
        return original(database, resource, identifiers)

    monkeypatch.setattr(SourceDatabase, "fetch_by_ids", fetch)
    first = execute(live_source, owner_job(live_source, source, limit=1))
    assert first.error is None
    assert calls == [("companies", ["7"])]
    calls.clear()
    full = execute(live_source, owner_job(live_source, source))
    assert full.error is None
    assert calls == [("companies", ["7", "8"])]
    assert [item.billing_company_name for item in full.page.items] == [
        "Example Carrier", "Example Partner", "Example Carrier",
    ]


def test_missing_billing_company_record_keeps_owner_circuits_with_explicit_issue(live_source):
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("DELETE FROM company WHERE Company_ID=8")
    result = execute(live_source, owner_job(live_source, enable(live_source)))
    assert result.error is None and len(result.page.items) == 2
    assert result.page.items[1].billing_company_name is None
    assert result.page.issues == ["Some referenced billing company names are unavailable."]


def test_billing_lookup_failure_is_not_a_silent_empty_company(live_source, monkeypatch, caplog):
    def fail(database, resource, identifiers):
        raise SourceDatabaseQueryError("test billing lookup failure")

    monkeypatch.setattr(SourceDatabase, "fetch_by_ids", fail)
    result = execute(live_source, owner_job(live_source, enable(live_source)))
    assert result.error is None and len(result.page.items) == 2
    assert all(item.billing_company_name is None for item in result.page.items)
    assert result.page.issues == ["Billing company names could not be read."]
    assert "circuit.data_unavailable" in caplog.text


def test_missing_company_mapping_does_not_break_circuit_only_owner_grant(live_source):
    settings, repo, _, _, database = live_source
    mapping = SourceMapping.nettool_defaults()
    del mapping.resources["companies"]
    repo.put_source_database_profile(
        name="inventory", driver="sqlite", database_name=str(database),
        host=None, port=None, username=None, password_env_var=None, tls_ca_path=None,
        timeout_seconds=10, mapping_json=mapping.to_json(),
    )
    result = execute(live_source, owner_job(live_source, enable(live_source)))
    assert result.error is None and len(result.page.items) == 2
    assert all(item.billing_company_name is None for item in result.page.items)
    assert result.page.issues == [
        "Billing company names are unavailable: their mapping is incomplete."
    ]


def test_absent_billing_reference_needs_no_lookup_or_error(live_source, monkeypatch):
    with closing(sqlite3.connect(live_source[4])) as db, db:
        db.execute("UPDATE LitCircuit SET BillingCompanyID=NULL")
    calls = []
    monkeypatch.setattr(
        SourceDatabase, "fetch_by_ids",
        lambda database, resource, identifiers: calls.append(identifiers),
    )
    result = execute(live_source, owner_job(live_source, enable(live_source)))
    assert result.error is None and len(result.page.items) == 2
    assert result.page.issues == [] and calls == []
    assert all(item.billing_company_name is None for item in result.page.items)

def test_request_variants_forbid_partner_owner_switches(live_source):
    data = job(live_source).model_dump(mode="json")
    adapter = TypeAdapter(LiveJob)
    for changes in (
        {"owner_view": True},
        {"audience": "owner"},
        {"audience": "owner_sources"},
        {"recipient_organization_id": data["owner_organization_id"]},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python({**data, **changes})


def test_owner_inventory_helper_does_not_relax_scoped_pagination(live_source):
    settings, repo, _, _, _ = live_source
    source = configured_profile_database(
        repo.get_source_database_profile("inventory"),
        settings.database_path,
    )
    with pytest.raises(ConfigurationError, match="scoped filters"):
        source.fetch_page("circuits", {}, offset=0, limit=1)
    assert len(source.fetch_inventory_page("circuits", offset=0, limit=1)) == 2
    for offset, limit in ((-1, 1), (0, 0), (0, 101), (1_000_000, 1)):
        with pytest.raises(ConfigurationError, match="page bounds"):
            source.fetch_inventory_page("circuits", offset=offset, limit=limit)


def test_source_ids_cannot_be_reinterpreted_in_another_approved_database(live_source):
    settings, repo, connection, _, _ = live_source
    first = enable(live_source)
    circuit_id = execute(live_source, owner_job(live_source, first)).page.items[0].id
    values = asdict(repo.get_source_database_profile("inventory"))
    for key in ("profile_id", "created_at", "updated_at"):
        values.pop(key)
    repo.put_source_database_profile(**{**values, "name": "another-database"})
    approve_owner_source(
        repo,
        connection,
        metadata_path=settings.database_path,
        profile_name="another-database",
        approved_by="operator",
    )
    second = next(
        item
        for item in repo.list_portal_owner_sources(connection)
        if item.name == "another-database"
    )
    request = owner_job(live_source, second)
    assert execute(live_source, request).page.items[0].id != circuit_id
    assert (
        execute(
            live_source,
            request.model_copy(
                update={
                    "operation": "circuits.read",
                    "circuit_id": circuit_id,
                }
            ),
        ).error
        == "not_found"
    )


def test_approval_rejects_bad_mapping_or_oversized_labels_without_partial_grants(live_source):
    settings, repo, connection, _, _ = live_source
    values = asdict(repo.get_source_database_profile("inventory"))
    for key in ("profile_id", "created_at", "updated_at"):
        values.pop(key)
    repo.put_source_database_profile(**{**values, "name": "missing-mapping", "mapping_json": "{}"})
    with pytest.raises((ConnectorError, ConfigurationError)):
        approve_owner_source(
            repo,
            connection,
            metadata_path=settings.database_path,
            profile_name="missing-mapping",
            approved_by="operator",
        )
    repo.put_source_database_profile(**{**values, "name": "x" * 513})
    with pytest.raises(ValueError, match="512 characters"):
        approve_owner_source(
            repo,
            connection,
            metadata_path=settings.database_path,
            profile_name="x" * 513,
            approved_by="operator",
        )
    assert repo.list_portal_owner_sources(connection) == []
