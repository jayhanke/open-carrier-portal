import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from open_carrier_portal.admin import create_admin_app
from open_carrier_portal.config import Settings
from open_carrier_portal.errors import ConfigurationError
from open_carrier_portal.source_database import (
    JoinMapping,
    MySQLSourceDatabaseSettings,
    ResourceMapping,
    SourceDatabase,
    SourceMapping,
    SQLiteSourceDatabaseSettings,
)


@pytest.fixture
def joined_source(tmp_path: Path):
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE sites (id INTEGER PRIMARY KEY, address TEXT, private TEXT);
            CREATE TABLE circuits (
                id INTEGER PRIMARY KEY, site_id INTEGER REFERENCES sites(id), label TEXT
            );
            INSERT INTO sites VALUES (10, 'Main street', 'never-export');
            INSERT INTO circuits VALUES (1, 10, 'A'), (2, NULL, 'B');
        """)
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "circuits",
                {"source_id": "base.id", "description": "j1.address"},
                (JoinMapping("sites", "j1", "base.site_id", "id"),),
            )
        }
    )
    return database, mapping


def test_joined_mapping_serialization_filtering_and_preview(joined_source):
    database, mapping = joined_source
    restored = SourceMapping.from_json(mapping.to_json())
    assert restored == mapping
    source = SourceDatabase(SQLiteSourceDatabaseSettings(database), restored)
    source.validate_mapping()
    assert source.fetch("circuits") == [
        {"source_id": 1, "description": "Main street"},
        {"source_id": 2, "description": None},
    ]
    assert source.fetch_matching(
        "circuits", {"description": "Main street"}, order_by="source_id"
    ) == [{"source_id": 1, "description": "Main street"}]
    assert source.fetch_matching("circuits", {"description": "' OR 1=1 --"}) == []
    assert "LEFT JOIN" in source.preview_sql("circuits")
    assert "private" not in source.preview_sql("circuits")
    assert source.describe_relationships() == [
        {
            "source_table": "circuits",
            "source_column": "site_id",
            "target_table": "sites",
            "target_column": "id",
        }
    ]


def test_legacy_rack_mappings_normalize_and_preserve_joins():
    legacy = {
        "resources": {
            "company_locations": {
                "table": "CompanyLocation",
                "columns": {"source_id": "CompanyLocationID", "name": "RackName"},
            },
            "rack_units": {
                "table": "RackUnit",
                "columns": {"source_id": "RackUnitID", "company_location_id": "CompanyLocationID"},
            },
            "devices": {
                "table": "Device",
                "columns": {"source_id": "base.DeviceID", "company_location_id": "j1.id"},
                "joins": [
                    {
                        "table": "racks",
                        "alias": "j1",
                        "left": "base.rack",
                        "right": "id",
                    }
                ],
            },
        }
    }
    restored = SourceMapping.from_json(json.dumps(legacy))
    assert set(restored.resources) == {"racks", "rack_positions", "devices"}
    assert restored.resources["rack_positions"].columns["rack_id"] == "CompanyLocationID"
    assert restored.resources["devices"].columns["rack_id"] == "j1.id"
    assert restored.resources["devices"].joins == (JoinMapping("racks", "j1", "base.rack", "id"),)
    assert restored == SourceMapping.from_json(restored.to_json())
    assert "company_locations" in legacy["resources"]


@pytest.mark.parametrize(
    "resources",
    [
        {
            "company_locations": ResourceMapping("old_racks", {"source_id": "id"}),
            "racks": ResourceMapping("new_racks", {"source_id": "id"}),
        },
        {
            "rack_units": ResourceMapping("old_positions", {"source_id": "id"}),
            "rack_positions": ResourceMapping("new_positions", {"source_id": "id"}),
        },
        {
            "devices": ResourceMapping(
                "Device",
                {"source_id": "id", "rack_id": "new_rack", "company_location_id": "old_rack"},
            ),
        },
    ],
)
def test_ambiguous_legacy_and_current_rack_mappings_fail_explicitly(resources):
    with pytest.raises(ConfigurationError, match="duplicate legacy/current"):
        SourceMapping(resources)


def test_inner_join_and_many_to_one_chain(joined_source):
    database, _ = joined_source
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "circuits",
                {"source_id": "id", "description": "j2.address"},
                (
                    JoinMapping("sites", "j1", "base.site_id", "id", "inner"),
                    JoinMapping("sites", "j2", "j1.id", "id"),
                ),
            )
        }
    )
    source = SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping)
    assert source.fetch("circuits") == [{"source_id": 1, "description": "Main street"}]


def test_join_targets_must_be_unique(joined_source):
    database, _ = joined_source
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "circuits",
                {"source_id": "id", "description": "j1.address"},
                (JoinMapping("sites", "j1", "base.label", "address"),),
            )
        }
    )
    source = SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping)
    with pytest.raises(ConfigurationError, match="primary or unique key"):
        source.fetch("circuits")
    with pytest.raises(ConfigurationError, match="primary or unique key"):
        source.validate_mapping()


@pytest.mark.parametrize(
    "constraint",
    [
        "CREATE UNIQUE INDEX site_address ON sites(address, private)",
        "CREATE UNIQUE INDEX site_address ON sites(address) WHERE address IS NOT NULL",
    ],
)
def test_composite_and_partial_unique_keys_are_not_join_targets(joined_source, constraint):
    database, _ = joined_source
    with sqlite3.connect(database) as db:
        db.execute(constraint)
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "circuits",
                {"source_id": "id", "description": "j1.address"},
                (JoinMapping("sites", "j1", "base.label", "address"),),
            )
        }
    )
    with pytest.raises(ConfigurationError, match="primary or unique key"):
        SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping).validate_mapping()


def test_single_unique_join_target_is_supported(joined_source):
    database, _ = joined_source
    with sqlite3.connect(database) as db:
        db.execute("CREATE UNIQUE INDEX site_address ON sites(address)")
    mapping = SourceMapping(
        {
            "circuits": ResourceMapping(
                "circuits",
                {"source_id": "id", "description": "j1.address"},
                (JoinMapping("sites", "j1", "base.label", "address"),),
            )
        }
    )
    SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping).validate_mapping()


def test_composite_foreign_keys_are_not_simplified(joined_source):
    database, mapping = joined_source
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE composite_parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b));
            CREATE TABLE composite_child (x INTEGER, y INTEGER,
                FOREIGN KEY(x, y) REFERENCES composite_parent(a, b));
        """)
    source = SourceDatabase(SQLiteSourceDatabaseSettings(database), mapping)
    assert len(source.describe_relationships()) == 1


def test_mysql_join_compiler_and_foreign_key_discovery(joined_source, monkeypatch):
    _, mapping = joined_source
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchall.side_effect = [
        [{"INDEX_NAME": "PRIMARY"}],
        [{"source_id": 1, "description": "Joined"}],
    ]
    monkeypatch.setattr(
        "open_carrier_portal.source_database.pymysql.connect",
        MagicMock(return_value=connection),
    )
    source = SourceDatabase(
        MySQLSourceDatabaseSettings(
            host="localhost",
            port=3306,
            database="sample",
            username="reader",
            password="test",
        ),
        mapping,
    )
    assert source.fetch_matching(
        "circuits",
        {"description": "' OR 1=1 --"},
        order_by="source_id",
        limit=5,
    ) == [{"source_id": 1, "description": "Joined"}]
    query, parameters = cursor.execute.call_args.args
    assert query == (
        "SELECT `base`.`id` AS `source_id`, `j1`.`address` AS `description` "
        "FROM `circuits` AS `base` LEFT JOIN `sites` AS `j1` "
        "ON `base`.`site_id` = `j1`.`id` "
        "WHERE `j1`.`address` = %s ORDER BY `base`.`id` LIMIT %s"
    )
    assert parameters == ("' OR 1=1 --", 6)
    assert "NON_UNIQUE = 0" in cursor.execute.call_args_list[0].args[0]
    cursor.fetchall.side_effect = None
    cursor.fetchall.return_value = [
        {
            "source_table": "circuits",
            "source_column": "site_id",
            "target_table": "sites",
            "target_column": "id",
            "relation_id": "single",
        },
        {
            "source_table": "circuits",
            "source_column": "a",
            "target_table": "sites",
            "target_column": "x",
            "relation_id": "composite",
        },
        {
            "source_table": "circuits",
            "source_column": "b",
            "target_table": "sites",
            "target_column": "y",
            "relation_id": "composite",
        },
    ]
    assert source.describe_relationships() == [
        {
            "source_table": "circuits",
            "source_column": "site_id",
            "target_table": "sites",
            "target_column": "id",
        }
    ]


@pytest.mark.parametrize(
    "join",
    [
        {"table": "sites;DELETE", "alias": "j1", "left": "base.site_id", "right": "id"},
        {"table": "sites", "alias": "base", "left": "base.site_id", "right": "id"},
        {"table": "sites", "alias": "j1", "left": "missing.site_id", "right": "id"},
        {"table": "sites", "alias": "j1", "left": "base.site_id", "right": "id", "kind": "CROSS"},
    ],
)
def test_join_validation_rejects_untrusted_sql(join):
    with pytest.raises(ConfigurationError):
        SourceMapping.from_json(
            json.dumps(
                {
                    "resources": {
                        "circuits": {
                            "table": "circuits",
                            "columns": {"source_id": "id"},
                            "joins": [join],
                        }
                    }
                }
            )
        )


def test_workspace_schema_auth_preview_and_save(joined_source, tmp_path):
    from test_admin import BASE, csrf, login

    database, mapping = joined_source
    settings = Settings(tmp_path / "metadata.db", "http://localhost:8000", "test", "s" * 48)
    app = create_admin_app(settings)
    app.state.admin_store.set_password("operator", "operator-password")
    repo = app.state.repository
    repo.put_source_database_profile(
        name="sample",
        driver="sqlite",
        database_name=str(database),
        host=None,
        port=None,
        username=None,
        password_env_var=None,
        tls_ca_path=None,
        timeout_seconds=10,
        mapping_json=SourceMapping(
            {"circuits": ResourceMapping("circuits", {"source_id": "id"})}
        ).to_json(),
    )
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/admin/mapping/data?profile=sample").status_code == 401
        assert client.post("/admin/mapping/workspace", data={}).status_code == 401
        login(client)
        board = client.get("/admin/mapping?profile=sample")
        assert board.status_code == 200
        assert "<h3>Linked database tables</h3>" in board.text
        assert "<h3>OCP tables " in board.text
        assert 'id="map-target-count"' in board.text
        assert 'id="map-target-search"' in board.text
        assert 'id="map-slots" class="mapper-targets"' in board.text
        assert 'id="map-resource"' not in board.text
        assert 'id="map-group"' not in board.text
        schema = client.get("/admin/mapping/data?profile=sample").json()
        assert schema["relationships"][0]["source_column"] == "site_id"
        assert "never-export" not in json.dumps(schema)
        data = {
            "csrf": csrf(client),
            "name": "sample",
            "resource": "circuits",
            "operation": "preview",
            "mapping": mapping.to_json(),
        }
        assert (
            client.post("/admin/mapping/workspace", data={**data, "csrf": "wrong"}).status_code
            == 403
        )
        assert (
            client.post(
                "/admin/mapping/workspace", data=data, headers={"Origin": "https://attacker.test"}
            ).status_code
            == 403
        )
        response = client.post("/admin/mapping/workspace", data=data)
        assert response.status_code == 200
        assert response.json()["rows"][0] == {"source_id": "1", "description": "Main street"}
        assert "never-export" not in response.text
        assert "joins" not in repo.get_source_database_profile("sample").mapping_json
        with sqlite3.connect(database) as db:
            db.executemany(
                "INSERT INTO circuits VALUES (?, 10, 'extra')",
                [(value,) for value in range(3, 13)],
            )
        response = client.post("/admin/mapping/workspace", data=data)
        assert len(response.json()["rows"]) == 5
        assert response.json()["sql"].endswith("LIMIT ?")
        original = repo.get_source_database_profile("sample").mapping_json
        invalid = json.loads(mapping.to_json())
        invalid["resources"]["circuits"]["joins"][0]["right"] = "address"
        response = client.post(
            "/admin/mapping/workspace",
            data={**data, "operation": "save", "mapping": json.dumps(invalid)},
        )
        assert response.status_code == 400
        assert "primary or unique key" in response.json()["error"]
        assert repo.get_source_database_profile("sample").mapping_json == original
        data["operation"] = "save"
        assert client.post("/admin/mapping/workspace", data=data).status_code == 200
        assert (
            SourceMapping.from_json(repo.get_source_database_profile("sample").mapping_json)
            == mapping
        )
        assert (
            client.get("/admin/assets/mapping.js")
            .headers["content-type"]
            .startswith("text/javascript")
        )
