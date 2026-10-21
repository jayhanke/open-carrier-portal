from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import pymysql
from pymysql.cursors import DictCursor

from open_carrier_portal.errors import (
    ConfigurationError,
    SourceDatabaseQueryError,
    SourceDatabaseUnavailableError,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOURCE_TYPES = (
    "locations",
    "devices",
    "ports",
    "circuits",
    "circuit_layouts",
    "circuit_layout_items",
    "patches",
    "pseudowires",
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _identifier(value: str, field_name: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{field_name} must be a simple SQL identifier")
    return value


@dataclass(frozen=True, slots=True)
class SQLiteSourceDatabaseSettings:
    database: Path
    timeout_seconds: int = 10
    driver: Literal["sqlite"] = "sqlite"

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ConfigurationError("source database timeout must be at least 1 second")


@dataclass(frozen=True, slots=True)
class MySQLSourceDatabaseSettings:
    host: str
    port: int
    database: str
    username: str
    password: str
    timeout_seconds: int = 10
    tls_ca: Path | None = None
    allow_insecure: bool = False
    driver: Literal["mysql"] = "mysql"

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ConfigurationError("source database host is required")
        if self.port < 1 or self.port > 65_535:
            raise ConfigurationError("source database port must be between 1 and 65535")
        if not self.database.strip():
            raise ConfigurationError("source database name is required")
        if not self.username.strip():
            raise ConfigurationError("source database username is required")
        if not self.password:
            raise ConfigurationError("source database password is required")
        if self.timeout_seconds < 1:
            raise ConfigurationError("source database timeout must be at least 1 second")
        is_remote = self.host.lower() not in _LOOPBACK_HOSTS
        if is_remote and self.tls_ca is None and not self.allow_insecure:
            raise ConfigurationError(
                "a TLS CA file or explicit insecure-connection opt-in is required "
                "for a non-loopback source database host"
            )
        if self.tls_ca is not None and self.allow_insecure:
            raise ConfigurationError(
                "TLS CA and insecure connection settings are mutually exclusive"
            )
        if self.tls_ca is not None and not self.tls_ca.is_file():
            raise ConfigurationError("source database TLS CA file does not exist")


SourceDatabaseSettings = SQLiteSourceDatabaseSettings | MySQLSourceDatabaseSettings


class _Cursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[Mapping[str, Any]]: ...

    def close(self) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ResourceMapping:
    table: str
    columns: Mapping[str, str]

    def __post_init__(self) -> None:
        _identifier(self.table, "mapping table")
        if not self.columns:
            raise ConfigurationError(f"mapping for {self.table} must define columns")
        for logical_name, column in self.columns.items():
            _identifier(logical_name, "mapping field")
            _identifier(column, f"mapping column for {logical_name}")


@dataclass(frozen=True, slots=True)
class SourceMapping:
    resources: Mapping[str, ResourceMapping]

    def __post_init__(self) -> None:
        unknown = set(self.resources) - set(_RESOURCE_TYPES)
        if unknown:
            raise ConfigurationError(f"unsupported mapped resources: {sorted(unknown)}")

    @classmethod
    def nettool_defaults(cls) -> SourceMapping:
        return cls(
            resources={
                "locations": ResourceMapping(
                    "Location",
                    {"source_id": "LocationID", "name": "Name", "address": "Address"},
                ),
                "devices": ResourceMapping(
                    "Device",
                    {"source_id": "DeviceID", "name": "Name", "observium_id": "ObserviumID"},
                ),
                "ports": ResourceMapping(
                    "Port",
                    {
                        "source_id": "PortID",
                        "device_id": "DeviceID",
                        "name": "Name",
                        "observium_id": "ObserviumID",
                    },
                ),
                "circuits": ResourceMapping(
                    "LitCircuit",
                    {
                        "source_id": "LitCircuitID",
                        "circuit_layout_id": "CircuitLayoutID",
                        "customer_id": "BillingCompanyID",
                        "description": "Description",
                        "a_location_id": "ALocationID",
                        "z_location_id": "ZLocationID",
                    },
                ),
                "circuit_layouts": ResourceMapping(
                    "CircuitLayout",
                    {
                        "source_id": "CircuitLayoutID",
                        "name": "CircuitName",
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
                "patches": ResourceMapping(
                    "Patch",
                    {
                        "source_id": "PatchID",
                        "patch_type": "PatchType",
                        "port_a_id": "PortID_A",
                        "port_b_id": "PortID_B",
                        "port_a_secondary_id": "PortID_A_sec",
                        "port_b_secondary_id": "PortID_B_sec",
                        "dark_fiber_circuit_id": "DarkFiberCircuitID",
                        "order_date": "OrderDate",
                        "install_date": "InstallDate",
                        "cancel_date": "CancelDate",
                        "removal_date": "RemovalDate",
                        "needs_removal": "needsRemoval",
                        "length": "Length",
                    },
                ),
                "pseudowires": ResourceMapping(
                    "PseudoWire",
                    {
                        "source_id": "PseudoWireID",
                        "pseudowire_id": "PWID",
                        "port_a_id": "PortAID",
                        "port_b_id": "PortBID",
                    },
                ),
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "resources": {
                    resource_type: {
                        "table": mapping.table,
                        "columns": dict(mapping.columns),
                    }
                    for resource_type, mapping in self.resources.items()
                }
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> SourceMapping:
        try:
            decoded: Any = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("OCP_SOURCE_DB_MAPPING_JSON must be valid JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("resources"), dict):
            raise ConfigurationError("source mapping JSON must contain a resources object")
        resources: dict[str, ResourceMapping] = {}
        for resource_type, raw_mapping in decoded["resources"].items():
            if not isinstance(resource_type, str) or not isinstance(raw_mapping, dict):
                raise ConfigurationError("each source resource mapping must be an object")
            table = raw_mapping.get("table")
            columns = raw_mapping.get("columns")
            if not isinstance(table, str) or not isinstance(columns, dict):
                raise ConfigurationError(
                    f"mapping for {resource_type} must contain table and columns"
                )
            if not all(
                isinstance(key, str) and isinstance(column, str) for key, column in columns.items()
            ):
                raise ConfigurationError(f"mapping columns for {resource_type} must be strings")
            resources[resource_type] = ResourceMapping(table, columns)
        return cls(resources)


class SourceDatabase:
    def __init__(self, settings: SourceDatabaseSettings, mapping: SourceMapping) -> None:
        self._settings = settings
        self._mapping = mapping

    @contextmanager
    def _connection(self) -> Iterator[_Connection]:
        try:
            if isinstance(self._settings, SQLiteSourceDatabaseSettings):
                connection = sqlite3.connect(
                    f"file:{self._settings.database.resolve()}?mode=ro",
                    uri=True,
                    timeout=self._settings.timeout_seconds,
                )
                connection.row_factory = sqlite3.Row
            else:
                ssl = (
                    {
                        "ca": str(self._settings.tls_ca),
                        "check_hostname": True,
                    }
                    if self._settings.tls_ca is not None
                    else None
                )
                connection = pymysql.connect(
                    host=self._settings.host,
                    port=self._settings.port,
                    user=self._settings.username,
                    password=self._settings.password,
                    database=self._settings.database,
                    connect_timeout=self._settings.timeout_seconds,
                    read_timeout=self._settings.timeout_seconds,
                    write_timeout=self._settings.timeout_seconds,
                    charset="utf8mb4",
                    cursorclass=DictCursor,
                    autocommit=True,
                    ssl=ssl,
                    ssl_disabled=self._settings.tls_ca is None,
                )
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            raise SourceDatabaseUnavailableError(
                "The configured source database could not be reached"
            ) from exc
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _cursor(self) -> Iterator[_Cursor]:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    @property
    def driver(self) -> str:
        return self._settings.driver

    def health_check(self) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT 1")
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            raise SourceDatabaseQueryError("The source database health query failed") from exc

    def validate_mapping(self) -> None:
        for resource_type in self._mapping.resources:
            self._select(resource_type, limit=0)

    def describe_schema(self) -> list[dict[str, str]]:
        """Discover identifiers only; never return source records or arbitrary SQL results."""
        try:
            with self._cursor() as cursor:
                if self.driver == "mysql":
                    cursor.execute(
                        """SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
                        FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
                        ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT 1000"""
                    )
                    return [
                        {"table": str(row["table_name"]), "column": str(row["column_name"])}
                        for row in cursor.fetchall()
                    ]
                cursor.execute(
                    """SELECT m.name AS table_name, p.name AS column_name
                    FROM sqlite_master m JOIN pragma_table_info(m.name) p
                    WHERE m.type = 'table' ORDER BY m.name, p.cid LIMIT 1000"""
                )
                return [
                    {"table": str(row["table_name"]), "column": str(row["column_name"])}
                    for row in cursor.fetchall()
                ]
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            raise SourceDatabaseQueryError("Source schema discovery failed") from exc

    def fetch(self, resource_type: str, limit: int = 100) -> list[dict[str, Any]]:
        if resource_type not in self._mapping.resources:
            raise ConfigurationError(f"no mapping configured for {resource_type}")
        if limit < 1 or limit > 1000:
            raise ConfigurationError("source query limit must be between 1 and 1000")
        return self._select(resource_type, limit)

    def fetch_matching(
        self,
        resource_type: str,
        filters: Mapping[str, object],
        *,
        order_by: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if resource_type not in self._mapping.resources:
            raise ConfigurationError(f"no mapping configured for {resource_type}")
        if not filters:
            raise ConfigurationError("source query filters are required")
        if limit < 1 or limit > 1000:
            raise ConfigurationError("source query limit must be between 1 and 1000")
        mapping = self._mapping.resources[resource_type]
        unknown_filters = set(filters) - set(mapping.columns)
        if unknown_filters:
            raise ConfigurationError(f"unmapped source query filters: {sorted(unknown_filters)}")
        if order_by is not None and order_by not in mapping.columns:
            raise ConfigurationError(f"unmapped source query order field: {order_by}")
        return self._select(
            resource_type,
            limit,
            filters=filters,
            order_by=order_by,
        )

    def _select(
        self,
        resource_type: str,
        limit: int,
        *,
        filters: Mapping[str, object] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        mapping = self._mapping.resources[resource_type]
        selected_columns = ", ".join(
            f"{self._quote(column, 'mapping column')} AS {self._quote(logical, 'mapping field')}"
            for logical, column in mapping.columns.items()
        )
        table = self._quote(mapping.table, "mapping table")
        placeholder = "?" if self._settings.driver == "sqlite" else "%s"
        where_clause = ""
        parameters: tuple[object, ...] = ()
        if filters:
            where_clause = " WHERE " + " AND ".join(
                f"{self._quote(mapping.columns[field], 'mapping column')} = {placeholder}"
                for field in filters
            )
            parameters = tuple(filters.values())
        order_clause = (
            ""
            if order_by is None
            else f" ORDER BY {self._quote(mapping.columns[order_by], 'mapping column')}"
        )
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    f"SELECT {selected_columns} FROM {table}{where_clause}"
                    f"{order_clause} LIMIT {placeholder}",
                    (*parameters, limit + 1 if filters else limit),
                )
                rows = cursor.fetchall()
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            raise SourceDatabaseQueryError(
                f"The configured mapping for {resource_type} could not be queried"
            ) from exc
        if filters and len(rows) > limit:
            raise SourceDatabaseQueryError(
                f"The configured query for {resource_type} exceeded its row limit"
            )
        return [dict(row) for row in rows]

    def _quote(self, value: str, field_name: str) -> str:
        identifier = _identifier(value, field_name)
        if self._settings.driver == "mysql":
            return f"`{identifier}`"
        return f'"{identifier}"'


def configured_source_database(
    *,
    driver: str,
    database_name: str,
    mapping_json: str,
    environment: Mapping[str, str],
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password_env_var: str | None = None,
    tls_ca_path: str | None = None,
    allow_insecure: bool = False,
    timeout_seconds: int = 10,
) -> SourceDatabase:
    mapping = SourceMapping.from_json(mapping_json)
    if driver == "sqlite":
        return SourceDatabase(
            SQLiteSourceDatabaseSettings(
                database=Path(database_name),
                timeout_seconds=timeout_seconds,
            ),
            mapping,
        )
    if driver != "mysql":
        raise ConfigurationError(f"unsupported source database driver: {driver}")
    if host is None or port is None or username is None or password_env_var is None:
        raise ConfigurationError(
            "mysql source profile requires host, port, username, and password environment"
        )
    if not _ENVIRONMENT_VARIABLE.fullmatch(password_env_var):
        raise ConfigurationError("source database password environment name is invalid")
    password = environment.get(password_env_var)
    if not password:
        raise ConfigurationError(
            f"source database password environment variable {password_env_var} is required"
        )
    return SourceDatabase(
        MySQLSourceDatabaseSettings(
            host=host,
            port=port,
            database=database_name,
            username=username,
            password=password,
            timeout_seconds=timeout_seconds,
            tls_ca=None if tls_ca_path is None else Path(tls_ca_path),
            allow_insecure=allow_insecure,
        ),
        mapping,
    )
