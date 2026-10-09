from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from open_carrier_portal.errors import AuthorizationError
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import hash_client_secret
from open_carrier_portal.source_database import SourceMapping


def test_authorization_requires_live_scope_and_grant(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "test.db", "k" * 32)
    repository.initialize()
    partner_id = repository.create_partner("Partner")
    relationship_id = repository.create_active_relationship(partner_id)
    repository.create_grant(
        relationship_id,
        "observium:devices:read",
        {"device_allowed"},
    )
    client_id = repository.create_client(
        partner_id,
        hash_client_secret("secret"),
        {"observium:devices:read"},
    )

    repository.authorize(
        client_id=client_id,
        partner_id=partner_id,
        token_scopes=frozenset({"observium:devices:read"}),
        required_scope="observium:devices:read",
        resource_id="device_allowed",
    )

    with pytest.raises(AuthorizationError):
        repository.authorize(
            client_id=client_id,
            partner_id=partner_id,
            token_scopes=frozenset({"observium:devices:read"}),
            required_scope="observium:devices:read",
            resource_id="device_hidden",
        )

    with pytest.raises(AuthorizationError):
        repository.authorize(
            client_id=client_id,
            partner_id=partner_id,
            token_scopes=frozenset(),
            required_scope="observium:devices:read",
        )


def test_resource_ids_are_stable_and_opaque(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "test.db", "k" * 32)
    repository.initialize()

    first = repository.ocp_resource_id("observium", "device", "123")
    second = repository.ocp_resource_id("observium", "device", "123")

    assert first == second
    assert "123" not in first
    assert repository.source_resource_id(first, "observium", "device") == "123"


def test_circuit_share_is_partner_scoped_and_revocable(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "test.db", "k" * 32)
    repository.initialize()
    partner_id = repository.create_partner("Partner")
    other_partner_id = repository.create_partner("Other partner")
    repository.create_active_relationship(partner_id)
    repository.create_active_relationship(other_partner_id)

    public_circuit_id = repository.share_circuit(
        partner_id=partner_id,
        source_circuit_id="926",
        visibility="sfn_path",
    )

    assert public_circuit_id == repository.ocp_resource_id("nettool", "circuit", "926")
    assert (
        repository.circuit_share_visibility(
            partner_id=partner_id,
            source_circuit_id="926",
        )
        == "sfn_path"
    )
    assert (
        repository.circuit_share_visibility(
            partner_id=other_partner_id,
            source_circuit_id="926",
        )
        is None
    )
    assert repository.revoke_circuit_share(
        partner_id=partner_id,
        source_circuit_id="926",
    )
    assert (
        repository.circuit_share_visibility(
            partner_id=partner_id,
            source_circuit_id="926",
        )
        is None
    )


def test_source_database_profile_stores_secret_reference_only(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    repository = Repository(database_path, "k" * 32)
    repository.initialize()

    profile_id = repository.put_source_database_profile(
        name="nettool",
        driver="mysql",
        host="nettool-db.internal",
        port=3306,
        database_name="nettool",
        username="ocp_reader",
        password_env_var="NETTOOL_PASSWORD",
        tls_ca_path=r"C:\certs\mysql-ca.pem",
        timeout_seconds=10,
        mapping_json=SourceMapping.nettool_defaults().to_json(),
    )
    profile = repository.get_source_database_profile("nettool")

    assert profile is not None
    assert profile.profile_id == profile_id
    assert profile.password_env_var == "NETTOOL_PASSWORD"
    assert profile.database_name == "nettool"
    assert profile.allow_insecure is False
    assert b"database-password" not in database_path.read_bytes()


def test_source_database_profile_persists_explicit_insecure_opt_in(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "test.db", "k" * 32)
    repository.initialize()

    repository.put_source_database_profile(
        name="nettool",
        driver="mysql",
        host="10.210.4.229",
        port=3306,
        database_name="NPDB",
        username="ocp_reader",
        password_env_var="OCP_SOURCE_DB_PASSWORD",
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=SourceMapping.nettool_defaults().to_json(),
        allow_insecure=True,
    )

    profile = repository.get_source_database_profile("nettool")

    assert profile is not None
    assert profile.allow_insecure is True
    assert profile.tls_ca_path is None


def test_initialize_migrates_partial_source_profile_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE source_database_profiles (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                driver TEXT NOT NULL,
                database_path TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_database_profiles
            VALUES ('profile', 'legacy', 'sqlite', 'nettool.db', '{}', 1, 'now', 'now')
            """
        )

    repository = Repository(database_path, "k" * 32)
    repository.initialize()
    profile = repository.get_source_database_profile("legacy")

    assert profile is not None
    assert profile.database_name == "nettool.db"
    assert profile.timeout_seconds == 10
    assert profile.allow_insecure is False
