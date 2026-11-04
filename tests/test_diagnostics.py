from __future__ import annotations

import json
import logging
import socket
import sqlite3
import ssl
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.diagnostics import (
    _SafeServerExceptions,
    failure_reason,
    install_request_logging,
    log_context,
    log_event,
    log_failure,
    request_reference,
    target_origin,
)


@pytest.fixture
def store(tmp_path: Path) -> AdminStore:
    result = AdminStore(tmp_path / "logs.db")
    result.initialize()
    return result


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ssl.SSLCertVerificationError(1, "private-error-content"), "tls_verification_failed"),
        (ssl.SSLError("private-error-content"), "tls_failed"),
        (socket.gaierror(-2, "private-error-content"), "dns_failed"),
        (ConnectionRefusedError("private-error-content"), "connection_refused"),
        (PermissionError("private-error-content"), "permission_denied"),
        (FileNotFoundError("private-error-content"), "file_missing"),
        (httpx.ReadTimeout("private-error-content"), "timeout"),
        (sqlite3.OperationalError("private-error-content"), "database_failed"),
    ],
)
def test_failure_categories_never_include_exception_messages(exc, reason):
    code, message = failure_reason(exc)
    assert code == reason
    assert "private-error-content" not in message


def test_wrapped_transport_error_is_actionable_without_secret_logging(store, caplog):
    exc = httpx.ConnectError("private-token")
    exc.__cause__ = ssl.SSLCertVerificationError(1, "private-upstream-body")
    with log_context(store) as context:
        log_failure(
            "observium",
            "request",
            exc,
            target=target_origin(
                "https://user:private-password@host.example:443/private-path?token=secret"
            ),
        )
    events = store.recent_events("observium")
    assert events[0]["event"] == "request.tls_verification_failed"
    assert events[0]["reference"] == context.request_id
    assert json.loads(events[0]["details"])["target"] == "https://host.example:443"
    for private in ("private-token", "private-upstream-body", "private-password", "private-path"):
        assert private not in json.dumps(events)
        assert private not in caplog.text


def test_contexts_do_not_mix_databases_or_references(store, tmp_path):
    other = AdminStore(tmp_path / "other.db")
    other.initialize()
    with log_context(store) as outer:
        log_event("app", "first", "First event.")
        with log_context(other) as inner:
            log_event("app", "second", "Second event.")
        assert request_reference() == outer.request_id
        log_event("app", "third", "Third event.")
    assert request_reference() == ""
    assert {row["reference"] for row in store.recent_events()} == {outer.request_id}
    assert {row["reference"] for row in other.recent_events()} == {inner.request_id}


def test_quiet_poll_context_retains_failures_but_not_success_noise(store):
    with log_context(store, quiet=True):
        log_event("observium", "health.passed", "Successful poll.")
        log_event("observium", "health.failed", "Failed poll.", level=logging.ERROR)
    assert [event["event"] for event in store.recent_events()] == ["health.failed"]


def test_operational_retention_is_bounded_and_does_not_prune_audit(store):
    with store.connection() as db:
        db.executemany(
            "INSERT INTO local_operational_logs "
            "(occurred, level, component, event, message, request_id, details) "
            "VALUES (0, 'INFO', 'app', ?, 'test', '', '{}')",
            [(str(index),) for index in range(2000)],
        )
    store.audit("test", "audit.must.remain")
    store.record_event(
        level="INFO",
        component="observium",
        event="new",
        message="New event.",
        request_id="test-reference",
        details={},
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM local_operational_logs").fetchone()[0] == 2000
        assert (
            db.execute("SELECT 1 FROM local_operational_logs WHERE event = '0'").fetchone() is None
        )
    assert len(store.recent_events()) == 100
    assert store.recent_events("observium")[0]["event"] == "new"
    assert store.recent_audit()[0]["operation"] == "audit.must.remain"


def test_persistence_failure_is_reported_safely_and_does_not_hide_operation(
    store, monkeypatch, caplog
):
    def fail(**_kwargs):
        raise sqlite3.OperationalError("private-storage-content")

    monkeypatch.setattr(store, "record_event", fail)
    with log_context(store):
        log_event("app", "test", "Test event.")
    assert "diagnostics.storage_failed" in caplog.text
    assert "private-storage-content" not in caplog.text


def test_request_logs_use_templates_and_never_headers_query_body_or_resource_ids(store):
    app = FastAPI()

    @app.get("/items/{item}")
    async def item(item: str):
        return {"private-response": item}

    @app.get("/crash")
    async def crash():
        raise RuntimeError("private-exception")

    install_request_logging(app, store)
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.get(
            "/items/private-path?token=private-query",
            headers={
                "Authorization": "Bearer private-authorization",
                "X-Request-ID": "private-supplied-reference",
            },
        )
        assert result.status_code == 200
        reference = result.headers["X-Request-ID"]
        UUID(reference)
        failed = client.get("/crash")
        assert failed.status_code == 500
        failed_reference = failed.headers["X-Request-ID"]
    events = store.recent_events()
    assert json.loads(events[-1]["details"])["route"] == "/items/{item}"
    assert events[-1]["reference"] == reference
    assert events[0]["event"] == "request.failed"
    assert events[0]["reference"] == failed_reference
    for value in (
        "private-path",
        "private-query",
        "private-authorization",
        "private-supplied-reference",
        "private-response",
        "private-exception",
    ):
        assert value not in json.dumps(events)


def test_server_exception_filter_preserves_stack_location_but_not_private_error():
    try:
        raise ValueError("private-exception")
    except ValueError:
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "private-server-message",
            (),
            sys.exc_info(),
        )
    assert _SafeServerExceptions().filter(record)
    assert "ValueError" in record.getMessage()
    assert "test_diagnostics.py" in record.getMessage()
    assert "private-exception" not in record.getMessage()
    assert "private-server-message" not in record.getMessage()
    assert record.exc_info is None


def test_cli_logs_outcome_not_arguments_or_signing_key(store, monkeypatch, capsys):
    from open_carrier_portal import cli

    monkeypatch.setenv("OCP_DATABASE_PATH", str(store.path))
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "private-signing-key" * 3)
    monkeypatch.setenv("OCP_OBSERVIUM_BASE_URL", "")
    monkeypatch.setattr(
        "sys.argv", ["ocp", "init", "--organization-name", "private-organization-name"]
    )
    cli.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["organization_name"] == "private-organization-name"
    events = store.recent_events("cli")
    assert {event["event"] for event in events} == {"command.started", "command.completed"}
    assert "private-organization-name" not in json.dumps(events)
    assert "private-signing-key" not in json.dumps(events)
    assert "private-organization-name" not in captured.err
    assert "private-signing-key" not in captured.err
