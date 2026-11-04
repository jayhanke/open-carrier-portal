from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import pymysql
from pymysql.cursors import DictCursor

from open_carrier_portal.diagnostics import log_event, log_failure
from open_carrier_portal.errors import (
    ConfigurationError,
    SourceDatabaseQueryError,
    SourceDatabaseUnavailableError,
)
from open_carrier_portal.source_contract import (
    CORE_RESOURCES,
    MONITORING_FIELDS,
    SHARING_FIELDS,
    SOURCE_RESOURCES,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOURCE_TYPES = tuple(SOURCE_RESOURCES)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SCHEMA_DISCOVERY_LIMIT = 50_000

if TYPE_CHECKING:
    from open_carrier_portal.repository import SourceDatabaseProfile


def configured_profile_database(
    profile: SourceDatabaseProfile, metadata_path: Path, mapping: SourceMapping | None = None
) -> SourceDatabase:
    if not profile.enabled:
        raise ConfigurationError("enabled source profile not found")
    return configured_source_database(
        driver=profile.driver,
        host=profile.host,
        port=profile.port,
        database_name=profile.database_name,
        username=profile.username,
        password_env_var=profile.password_env_var,
        tls_ca_path=profile.tls_ca_path,
        allow_insecure=profile.allow_insecure,
        timeout_seconds=profile.timeout_seconds,
        mapping_json=mapping.to_json() if mapping is not None else profile.mapping_json,
        environment=os.environ,
        metadata_path=metadata_path,
    )


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
class JoinMapping:
    table: str
    alias: str
    left: str
    right: str
    kind: str = "left"

    def __post_init__(self) -> None:
        for value in (self.table, self.alias, self.right):
            _identifier(value, "join identifier")
        parts = self.left.split(".")
        if len(parts) != 2:
            raise ConfigurationError("join source must be alias.column")
        for part in parts:
            _identifier(part, "join source")
        if self.kind not in {"left", "inner"}:
            raise ConfigurationError("join type must be left or inner")
        if self.alias == "base":
            raise ConfigurationError("base is reserved for the primary table")


@dataclass(frozen=True, slots=True)
class ResourceMapping:
    table: str
    columns: Mapping[str, str]
    joins: tuple[JoinMapping, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.table, "mapping table")
        if not self.columns:
            raise ConfigurationError(f"mapping for {self.table} must define columns")
        if len(self.joins) > 8:
            raise ConfigurationError("a resource supports at most eight joins")
        aliases = {"base"}
        for join in self.joins:
            if join.alias in aliases or join.left.split(".")[0] not in aliases:
                raise ConfigurationError("joins must use distinct aliases and earlier tables")
            aliases.add(join.alias)
        for logical_name, column in self.columns.items():
            _identifier(logical_name, "mapping field")
            parts = column.split(".")
            if len(parts) == 2 and parts[0] in aliases:
                _identifier(parts[1], f"mapping column for {logical_name}")
            elif len(parts) == 1:
                _identifier(column, f"mapping column for {logical_name}")
            else:
                raise ConfigurationError("mapped columns must use a known alias and column")
        if self.joins and self.columns.get("source_id", "").split(".")[0] in aliases - {"base"}:
            raise ConfigurationError("source_id must come from the primary table")


@dataclass(frozen=True, slots=True)
class SourceMapping:
    resources: Mapping[str, ResourceMapping]

    def __post_init__(self) -> None:
        resources: dict[str, ResourceMapping] = {}
        aliases = {"company_locations": "racks", "rack_units": "rack_positions"}
        for resource, mapping in self.resources.items():
            target = aliases.get(resource, resource)
            if target in resources:
                raise ConfigurationError(f"duplicate legacy/current mapping for {target}")
            columns = dict(mapping.columns)
            if target in {"devices", "rack_positions"} and "company_location_id" in columns:
                if "rack_id" in columns:
                    raise ConfigurationError(
                        f"duplicate legacy/current rack reference for {target}"
                    )
                columns["rack_id"] = columns.pop("company_location_id")
            resources[target] = ResourceMapping(mapping.table, columns, mapping.joins)
        unknown = set(resources) - set(_RESOURCE_TYPES)
        if unknown:
            raise ConfigurationError(f"unsupported mapped resources: {sorted(unknown)}")
        object.__setattr__(self, "resources", resources)

    @property
    def missing_fields(self) -> dict[str, list[str]]:
        return self.missing_fields_for()

    def missing_fields_for(
        self,
        *,
        monitoring: bool = False,
        requirements: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, list[str]]:
        if requirements is None:
            requirements = {
                resource: definition.required
                for resource, definition in SOURCE_RESOURCES.items()
                if resource in CORE_RESOURCES or resource in self.resources
            }
        missing = {}
        for resource, fields in requirements.items():
            required = set(fields)
            if monitoring:
                required.update(MONITORING_FIELDS.get(resource, ()))
            configured = self.resources.get(resource)
            absent = required - set(configured.columns if configured else ())
            if absent:
                missing[resource] = sorted(absent)
        return missing

    @classmethod
    def sharing_defaults(cls) -> SourceMapping:
        template = cls.nettool_defaults()
        return cls(
            {
                resource: ResourceMapping(
                    template.resources[resource].table,
                    {field: template.resources[resource].columns[field] for field in selected},
                )
                for resource, selected in SHARING_FIELDS.items()
            }
        )

    @classmethod
    def nettool_defaults(cls) -> SourceMapping:
        return cls(
            resources={
                "companies": ResourceMapping(
                    "company",
                    {"source_id": "Company_ID", "name": "CompanyName"},
                ),
                "locations": ResourceMapping(
                    "Location",
                    {
                        "source_id": "LocationID",
                        "address1": "Address1",
                        "address2": "Address2",
                        "city": "City",
                        "state": "State",
                        "postal_code": "Zip",
                        "latitude": "Lat",
                        "longitude": "Lng",
                        "floor": "Floor",
                        "suite": "Suite",
                    },
                ),
                "racks": ResourceMapping(
                    "CompanyLocation",
                    {
                        "source_id": "CompanyLocationID",
                        "company_id": "Company_ID",
                        "location_id": "LocationID",
                        "name": "RackName",
                        "site_code": "SiteCode",
                        "rack_name": "RackName",
                    },
                ),
                "rack_positions": ResourceMapping(
                    "RackUnit",
                    {
                        "source_id": "RackUnitID",
                        "rack_id": "CompanyLocationID",
                        "name": "RackUnitName",
                        "device_id": "DeviceID",
                    },
                ),
                "devices": ResourceMapping(
                    "Device",
                    {
                        "source_id": "DeviceID",
                        "name": "DeviceName",
                        "rack_id": "CompanyLocationID",
                        "company_id": "CompanyID",
                        "type": "DeviceType",
                        "observium_id": "ObserviumID",
                    },
                ),
                "ports": ResourceMapping(
                    "Port",
                    {
                        "source_id": "PortID",
                        "device_id": "DeviceID",
                        "name": "PortName",
                        "observium_id": "ObserviumID",
                        "owner_company_id": "OwnerCompany",
                        "connector_type": "ConnectorType",
                        "nni": "NNI",
                        "parent_port_id": "SubPortOf",
                        "vlan": "Vlan",
                        "ip_address": "IPAddress",
                        "card_type_id": "CardTypeID",
                    },
                ),
                "circuits": ResourceMapping(
                    "LitCircuit",
                    {
                        "source_id": "LitCircuitID",
                        "circuit_layout_id": "CircuitLayoutID",
                        "name": "LitCircuitName",
                        "billing_company_id": "BillingCompanyID",
                        "service_id": "ServiceID",
                        "service_order_id": "ServiceOrderID",
                        "description": "Description",
                        "a_company_location_id": "CompanyLocationA",
                        "z_company_location_id": "CompanyLocationZ",
                        "active": "Active",
                    },
                ),
                "services": ResourceMapping(
                    "Service",
                    {
                        "source_id": "ServiceID",
                        "name": "ServiceName",
                        "type": "ServiceType",
                        "bandwidth": "ServiceBW",
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
                "new_dark_fiber_circuits": ResourceMapping(
                    "NewDarkFiberCircuit",
                    {"source_id": "NewDarkFiberCircuitID", "name": "Description"},
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
                        **(
                            {
                                "joins": [
                                    {
                                        "table": j.table,
                                        "alias": j.alias,
                                        "left": j.left,
                                        "right": j.right,
                                        "kind": j.kind,
                                    }
                                    for j in mapping.joins
                                ]
                            }
                            if mapping.joins
                            else {}
                        ),
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
            raw_joins = raw_mapping.get("joins", [])
            if not isinstance(raw_joins, list) or len(raw_joins) > 8:
                raise ConfigurationError("joins must be a list of at most eight relationships")
            joins = []
            for join in raw_joins:
                if (
                    not isinstance(join, dict)
                    or not all(
                        isinstance(join.get(key), str)
                        for key in ("table", "alias", "left", "right")
                    )
                    or not isinstance(join.get("kind", "left"), str)
                ):
                    raise ConfigurationError("invalid join definition")
                joins.append(
                    JoinMapping(
                        join["table"],
                        join["alias"],
                        join["left"],
                        join["right"],
                        join.get("kind", "left"),
                    )
                )
            resources[resource_type] = ResourceMapping(table, columns, tuple(joins))
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
            log_failure("database", "connection.failed", exc, driver=self.driver)
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

    @property
    def missing_fields(self) -> dict[str, list[str]]:
        return self._mapping.missing_fields

    def missing_fields_for(
        self,
        *,
        monitoring: bool = False,
        requirements: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, list[str]]:
        return self._mapping.missing_fields_for(monitoring=monitoring, requirements=requirements)

    def has_resource(self, resource: str) -> bool:
        return resource in self._mapping.resources

    def health_check(self) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT 1")
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "health.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError("The source database health query failed") from exc
        log_event(
            "database", "health.passed", "Database health query succeeded.", driver=self.driver
        )

    def validate_mapping(self) -> None:
        for resource_type in self._mapping.resources:
            self._select(resource_type, limit=0)
        log_event("database", "mapping.validated", "Mapped database columns validated.")

    def describe_schema(self) -> list[dict[str, str]]:
        """Discover identifiers only; never return source records or arbitrary SQL results."""
        try:
            with self._cursor() as cursor:
                if self.driver == "mysql":
                    cursor.execute(
                        """SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
                        FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
                        ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT %s""",
                        (_SCHEMA_DISCOVERY_LIMIT + 1,),
                    )
                else:
                    cursor.execute(
                        """SELECT m.name AS table_name, p.name AS column_name
                        FROM sqlite_master m JOIN pragma_table_info(m.name) p
                        WHERE m.type = 'table' ORDER BY m.name, p.cid LIMIT ?""",
                        (_SCHEMA_DISCOVERY_LIMIT + 1,),
                    )
                rows = cursor.fetchall()
                if len(rows) > _SCHEMA_DISCOVERY_LIMIT:
                    raise ConfigurationError(
                        f"schema discovery supports at most {_SCHEMA_DISCOVERY_LIMIT} columns; "
                        "use a dedicated read-only schema exposing only the tables "
                        "needed for sharing"
                    )
                return [
                    {"table": str(row["table_name"]), "column": str(row["column_name"])}
                    for row in rows
                ]
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "schema.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError("Source schema discovery failed") from exc

    def fetch(self, resource_type: str, limit: int = 100) -> list[dict[str, Any]]:
        if resource_type not in self._mapping.resources:
            raise ConfigurationError(f"no mapping configured for {resource_type}")
        if limit < 1 or limit > 1000:
            raise ConfigurationError("source query limit must be between 1 and 1000")
        return self._select(resource_type, limit)

    def describe_relationships(self) -> list[dict[str, str]]:
        try:
            with self._cursor() as cursor:
                if self.driver == "mysql":
                    cursor.execute(
                        """SELECT TABLE_NAME AS source_table, COLUMN_NAME AS source_column,
                        REFERENCED_TABLE_NAME AS target_table,
                        REFERENCED_COLUMN_NAME AS target_column,
                        CONSTRAINT_NAME AS relation_id
                        FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_SCHEMA = DATABASE()
                        AND REFERENCED_TABLE_NAME IS NOT NULL LIMIT %s""",
                        (_SCHEMA_DISCOVERY_LIMIT + 1,),
                    )
                else:
                    cursor.execute(
                        """SELECT m.name AS source_table, f."from" AS source_column,
                        f."table" AS target_table,
                        COALESCE(f."to", (
                            SELECT name FROM pragma_table_info(f."table") WHERE pk = 1
                            AND (SELECT COUNT(*) FROM pragma_table_info(f."table")
                                 WHERE pk > 0) = 1
                        )) AS target_column,
                        CAST(f.id AS TEXT) AS relation_id
                        FROM sqlite_master m JOIN pragma_foreign_key_list(m.name) f
                        WHERE m.type = 'table' LIMIT ?""",
                        (_SCHEMA_DISCOVERY_LIMIT + 1,),
                    )
                rows = cursor.fetchall()
                if len(rows) > _SCHEMA_DISCOVERY_LIMIT:
                    raise ConfigurationError(
                        f"relationship discovery supports at most {_SCHEMA_DISCOVERY_LIMIT} "
                        "foreign-key columns; use a dedicated read-only schema exposing only "
                        "the tables needed for sharing"
                    )
                counts: dict[tuple[str, str], int] = {}
                for row in rows:
                    key = (str(row["source_table"]), str(row["relation_id"]))
                    counts[key] = counts.get(key, 0) + 1
                return [
                    {
                        key: str(row[key])
                        for key in (
                            "source_table",
                            "source_column",
                            "target_table",
                            "target_column",
                        )
                    }
                    for row in rows
                    if counts[(str(row["source_table"]), str(row["relation_id"]))] == 1
                    and row["target_column"] is not None
                ]
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "relationships.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError("Source relationship discovery failed") from exc

    def _validate_join_targets(self, mapping: ResourceMapping) -> None:
        for join in mapping.joins:
            with self._cursor() as cursor:
                if self.driver == "mysql":
                    cursor.execute(
                        """SELECT INDEX_NAME FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND NON_UNIQUE = 0
                        GROUP BY INDEX_NAME HAVING COUNT(*) = 1 AND MAX(COLUMN_NAME) = %s
                        AND MAX(SUB_PART) IS NULL""",
                        (join.table, join.right),
                    )
                else:
                    cursor.execute(
                        """SELECT name FROM pragma_table_info(?) WHERE name = ? AND pk = 1
                        AND (SELECT COUNT(*) FROM pragma_table_info(?) WHERE pk > 0) = 1
                        UNION ALL
                        SELECT i.name FROM pragma_index_list(?) i
                        JOIN pragma_index_info(i.name) c
                        WHERE i."unique" = 1 AND i.partial = 0
                        GROUP BY i.name HAVING COUNT(*) = 1 AND MAX(c.name) = ?""",
                        (join.table, join.right, join.table, join.table, join.right),
                    )
                if not cursor.fetchall():
                    raise ConfigurationError(
                        f"join target {join.table}.{join.right} must have a single-column "
                        "primary or unique key; one-to-many joins would duplicate resource rows"
                    )

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

    def fetch_by_ids(self, resource_type: str, identifiers: list[str]) -> list[dict[str, Any]]:
        if not identifiers or len(identifiers) > 100:
            raise ConfigurationError("reference reads require between 1 and 100 identifiers")
        if resource_type not in self._mapping.resources:
            raise ConfigurationError(f"no mapping configured for {resource_type}")
        mapping = self._mapping.resources[resource_type]
        if "source_id" not in mapping.columns:
            raise ConfigurationError("reference reads require a mapped source ID")
        self._validate_join_targets(mapping)
        base = self._compile_select(resource_type, 1, paginate=False)[0]
        placeholder = "?" if self._settings.driver == "sqlite" else "%s"
        key = self._quote("source_id", "mapping field")
        sql = (
            f"SELECT * FROM ({base}) AS related WHERE {key} IN "
            f"({', '.join(placeholder for _ in identifiers)}) LIMIT {placeholder}"
        )
        try:
            with self._cursor() as cursor:
                cursor.execute(sql, (*identifiers, len(identifiers) + 1))
                rows = [dict(row) for row in cursor.fetchall()]
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "query.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError("Mapped reference records could not be read") from exc
        if len({str(row["source_id"]) for row in rows}) != len(rows):
            raise SourceDatabaseQueryError("Mapped reference identifiers must be unique")
        return rows

    def fetch_page(
        self, resource_type: str, filters: Mapping[str, object], *, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if not filters or resource_type not in self._mapping.resources:
            raise ConfigurationError("a mapped resource and scoped filters are required")
        mapping = self._mapping.resources[resource_type]
        if set(filters) - set(mapping.columns) or "source_id" not in mapping.columns:
            raise ConfigurationError("pagination requires mapped filters and source ID")
        if not 0 <= offset <= 999_900 or not 1 <= limit <= 100:
            raise ConfigurationError("source page bounds are invalid")
        return self._select(
            resource_type, limit, filters=filters, order_by="source_id", offset=offset
        )

    def fetch_inventory_page(
        self, resource_type: str, *, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        if resource_type not in self._mapping.resources or "source_id" not in (
            self._mapping.resources[resource_type].columns
        ):
            raise ConfigurationError(
                "inventory pagination requires a mapped resource and source ID"
            )
        if not 0 <= offset <= 999_900 or not 1 <= limit <= 100:
            raise ConfigurationError("source page bounds are invalid")
        return self._select(resource_type, limit, order_by="source_id", offset=offset)

    def fetch_explorer_page(
        self,
        resource_type: str,
        *,
        filters: Mapping[str, object] | None = None,
        query: str = "",
        related_port_id: object | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if resource_type not in {"locations", "racks", "devices", "ports", "companies", "circuits"}:
            raise ConfigurationError("unsupported explorer resource")
        if not 0 <= offset <= 999_900 or not 1 <= limit <= 100 or len(query) > 120:
            raise ConfigurationError("explorer query bounds are invalid")
        if related_port_id is not None and resource_type != "circuits":
            raise ConfigurationError("port membership applies only to circuits")

        def mapped(resource: str) -> str:
            if resource not in self._mapping.resources:
                raise ConfigurationError(f"no mapping configured for {resource}")
            self._validate_join_targets(self._mapping.resources[resource])
            return self._compile_select(resource, 1, paginate=False)[0]

        base = mapped(resource_type)
        columns = self._mapping.resources[resource_type].columns
        if "source_id" not in columns or set(filters or {}) - set(columns):
            raise ConfigurationError("explorer requires mapped filters and source ID")

        def quote(name: str) -> str:
            return self._quote(name, "mapping field")

        placeholder = "?" if self._settings.driver == "sqlite" else "%s"
        conditions = [f"{quote(field)} = {placeholder}" for field in (filters or {})]
        parameters: list[object] = list((filters or {}).values())
        if query.strip():
            fields = (
                ("address1", "city", "state", "postal_code")
                if resource_type == "locations"
                else ("name",)
            )
            fields = tuple(field for field in fields if field in columns)
            if not fields:
                raise ConfigurationError("map display fields before searching this resource")
            cast_type = "TEXT" if self._settings.driver == "sqlite" else "CHAR"
            conditions.append(
                "("
                + " OR ".join(
                    f"CAST({quote(field)} AS {cast_type}) LIKE {placeholder} ESCAPE '!'"
                    for field in fields
                )
                + ")"
            )
            escaped = query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            parameters.extend(["%" + escaped + "%"] * len(fields))
        if related_port_id is not None:
            if "circuit_layout_id" not in columns:
                raise ConfigurationError("map the circuit layout reference for related circuits")
            endpoints = mapped("circuit_layouts")
            required = {"source_id", "port_a_id", "port_z_id"}
            if not required <= set(self._mapping.resources["circuit_layouts"].columns):
                raise ConfigurationError("map both circuit endpoint ports")
            membership = (
                f"SELECT {quote('source_id')} FROM ({endpoints}) AS endpoints "
                f"WHERE {quote('port_a_id')} = {placeholder} "
                f"OR {quote('port_z_id')} = {placeholder}"
            )
            parameters.extend([related_port_id, related_port_id])
            if "circuit_layout_items" in self._mapping.resources:
                if not {"circuit_layout_id", "item_type", "item_source_id"} <= set(
                    self._mapping.resources["circuit_layout_items"].columns
                ):
                    raise ConfigurationError("complete the mapped circuit path membership fields")
                route = mapped("circuit_layout_items")
                membership += (
                    f" UNION SELECT {quote('circuit_layout_id')} FROM ({route}) AS route "
                    f"WHERE LOWER({quote('item_type')}) = 'port' "
                    f"AND {quote('item_source_id')} = {placeholder}"
                )
                parameters.append(related_port_id)
            conditions.append(f"{quote('circuit_layout_id')} IN ({membership})")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            f"SELECT * FROM ({base}) AS inventory{where} ORDER BY {quote('source_id')} "
            f"LIMIT {placeholder} OFFSET {placeholder}"
        )
        try:
            with self._cursor() as cursor:
                cursor.execute(sql, (*parameters, limit + 1, offset))
                rows = cursor.fetchall()
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "query.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError(
                "The mapped explorer query could not be completed"
            ) from exc
        return [dict(row) for row in rows]

    def _select(
        self,
        resource_type: str,
        limit: int,
        *,
        filters: Mapping[str, object] | None = None,
        order_by: str | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            self._validate_join_targets(self._mapping.resources[resource_type])
            query, parameters = self._compile_select(
                resource_type, limit, filters=filters, order_by=order_by, offset=offset
            )
            with self._cursor() as cursor:
                cursor.execute(query, parameters)
                rows = cursor.fetchall()
        except SourceDatabaseUnavailableError:
            raise
        except (sqlite3.Error, pymysql.MySQLError) as exc:
            log_failure("database", "query.failed", exc, driver=self.driver)
            raise SourceDatabaseQueryError(
                f"The configured mapping for {resource_type} could not be queried"
            ) from exc
        if offset is None and filters and len(rows) > limit:
            log_event(
                "database",
                "query.row_limit_exceeded",
                "Mapped database query exceeded its row limit.",
                level=logging.ERROR,
            )
            raise SourceDatabaseQueryError(
                f"The configured query for {resource_type} exceeded its row limit"
            )
        return [dict(row) for row in rows]

    def preview_sql(self, resource_type: str) -> str:
        return self._compile_select(resource_type, 5)[0]

    def _compile_select(
        self,
        resource_type: str,
        limit: int,
        *,
        filters: Mapping[str, object] | None = None,
        order_by: str | None = None,
        offset: int | None = None,
        paginate: bool = True,
    ) -> tuple[str, tuple[object, ...]]:
        mapping = self._mapping.resources[resource_type]

        def column_sql(column: str) -> str:
            parts = column.split(".")
            if mapping.joins or len(parts) == 2:
                alias, name = parts if len(parts) == 2 else ("base", column)
                return self._quote(alias, "table alias") + "." + self._quote(name, "column")
            return self._quote(column, "mapping column")

        selected_columns = ", ".join(
            f"{column_sql(column)} AS {self._quote(logical, 'mapping field')}"
            for logical, column in mapping.columns.items()
        )
        table = self._quote(mapping.table, "mapping table")
        if mapping.joins or any("." in column for column in mapping.columns.values()):
            table += " AS " + self._quote("base", "table alias")
        for join in mapping.joins:
            table += (
                f" {join.kind.upper()} JOIN {self._quote(join.table, 'join table')} AS "
                f"{self._quote(join.alias, 'join alias')} ON {column_sql(join.left)} = "
                f"{column_sql(join.alias + '.' + join.right)}"
            )
        placeholder = "?" if self._settings.driver == "sqlite" else "%s"
        where_clause = ""
        parameters: tuple[object, ...] = ()
        if filters:
            where_clause = " WHERE " + " AND ".join(
                f"{column_sql(mapping.columns[field])} = {placeholder}" for field in filters
            )
            parameters = tuple(filters.values())
        order_clause = (
            "" if order_by is None else f" ORDER BY {column_sql(mapping.columns[order_by])}"
        )
        offset_clause = "" if offset is None else f" OFFSET {placeholder}"
        if not paginate:
            return f"SELECT {selected_columns} FROM {table}{where_clause}{order_clause}", parameters
        return (
            f"SELECT {selected_columns} FROM {table}{where_clause}{order_clause} "
            f"LIMIT {placeholder}{offset_clause}",
            (
                *parameters,
                limit + 1 if filters or offset is not None else limit,
                *((offset,) if offset is not None else ()),
            ),
        )

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
    metadata_path: Path | None = None,
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
    if not password and metadata_path is not None:
        from open_carrier_portal.source_credentials import read_password

        password = read_password(metadata_path, password_env_var)
    if not password:
        log_event(
            "database",
            "configuration.password_missing",
            "No database password was resolved. Configure the named environment variable "
            "or save the database password through Connections.",
            level=logging.ERROR,
        )
        raise ConfigurationError(
            f"source database password is required: configure {password_env_var}"
            + (" or save a password through Connections" if metadata_path is not None else "")
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
