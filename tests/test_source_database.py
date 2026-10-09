from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from open_carrier_portal.errors import ConfigurationError
from open_carrier_portal.source_database import (
    MySQLSourceDatabaseSettings,
    ResourceMapping,
    SourceDatabase,
    SourceMapping,
    SQLiteSourceDatabaseSettings,
    configured_source_database,
)


def test_source_database_reads_only_explicit_mapped_columns(tmp_path: Path) -> None:
    database = tmp_path / "nettool.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE circuits (id INTEGER, label TEXT, private_value TEXT)")
        connection.execute("INSERT INTO circuits VALUES (7, 'Circuit A', 'do-not-export')")

    source = SourceDatabase(
        SQLiteSourceDatabaseSettings(database),
        SourceMapping(
            {"circuits": ResourceMapping("circuits", {"source_id": "id", "name": "label"})}
        ),
    )

    assert source.fetch("circuits") == [{"source_id": 7, "name": "Circuit A"}]


def test_source_database_rejects_invalid_mapping_identifier(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        SourceDatabase(
            SQLiteSourceDatabaseSettings(tmp_path / "missing.db"),
            SourceMapping(
                {"circuits": ResourceMapping("circuits; DROP TABLE users", {"id": "id"})}
            ),
        )


def test_nettool_mapping_can_be_loaded_from_json() -> None:
    mapping = SourceMapping.from_json(
        json.dumps(
            {
                "resources": {
                    "circuits": {
                        "table": "LitCircuit",
                        "columns": {"source_id": "LitCircuitID"},
                    }
                }
            }
        )
    )

    assert mapping.resources["circuits"].table == "LitCircuit"


def test_nettool_defaults_map_complete_circuit_path() -> None:
    resources = SourceMapping.nettool_defaults().resources

    assert resources["circuits"].columns["circuit_layout_id"] == "CircuitLayoutID"
    assert resources["circuit_layouts"] == ResourceMapping(
        "CircuitLayout",
        {
            "source_id": "CircuitLayoutID",
            "name": "CircuitName",
            "port_a_id": "PortA",
            "port_z_id": "PortZ",
        },
    )
    assert resources["circuit_layout_items"] == ResourceMapping(
        "CircuitLayoutItem",
        {
            "source_id": "CircuitLayoutItemID",
            "circuit_layout_id": "CircuitLayoutID",
            "position": "ItemIndex",
            "item_type": "TableName",
            "item_source_id": "TablePK",
        },
    )
    assert resources["patches"].columns["port_a_id"] == "PortID_A"
    assert resources["patches"].columns["port_b_secondary_id"] == "PortID_B_sec"
    assert resources["pseudowires"] == ResourceMapping(
        "PseudoWire",
        {
            "source_id": "PseudoWireID",
            "pseudowire_id": "PWID",
            "port_a_id": "PortAID",
            "port_b_id": "PortBID",
        },
    )


def test_source_database_fetch_matching_filters_and_orders_mapped_fields(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nettool.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE path_items (layout_id INTEGER, item_order INTEGER, private TEXT)"
        )
        connection.executemany(
            "INSERT INTO path_items VALUES (?, ?, ?)",
            [(9, 20, "secret"), (9, 10, "secret"), (10, 1, "secret")],
        )

    source = SourceDatabase(
        SQLiteSourceDatabaseSettings(database),
        SourceMapping(
            {
                "circuit_layout_items": ResourceMapping(
                    "path_items",
                    {
                        "circuit_layout_id": "layout_id",
                        "position": "item_order",
                    },
                )
            }
        ),
    )

    assert source.fetch_matching(
        "circuit_layout_items",
        {"circuit_layout_id": 9},
        order_by="position",
    ) == [
        {"circuit_layout_id": 9, "position": 10},
        {"circuit_layout_id": 9, "position": 20},
    ]


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.executions.append((query, parameters))

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"source_id": 7}]

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_mysql_source_uses_tls_and_quoted_mapped_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    connect_arguments: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> _FakeConnection:
        connect_arguments.update(kwargs)
        return connection

    monkeypatch.setattr("open_carrier_portal.source_database.pymysql.connect", fake_connect)
    source = SourceDatabase(
        MySQLSourceDatabaseSettings(
            host="nettool-db.internal",
            port=3306,
            database="nettool",
            username="ocp_reader",
            password="secret",
            tls_ca=ca_file,
        ),
        SourceMapping({"circuits": ResourceMapping("LitCircuit", {"source_id": "LitCircuitID"})}),
    )

    assert source.fetch("circuits", limit=25) == [{"source_id": 7}]
    assert cursor.executions == [
        ("SELECT `LitCircuitID` AS `source_id` FROM `LitCircuit` LIMIT %s", (25,))
    ]
    assert connect_arguments["ssl"] == {
        "ca": str(ca_file),
        "check_hostname": True,
    }
    assert connect_arguments["ssl_disabled"] is False
    assert connect_arguments["autocommit"] is True
    assert connection.closed is True


def test_remote_mysql_source_requires_tls_or_explicit_insecure_opt_in() -> None:
    with pytest.raises(ConfigurationError, match="TLS CA"):
        MySQLSourceDatabaseSettings(
            host="nettool-db.internal",
            port=3306,
            database="nettool",
            username="ocp_reader",
            password="secret",
        )

    settings = MySQLSourceDatabaseSettings(
        host="nettool-db.internal",
        port=3306,
        database="nettool",
        username="ocp_reader",
        password="test-password",
        allow_insecure=True,
    )
    assert settings.allow_insecure is True


def test_insecure_remote_mysql_explicitly_disables_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    connect_arguments: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> _FakeConnection:
        connect_arguments.update(kwargs)
        return connection

    monkeypatch.setattr("open_carrier_portal.source_database.pymysql.connect", fake_connect)
    source = SourceDatabase(
        MySQLSourceDatabaseSettings(
            host="nettool-db.internal",
            port=3306,
            database="nettool",
            username="ocp_reader",
            password="test-password",
            allow_insecure=True,
        ),
        SourceMapping({"circuits": ResourceMapping("LitCircuit", {"source_id": "LitCircuitID"})}),
    )

    source.health_check()

    assert connect_arguments["ssl"] is None
    assert connect_arguments["ssl_disabled"] is True


def test_remote_mysql_rejects_conflicting_tls_and_insecure_settings(
    tmp_path: Path,
) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        MySQLSourceDatabaseSettings(
            host="nettool-db.internal",
            port=3306,
            database="nettool",
            username="ocp_reader",
            password="test-password",
            tls_ca=ca_file,
            allow_insecure=True,
        )


def test_profile_configuration_resolves_password_from_environment(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    source = configured_source_database(
        driver="mysql",
        host="nettool-db.internal",
        port=3306,
        database_name="nettool",
        username="ocp_reader",
        password_env_var="NETTOOL_PASSWORD",
        tls_ca_path=str(ca_file),
        mapping_json=SourceMapping.nettool_defaults().to_json(),
        environment={"NETTOOL_PASSWORD": "secret"},
    )

    assert source.driver == "mysql"


def test_profile_configuration_accepts_explicit_insecure_opt_in() -> None:
    source = configured_source_database(
        driver="mysql",
        host="nettool-db.internal",
        port=3306,
        database_name="nettool",
        username="ocp_reader",
        password_env_var="NETTOOL_PASSWORD",
        mapping_json=SourceMapping.nettool_defaults().to_json(),
        environment={"NETTOOL_PASSWORD": "secret"},
        allow_insecure=True,
    )

    assert source.driver == "mysql"


def test_profile_configuration_requires_password_environment(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="NETTOOL_PASSWORD"):
        configured_source_database(
            driver="mysql",
            host="nettool-db.internal",
            port=3306,
            database_name="nettool",
            username="ocp_reader",
            password_env_var="NETTOOL_PASSWORD",
            tls_ca_path=str(ca_file),
            mapping_json=SourceMapping.nettool_defaults().to_json(),
            environment={},
        )
