from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from open_carrier_portal.errors import AuthenticationError, AuthorizationError
from open_carrier_portal.models import ConnectorIdentity, RelationshipState
from open_carrier_portal.security import ALLOWED_SCOPES

_CIRCUIT_VISIBILITIES = {
    "summary",
    "endpoints",
    "sfn_path",
    "full_shared_path",
    "metrics",
}


@dataclass(frozen=True, slots=True)
class ClientRecord:
    client_id: str
    partner_id: str
    secret_hash: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class SourceDatabaseProfile:
    profile_id: str
    name: str
    driver: str
    host: str | None
    port: int | None
    database_name: str
    username: str | None
    password_env_var: str | None
    tls_ca_path: str | None
    allow_insecure: bool
    timeout_seconds: int
    mapping_json: str
    enabled: bool
    created_at: str
    updated_at: str


class Repository:
    def __init__(self, database_path: Path, resource_id_key: str) -> None:
        self._database_path = database_path
        self._resource_id_key = resource_id_key.encode("utf-8")
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS connector_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    organization_id TEXT NOT NULL,
                    organization_name TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    issuer_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS partners (
                    partner_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
                    secret_hash TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS relationships (
                    relationship_id TEXT PRIMARY KEY,
                    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
                    state TEXT NOT NULL,
                    owner_approved_at TEXT,
                    partner_approved_at TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    relationship_id TEXT NOT NULL REFERENCES relationships(relationship_id),
                    scope TEXT NOT NULL,
                    resource_ids_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS resource_ids (
                    ocp_id TEXT PRIMARY KEY,
                    integration TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    UNIQUE(integration, resource_type, source_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    partner_id TEXT,
                    client_id TEXT,
                    operation TEXT NOT NULL,
                    resource_summary TEXT,
                    outcome TEXT NOT NULL,
                    correlation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_database_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    driver TEXT NOT NULL,
                    host TEXT,
                    port INTEGER,
                    database_name TEXT NOT NULL,
                    username TEXT,
                    password_env_var TEXT,
                    tls_ca_path TEXT,
                    allow_insecure INTEGER NOT NULL DEFAULT 0,
                    timeout_seconds INTEGER NOT NULL,
                    mapping_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuit_shares (
                    share_id TEXT PRIMARY KEY,
                    circuit_source_id TEXT NOT NULL,
                    partner_id TEXT NOT NULL REFERENCES partners(partner_id),
                    visibility TEXT NOT NULL CHECK (
                        visibility IN ('summary', 'endpoints', 'sfn_path',
                                       'full_shared_path', 'metrics')
                    ),
                    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                    shared_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_by_id TEXT,
                    UNIQUE(partner_id, circuit_source_id)
                );
                CREATE INDEX IF NOT EXISTS circuit_shares_partner_status
                    ON circuit_shares(partner_id, status, circuit_source_id);
                """
            )
            self._migrate_source_database_profiles(connection)

    @staticmethod
    def _migrate_source_database_profiles(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(source_database_profiles)")
        }
        if "database_path" in columns:
            connection.executescript(
                """
                DROP TABLE IF EXISTS source_database_profiles_legacy;
                ALTER TABLE source_database_profiles RENAME TO source_database_profiles_legacy;
                CREATE TABLE source_database_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    driver TEXT NOT NULL,
                    host TEXT,
                    port INTEGER,
                    database_name TEXT NOT NULL,
                    username TEXT,
                    password_env_var TEXT,
                    tls_ca_path TEXT,
                    allow_insecure INTEGER NOT NULL DEFAULT 0,
                    timeout_seconds INTEGER NOT NULL,
                    mapping_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO source_database_profiles (
                    profile_id, name, driver, host, port, database_name, username,
                    password_env_var, tls_ca_path, allow_insecure, timeout_seconds,
                    mapping_json, enabled, created_at, updated_at
                )
                SELECT
                    profile_id, name, driver, NULL, NULL, database_path, NULL,
                    NULL, NULL, 0, 10, mapping_json, enabled, created_at, updated_at
                FROM source_database_profiles_legacy;
                DROP TABLE source_database_profiles_legacy;
                """
            )
        current_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(source_database_profiles)")
        }
        if "allow_insecure" not in current_columns:
            connection.execute(
                "ALTER TABLE source_database_profiles "
                "ADD COLUMN allow_insecure INTEGER NOT NULL DEFAULT 0"
            )

    def put_source_database_profile(
        self,
        *,
        name: str,
        driver: str,
        host: str | None,
        port: int | None,
        database_name: str,
        username: str | None,
        password_env_var: str | None,
        tls_ca_path: str | None,
        timeout_seconds: int,
        mapping_json: str,
        allow_insecure: bool = False,
        enabled: bool = True,
    ) -> str:
        if not name.strip():
            raise ValueError("source database profile name is required")
        if driver not in {"mysql", "sqlite"}:
            raise ValueError("source database driver must be mysql or sqlite")
        if not database_name.strip():
            raise ValueError("source database name is required")
        if timeout_seconds < 1:
            raise ValueError("source database timeout must be at least 1 second")
        if driver == "mysql" and (not host or port is None or not username or not password_env_var):
            raise ValueError(
                "mysql source profiles require host, port, username, and password environment"
            )
        if driver == "mysql":
            is_remote = host.lower() not in {"localhost", "127.0.0.1", "::1"}
            if tls_ca_path is not None and allow_insecure:
                raise ValueError("TLS CA and insecure connection settings are mutually exclusive")
            if is_remote and tls_ca_path is None and not allow_insecure:
                raise ValueError(
                    "remote mysql source profiles require a TLS CA or explicit insecure opt-in"
                )
        elif allow_insecure:
            raise ValueError("insecure connection opt-in applies only to remote MySQL profiles")
        profile_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT profile_id FROM source_database_profiles WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                profile_id = str(existing["profile_id"])
                connection.execute(
                    """
                    UPDATE source_database_profiles
                    SET driver = ?, host = ?, port = ?, database_name = ?, username = ?,
                        password_env_var = ?, tls_ca_path = ?, allow_insecure = ?,
                        timeout_seconds = ?,
                        mapping_json = ?, enabled = ?, updated_at = ?
                    WHERE profile_id = ?
                    """,
                    (
                        driver,
                        host,
                        port,
                        database_name,
                        username,
                        password_env_var,
                        tls_ca_path,
                        int(allow_insecure),
                        timeout_seconds,
                        mapping_json,
                        int(enabled),
                        now,
                        profile_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO source_database_profiles
                        (profile_id, name, driver, host, port, database_name, username,
                         password_env_var, tls_ca_path, allow_insecure, timeout_seconds,
                         mapping_json, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        name,
                        driver,
                        host,
                        port,
                        database_name,
                        username,
                        password_env_var,
                        tls_ca_path,
                        int(allow_insecure),
                        timeout_seconds,
                        mapping_json,
                        int(enabled),
                        now,
                        now,
                    ),
                )
        return profile_id

    def get_source_database_profile(self, name: str) -> SourceDatabaseProfile | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT profile_id, name, driver, host, port, database_name, username,
                       password_env_var, tls_ca_path, allow_insecure, timeout_seconds,
                       mapping_json, enabled, created_at, updated_at
                FROM source_database_profiles WHERE name = ?
                """,
                (name,),
            ).fetchone()
        if row is None:
            return None
        return SourceDatabaseProfile(
            profile_id=str(row["profile_id"]),
            name=str(row["name"]),
            driver=str(row["driver"]),
            host=None if row["host"] is None else str(row["host"]),
            port=None if row["port"] is None else int(row["port"]),
            database_name=str(row["database_name"]),
            username=None if row["username"] is None else str(row["username"]),
            password_env_var=(
                None if row["password_env_var"] is None else str(row["password_env_var"])
            ),
            tls_ca_path=None if row["tls_ca_path"] is None else str(row["tls_ca_path"]),
            allow_insecure=bool(row["allow_insecure"]),
            timeout_seconds=int(row["timeout_seconds"]),
            mapping_json=str(row["mapping_json"]),
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def put_connector_identity(self, identity: ConnectorIdentity) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO connector_identity
                    (singleton, organization_id, organization_name, connector_id,
                     issuer_url, enabled)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    organization_id = excluded.organization_id,
                    organization_name = excluded.organization_name,
                    connector_id = excluded.connector_id,
                    issuer_url = excluded.issuer_url,
                    enabled = excluded.enabled
                """,
                (
                    identity.organization_id,
                    identity.organization_name,
                    identity.connector_id,
                    identity.issuer_url,
                    int(identity.enabled),
                ),
            )

    def get_connector_identity(self) -> ConnectorIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT organization_id, organization_name, connector_id, issuer_url, enabled
                FROM connector_identity WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        return ConnectorIdentity(
            organization_id=row["organization_id"],
            organization_name=row["organization_name"],
            connector_id=row["connector_id"],
            issuer_url=row["issuer_url"],
            enabled=bool(row["enabled"]),
        )

    def create_partner(self, name: str) -> str:
        partner_id = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO partners (partner_id, name, state) VALUES (?, ?, 'active')",
                (partner_id, name),
            )
        return partner_id

    def administration_snapshot(self) -> dict[str, list[dict[str, str]]]:
        queries = {
            "partners": "SELECT partner_id, name, state FROM partners ORDER BY name LIMIT 100",
            "clients": """SELECT client_id, partner_id, scopes_json, state
                FROM oauth_clients LIMIT 100""",
            "grants": """SELECT grant_id, relationship_id, scope, resource_ids_json, state
                FROM grants LIMIT 100""",
            "shares": """SELECT partner_id, circuit_source_id, visibility, status
                FROM circuit_shares LIMIT 100""",
            "profiles": """SELECT name, driver, enabled
                FROM source_database_profiles ORDER BY name LIMIT 100""",
            "audit": """SELECT occurred_at, operation, outcome, correlation_id
                FROM audit_events ORDER BY occurred_at DESC LIMIT 50""",
        }
        with self._connection() as db:
            return {
                key: [
                    {field: str(value) for field, value in dict(row).items()}
                    for row in db.execute(query).fetchall()
                ]
                for key, query in queries.items()
            }

    def provision_partner(
        self, *, name: str, secret_hash: str, grants: dict[str, set[str]]
    ) -> tuple[str, str, str]:
        if not name.strip() or not grants or not set(grants) <= ALLOWED_SCOPES:
            raise ValueError("partner name and at least one grant are required")
        partner_id, relationship_id = str(uuid.uuid4()), str(uuid.uuid4())
        client_id = f"ocp_{secrets_token(18)}"
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as db:
            db.execute("INSERT INTO partners VALUES (?, ?, 'active')", (partner_id, name.strip()))
            db.execute(
                """INSERT INTO relationships
                (relationship_id, partner_id, state, owner_approved_at, partner_approved_at)
                VALUES (?, ?, 'active', ?, ?)""",
                (relationship_id, partner_id, now, now),
            )
            for scope, resources in grants.items():
                db.execute(
                    """INSERT INTO grants
                    (grant_id, relationship_id, scope, resource_ids_json, state)
                    VALUES (?, ?, ?, ?, 'active')""",
                    (str(uuid.uuid4()), relationship_id, scope, json.dumps(sorted(resources))),
                )
            db.execute(
                """INSERT INTO oauth_clients
                (client_id, partner_id, secret_hash, scopes_json, state, created_at)
                VALUES (?, ?, ?, ?, 'active', ?)""",
                (client_id, partner_id, secret_hash, json.dumps(sorted(grants)), now),
            )
        return partner_id, relationship_id, client_id

    def revoke_partner(self, partner_id: str) -> None:
        with self._lock, self._connection() as db:
            cursor = db.execute(
                "UPDATE partners SET state = 'revoked' WHERE partner_id = ? AND state = 'active'",
                (partner_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("active partner not found")
            db.execute(
                "UPDATE relationships SET state = 'revoked' WHERE partner_id = ?", (partner_id,)
            )
            db.execute(
                "UPDATE oauth_clients SET state = 'revoked', revoked_at = ? WHERE partner_id = ?",
                (datetime.now(UTC).isoformat(), partner_id),
            )
            db.execute(
                "UPDATE circuit_shares SET status = 'revoked', revoked_at = ? WHERE partner_id = ?",
                (datetime.now(UTC).isoformat(), partner_id),
            )

    def create_client(self, partner_id: str, secret_hash: str, scopes: set[str]) -> str:
        client_id = f"ocp_{secrets_token(18)}"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients
                    (client_id, partner_id, secret_hash, scopes_json, state, created_at)
                VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    client_id,
                    partner_id,
                    secret_hash,
                    json.dumps(sorted(scopes)),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return client_id

    def create_active_relationship(self, partner_id: str) -> str:
        relationship_id = str(uuid.uuid4())
        approved_at = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO relationships
                    (relationship_id, partner_id, state, owner_approved_at, partner_approved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    relationship_id,
                    partner_id,
                    RelationshipState.ACTIVE.value,
                    approved_at,
                    approved_at,
                ),
            )
        return relationship_id

    def create_grant(
        self,
        relationship_id: str,
        scope: str,
        resource_ids: set[str] | None = None,
    ) -> str:
        grant_id = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO grants
                    (grant_id, relationship_id, scope, resource_ids_json, state)
                VALUES (?, ?, ?, ?, 'active')
                """,
                (grant_id, relationship_id, scope, json.dumps(sorted(resource_ids or set()))),
            )
        return grant_id

    def share_circuit(
        self,
        *,
        partner_id: str,
        source_circuit_id: str,
        visibility: str,
        created_by_id: str | None = None,
    ) -> str:
        if visibility not in _CIRCUIT_VISIBILITIES:
            raise ValueError("unsupported circuit share visibility")
        if not source_circuit_id.strip():
            raise ValueError("source circuit ID is required")
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                """
                SELECT share_id FROM circuit_shares
                WHERE partner_id = ? AND circuit_source_id = ?
                """,
                (partner_id, source_circuit_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO circuit_shares
                        (share_id, circuit_source_id, partner_id, visibility, status,
                         shared_at, revoked_at, created_by_id)
                    VALUES (?, ?, ?, ?, 'active', ?, NULL, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        source_circuit_id,
                        partner_id,
                        visibility,
                        now,
                        created_by_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE circuit_shares
                    SET visibility = ?, status = 'active', shared_at = ?,
                        revoked_at = NULL, created_by_id = ?
                    WHERE share_id = ?
                    """,
                    (visibility, now, created_by_id, str(existing["share_id"])),
                )
        return self.ocp_resource_id("nettool", "circuit", source_circuit_id)

    def revoke_circuit_share(self, *, partner_id: str, source_circuit_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE circuit_shares
                SET status = 'revoked', revoked_at = ?
                WHERE partner_id = ? AND circuit_source_id = ? AND status = 'active'
                """,
                (datetime.now(UTC).isoformat(), partner_id, source_circuit_id),
            )
        return cursor.rowcount == 1

    def circuit_share_visibility(
        self,
        *,
        partner_id: str,
        source_circuit_id: str,
    ) -> str | None:
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.visibility
                FROM circuit_shares s
                JOIN partners p ON p.partner_id = s.partner_id
                JOIN relationships r ON r.partner_id = s.partner_id
                WHERE s.partner_id = ? AND s.circuit_source_id = ?
                    AND s.status = 'active' AND s.revoked_at IS NULL
                    AND p.state = 'active'
                    AND r.state = 'active'
                    AND (r.expires_at IS NULL OR r.expires_at > ?)
                ORDER BY s.shared_at DESC
                LIMIT 1
                """,
                (partner_id, source_circuit_id, now),
            ).fetchone()
        return None if row is None else str(row["visibility"])

    def get_active_client(self, client_id: str) -> ClientRecord:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT c.client_id, c.partner_id, c.secret_hash, c.scopes_json
                FROM oauth_clients c
                JOIN partners p ON p.partner_id = c.partner_id
                WHERE c.client_id = ? AND c.state = 'active' AND p.state = 'active'
                """,
                (client_id,),
            ).fetchone()
        if row is None:
            raise AuthenticationError()
        scopes = json.loads(row["scopes_json"])
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            raise RuntimeError("Stored OAuth client scopes are invalid")
        return ClientRecord(
            client_id=row["client_id"],
            partner_id=row["partner_id"],
            secret_hash=row["secret_hash"],
            scopes=frozenset(scopes),
        )

    def authorize(
        self,
        *,
        client_id: str,
        partner_id: str,
        token_scopes: frozenset[str],
        required_scope: str,
        resource_id: str | None = None,
    ) -> set[str] | None:
        if required_scope not in token_scopes:
            raise AuthorizationError()
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            client = connection.execute(
                """
                SELECT 1 FROM oauth_clients c
                JOIN partners p ON p.partner_id = c.partner_id
                WHERE c.client_id = ? AND c.partner_id = ?
                    AND c.state = 'active' AND p.state = 'active'
                """,
                (client_id, partner_id),
            ).fetchone()
            if client is None:
                raise AuthorizationError()
            rows = connection.execute(
                """
                SELECT g.resource_ids_json
                FROM grants g
                JOIN relationships r ON r.relationship_id = g.relationship_id
                WHERE r.partner_id = ? AND r.state = 'active'
                    AND (r.expires_at IS NULL OR r.expires_at > ?)
                    AND g.scope = ? AND g.state = 'active'
                    AND (g.expires_at IS NULL OR g.expires_at > ?)
                """,
                (partner_id, now, required_scope, now),
            ).fetchall()
        if not rows:
            raise AuthorizationError()
        unrestricted = False
        allowed_resources: set[str] = set()
        for row in rows:
            allowed = json.loads(row["resource_ids_json"])
            if allowed == []:
                unrestricted = True
            elif isinstance(allowed, list) and all(isinstance(value, str) for value in allowed):
                allowed_resources.update(allowed)
            else:
                raise RuntimeError("Stored resource grant filters are invalid")
        if resource_id is None:
            return None if unrestricted else allowed_resources
        if unrestricted or resource_id in allowed_resources:
            return None
        raise AuthorizationError()

    def ocp_resource_id(self, integration: str, resource_type: str, source_id: str) -> str:
        message = f"{integration}\x00{resource_type}\x00{source_id}".encode()
        digest = hmac.new(self._resource_id_key, message, hashlib.sha256).hexdigest()[:24]
        ocp_id = f"{resource_type}_{digest}"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO resource_ids
                    (ocp_id, integration, resource_type, source_id)
                VALUES (?, ?, ?, ?)
                """,
                (ocp_id, integration, resource_type, source_id),
            )
        return ocp_id

    def source_resource_id(self, ocp_id: str, integration: str, resource_type: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT source_id FROM resource_ids
                WHERE ocp_id = ? AND integration = ? AND resource_type = ?
                """,
                (ocp_id, integration, resource_type),
            ).fetchone()
        return None if row is None else str(row["source_id"])

    def record_audit(
        self,
        *,
        partner_id: str | None,
        client_id: str | None,
        operation: str,
        resource_summary: str | None,
        outcome: str,
        correlation_id: str,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, occurred_at, partner_id, client_id, operation,
                     resource_summary, outcome, correlation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    datetime.now(UTC).isoformat(),
                    partner_id,
                    client_id,
                    operation,
                    resource_summary,
                    outcome,
                    correlation_id,
                ),
            )


def secrets_token(byte_count: int) -> str:
    import secrets

    return secrets.token_urlsafe(byte_count)
