from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from open_carrier_portal.cli import main
from open_carrier_portal.repository import Repository


def _create_nettool_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE Location (LocationID INTEGER, Name TEXT, Address TEXT)")
        connection.execute("CREATE TABLE Device (DeviceID INTEGER, Name TEXT, ObserviumID INTEGER)")
        connection.execute(
            """
            CREATE TABLE Port (
                PortID INTEGER, DeviceID INTEGER, Name TEXT, ObserviumID INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE LitCircuit (
                LitCircuitID INTEGER, BillingCompanyID INTEGER, Description TEXT,
                CircuitLayoutID INTEGER, ALocationID INTEGER, ZLocationID INTEGER
            )
            """
        )
        connection.execute(
            "CREATE TABLE CircuitLayout "
            "(CircuitLayoutID INTEGER, CircuitName TEXT, PortA INTEGER, PortZ INTEGER)"
        )
        connection.execute(
            """
            CREATE TABLE CircuitLayoutItem (
                CircuitLayoutItemID INTEGER, CircuitLayoutID INTEGER, ItemIndex INTEGER,
                TableName TEXT, TablePK INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE Patch (
                PatchID INTEGER, PatchType INTEGER, PortID_A INTEGER, PortID_B INTEGER,
                PortID_A_sec INTEGER, PortID_B_sec INTEGER, DarkFiberCircuitID INTEGER,
                OrderDate TEXT, InstallDate TEXT, CancelDate TEXT, RemovalDate TEXT,
                needsRemoval INTEGER, Length INTEGER
            )
            """
        )
        connection.execute(
            "CREATE TABLE PseudoWire "
            "(PseudoWireID INTEGER, PWID INTEGER, PortAID INTEGER, PortBID INTEGER)"
        )


def test_configure_and_test_sqlite_source_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata_database = tmp_path / "ocp.db"
    source_database = tmp_path / "nettool.db"
    _create_nettool_database(source_database)
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
        "devices",
        "locations",
        "patches",
        "ports",
        "pseudowires",
    ]


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
