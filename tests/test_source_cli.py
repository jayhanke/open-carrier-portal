from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from npdb_fixture import create_npdb

from open_carrier_portal.cli import main
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import SourceMapping


def _create_nettool_database(path: Path) -> None:
    create_npdb(path)


def test_configure_and_test_sqlite_source_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata_database = tmp_path / "ocp.db"
    source_database = tmp_path / "nettool.db"
    _create_nettool_database(source_database)
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(SourceMapping.sharing_defaults().to_json(), encoding="utf-8")
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata_database))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "configure-source",
            "--driver",
            "sqlite",
            "--database",
            str(source_database),
            "--mapping-file",
            str(mapping_file),
        ],
    )

    main()
    configured = json.loads(capsys.readouterr().out)

    assert configured["name"] == "nettool"
    assert configured["password_env"] is None

    monkeypatch.setattr(sys, "argv", ["ocp", "test-source"])
    main()
    tested = json.loads(capsys.readouterr().out)

    assert tested["status"] == "ok"
    assert tested["mapped_resources"] == [
        "circuit_layout_items",
        "circuit_layouts",
        "circuits",
        "companies",
        "devices",
        "locations",
        "new_dark_fiber_circuits",
        "patches",
        "ports",
        "pseudowires",
        "racks",
        "services",
    ]


def test_new_source_is_unmapped_and_connection_updates_preserve_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    metadata = tmp_path / "metadata.db"
    source = tmp_path / "customer.db"
    _create_nettool_database(source)
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata))
    command = ["ocp", "configure-source", "--driver", "sqlite", "--database", str(source)]
    monkeypatch.setattr(sys, "argv", command)
    main()
    configured = json.loads(capsys.readouterr().out)
    assert configured["mapped_resources"] == []
    repository = Repository(metadata, "k" * 32)
    assert json.loads(repository.get_source_database_profile("nettool").mapping_json) == {
        "resources": {}
    }
    monkeypatch.setattr(sys, "argv", ["ocp", "test-source"])
    main()
    tested = json.loads(capsys.readouterr().out)
    assert tested["ocp_contract_ready"] is False
    assert tested["mapped_resources"] == []
    assert set(tested["missing_fields"]) == {
        "circuits",
        "circuit_layouts",
        "ports",
        "devices",
        "racks",
        "locations",
    }
    mapping_file = tmp_path / "explicit.json"
    mapping_file.write_text(SourceMapping.sharing_defaults().to_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [*command, "--mapping-file", str(mapping_file)])
    main()
    capsys.readouterr()
    explicit = repository.get_source_database_profile("nettool").mapping_json
    monkeypatch.setattr(sys, "argv", command)
    main()
    capsys.readouterr()
    assert repository.get_source_database_profile("nettool").mapping_json == explicit


@pytest.mark.parametrize("monitoring", [False, True])
def test_source_readiness_requires_monitoring_mappings_only_when_configured(
    tmp_path, monkeypatch, capsys, monitoring
):
    from open_carrier_portal.admin_store import AdminStore

    metadata = tmp_path / "metadata.db"
    database = tmp_path / "source.db"
    create_npdb(database)
    mapping = json.loads(SourceMapping.sharing_defaults().to_json())
    for resource in ("devices", "ports"):
        del mapping["resources"][resource]["columns"]["observium_id"]
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata))
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "k" * 32)
    monkeypatch.delenv("OCP_OBSERVIUM_BASE_URL", raising=False)
    monkeypatch.delenv("OCP_OBSERVIUM_API_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "configure-source",
            "--driver",
            "sqlite",
            "--database",
            str(database),
            "--mapping-file",
            str(mapping_file),
        ],
    )
    main()
    capsys.readouterr()
    if monitoring:
        store = AdminStore(metadata)
        store.initialize()
        store.save(
            "observium", {"url": "https://monitoring.example", "token_env": "MISSING_TEST_TOKEN"}
        )
    monkeypatch.setattr(sys, "argv", ["ocp", "test-source"])
    main()
    tested = json.loads(capsys.readouterr().out)
    assert tested["ocp_contract_ready"] is not monitoring
    assert tested["missing_fields"] == (
        {"devices": ["observium_id"], "ports": ["observium_id"]} if monitoring else {}
    )


def test_configure_source_requires_explicit_insecure_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata_database = tmp_path / "ocp.db"
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata_database))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "configure-source",
            "--driver",
            "mysql",
            "--host",
            "10.210.4.229",
            "--database",
            "NPDB",
            "--username",
            "ocp_reader",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "--allow-insecure" in capsys.readouterr().err

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "configure-source",
            "--driver",
            "mysql",
            "--host",
            "10.210.4.229",
            "--database",
            "NPDB",
            "--username",
            "ocp_reader",
            "--allow-insecure",
        ],
    )
    main()
    configured = json.loads(capsys.readouterr().out)

    assert configured["allow_insecure"] is True
    profile = Repository(metadata_database, "k" * 32).get_source_database_profile("nettool")
    assert profile is not None
    assert profile.allow_insecure is True


def test_share_and_revoke_circuit_cli_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata_database = tmp_path / "ocp.db"
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("OCP_DATABASE_PATH", str(metadata_database))
    repository = Repository(metadata_database, "k" * 32)
    repository.initialize()
    partner_id = repository.create_partner("Circuit partner")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "share-circuit",
            "--partner-id",
            partner_id,
            "--circuit-source-id",
            "926",
            "--visibility",
            "sfn_path",
        ],
    )
    main()
    shared = json.loads(capsys.readouterr().out)

    assert shared["status"] == "active"
    assert shared["visibility"] == "sfn_path"
    assert shared["circuit_id"] != "926"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ocp",
            "revoke-circuit-share",
            "--partner-id",
            partner_id,
            "--circuit-source-id",
            "926",
        ],
    )
    main()
    revoked = json.loads(capsys.readouterr().out)

    assert revoked["status"] == "revoked"
