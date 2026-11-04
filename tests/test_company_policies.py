from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from open_carrier_portal.company_access import approve_company_policy, require_company_circuit
from open_carrier_portal.company_policies import CompanyPolicy, PolicySnapshot, source_fingerprint
from open_carrier_portal.errors import AuthorizationError, SourceDatabaseError
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.portal_agent import PortalConnection, send_heartbeat
from open_carrier_portal.repository import Repository


@pytest.fixture
def sharing(tmp_path):
    metadata = tmp_path / "metadata.db"
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            INSERT INTO companies VALUES (42, 'Customer'), (77, 'Other customer');
            CREATE TABLE circuits (id INTEGER PRIMARY KEY, name TEXT, billing_company INTEGER);
            INSERT INTO circuits VALUES (1, 'Our customer circuit', 42), (2, 'Other circuit', 77);
            """
        )
    repo = Repository(metadata, "s" * 48)
    repo.initialize()
    connector = str(uuid4())
    repo.put_connector_identity(
        ConnectorIdentity(
            organization_id=str(uuid4()),
            organization_name="Local owner",
            connector_id=connector,
            issuer_url="http://localhost:8000",
        )
    )
    mapping = json.dumps(
        {
            "resources": {
                "companies": {"table": "companies", "columns": {"source_id": "id", "name": "name"}},
                "circuits": {
                    "table": "circuits",
                    "columns": {
                        "source_id": "id",
                        "name": "name",
                        "billing_company_id": "billing_company",
                    },
                },
            }
        }
    )
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
        mapping_json=mapping,
    )
    portal = PortalConnection("https://portal.example.test", connector, str(uuid4()), "t" * 64)
    policy = CompanyPolicy(
        policy_id=uuid4(),
        revision=1,
        owner_organization_id=portal.organization_id,
        recipient_organization_id=uuid4(),
        recipient_name="Customer <Carrier>",
        connector_id=connector,
        state="pending",
    )
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[policy]))
    return repo, portal, policy, metadata, database


def approve(sharing, **changes):
    repo, portal, policy, metadata, _ = sharing
    approve_company_policy(
        repo,
        portal,
        **(
            {
                "metadata_path": metadata,
                "policy_id": str(policy.policy_id),
                "revision": policy.revision,
                "profile_name": "inventory",
                "company_id": "42",
                "approved_by": "owner",
            }
            | changes
        ),
    )


def activate(sharing):
    repo, portal, policy, _, _ = sharing
    approve(sharing)
    policy = policy.model_copy(update={"state": "active"})
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[policy]))
    return policy


def require(sharing, **changes):
    repo, portal, policy, metadata, _ = sharing
    return require_company_circuit(
        repo,
        portal,
        **(
            {
                "metadata_path": metadata,
                "policy_id": str(policy.policy_id),
                "revision": policy.revision,
                "recipient_organization_id": str(policy.recipient_organization_id),
                "circuit_source_id": "1",
            }
            | changes
        ),
    )


def test_link_and_pending_policy_do_not_grant_access(sharing):
    repo, portal, _, _, _ = sharing
    assert repo.portal_policy_receipts(portal) == []
    with pytest.raises(AuthorizationError):
        require(sharing)
    approve(sharing)
    assert repo.portal_policy_receipts(portal)[0].state == "accepted"
    with pytest.raises(AuthorizationError):
        require(sharing)


def test_live_billing_company_filters_current_future_and_transferred_circuits(sharing):
    _, portal, _, _, database = sharing
    activate(sharing)
    require(sharing)
    with pytest.raises(AuthorizationError):
        require(sharing, circuit_source_id="2")
    with pytest.raises(AuthorizationError):
        require(sharing, recipient_organization_id=portal.organization_id)
    with pytest.raises(AuthorizationError):
        require(sharing, recipient_organization_id=str(uuid4()))
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO circuits VALUES (3, 'New circuit', 42)")
        db.execute("UPDATE circuits SET billing_company = 77 WHERE id = 1")
    require(sharing, circuit_source_id="3")
    with pytest.raises(AuthorizationError):
        require(sharing)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM companies WHERE id = 42")
    with pytest.raises(AuthorizationError):
        require(sharing, circuit_source_id="3")


@pytest.mark.parametrize("company", ["missing", "42 OR 1=1", ""])
def test_company_link_must_exist_in_selected_source(sharing, company):
    with pytest.raises((ValueError, SourceDatabaseError)):
        approve(sharing, company_id=company)
    assert sharing[0].portal_policy_receipts(sharing[1]) == []


def test_mapping_or_source_identity_change_invalidates_approval_immediately(sharing):
    repo, portal, _, _, _ = sharing
    activate(sharing)
    profile = repo.get_source_database_profile("inventory")
    repo.put_source_database_profile(
        name=profile.name,
        driver=profile.driver,
        host=profile.host,
        port=profile.port,
        database_name=profile.database_name,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=profile.mapping_json.replace('"billing_company"', '"id"'),
    )
    with pytest.raises(AuthorizationError):
        require(sharing)
    assert repo.portal_policy_receipts(portal)[0].state == "needs_approval"
    with pytest.raises(ValueError, match="new policy revision"):
        approve(sharing)
    assert source_fingerprint(profile) != source_fingerprint(
        replace(profile, database_name="different-source.db")
    )


def test_exact_policy_lease_boundary_and_revision(sharing, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("open_carrier_portal.repository.time.time", lambda: clock[0])
    activate(sharing)
    with pytest.raises(AuthorizationError):
        require(sharing, revision=2)
    clock[0] += 90
    require(sharing)
    clock[0] += 0.001
    with pytest.raises(AuthorizationError):
        require(sharing)


def test_disabling_and_reenabling_source_cannot_restore_an_old_approval(sharing):
    repo, portal, _, _, _ = sharing
    activate(sharing)
    profile = repo.get_source_database_profile("inventory")
    values = {
        key: getattr(profile, key)
        for key in (
            "name",
            "driver",
            "host",
            "port",
            "database_name",
            "username",
            "password_env_var",
            "tls_ca_path",
            "timeout_seconds",
            "mapping_json",
        )
    }
    repo.put_source_database_profile(**values)
    require(sharing)
    repo.put_source_database_profile(**values, enabled=False)
    repo.put_source_database_profile(**values, enabled=True)
    with pytest.raises(AuthorizationError):
        require(sharing)
    assert repo.portal_policy_receipts(portal)[0].state == "needs_approval"


def test_stop_revision_revocation_and_lease_fail_closed(sharing):
    repo, portal, policy, metadata, _ = sharing
    active = activate(sharing)
    with sqlite3.connect(metadata) as db:
        db.execute("UPDATE portal_company_policies SET synced_at = ?", (time.time() - 91,))
    with pytest.raises(AuthorizationError):
        require(sharing)
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[active]))
    require(sharing)
    repo.stop_portal_policy(portal, policy_id=str(policy.policy_id))
    with pytest.raises(AuthorizationError):
        require(sharing)
    assert repo.portal_policy_receipts(portal)[0].state == "needs_approval"
    newer = active.model_copy(update={"revision": 2, "state": "needs_approval"})
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[newer]))
    approve(sharing, revision=2)
    repo.synchronize_portal_policies(
        portal,
        PolicySnapshot(version=1, policies=[newer.model_copy(update={"state": "active"})]),
    )
    require(sharing, revision=2)
    repo.synchronize_portal_policies(
        portal,
        PolicySnapshot(
            version=1, policies=[newer.model_copy(update={"revision": 3, "state": "revoked"})]
        ),
    )
    with pytest.raises(AuthorizationError):
        require(sharing)
    assert repo.portal_policy_receipts(portal) == []
    with pytest.raises(ValueError, match="obsolete"):
        repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[active]))


def test_repair_with_same_connector_id_cannot_inherit_approval(sharing):
    repo, portal, policy, _, _ = sharing
    active = activate(sharing)
    replaced = replace(portal, token="u" * 64)
    with pytest.raises(AuthorizationError):
        repo.require_portal_company_policy(
            replaced,
            policy_id=str(policy.policy_id),
            revision=policy.revision,
            recipient_organization_id=str(policy.recipient_organization_id),
        )
    repo.synchronize_portal_policies(replaced, PolicySnapshot(version=1, policies=[active]))
    assert repo.list_portal_policies(replaced)[0].receipt is None
    with pytest.raises(AuthorizationError):
        require(sharing)


def test_empty_snapshot_removes_access_and_duplicate_or_cross_tenant_snapshot_rejected(sharing):
    repo, portal, policy, _, _ = sharing
    active = activate(sharing)
    for bad in [
        PolicySnapshot(version=1, policies=[active, active]),
        PolicySnapshot(
            version=1, policies=[active.model_copy(update={"owner_organization_id": uuid4()})]
        ),
        PolicySnapshot(version=1, policies=[active.model_copy(update={"connector_id": uuid4()})]),
    ]:
        with pytest.raises(ValueError):
            repo.synchronize_portal_policies(portal, bad)
        require(sharing)
    changed = active.model_copy(update={"recipient_organization_id": uuid4()})
    with pytest.raises(ValueError, match="new revision"):
        repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[changed]))
    repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[]))
    with pytest.raises(AuthorizationError):
        require(sharing)
    with pytest.raises(ValueError, match="revoked"):
        repo.synchronize_portal_policies(portal, PolicySnapshot(version=1, policies=[active]))
    assert policy.visibility == "endpoints"
    assert policy.resource == "circuits"


def test_heartbeat_metadata_sync_and_exact_receipt(sharing):
    repo, portal, policy, _, _ = sharing
    approve(sharing)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["policy_version"] == 1
        receipt = payload["policy_receipts"][0]
        assert receipt["revision"] == policy.revision
        assert receipt["source_company_id"] == "42"
        assert receipt["state"] == "accepted"
        assert set(payload) == {"connector_id", "policy_version", "policy_receipts"}
        return httpx.Response(
            200,
            json={
                "status": "connected",
                "company_policies": {
                    "version": 1,
                    "policies": [
                        policy.model_copy(update={"state": "active"}).model_dump(mode="json")
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send_heartbeat(client, portal, repository=repo)
    require(sharing)
    assert len(requests) == 1
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "connected"})
            )
        ) as client,
        pytest.raises(ValueError, match="invalid company policy"),
    ):
        send_heartbeat(client, portal, repository=repo)
