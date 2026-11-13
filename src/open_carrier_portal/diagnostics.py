from __future__ import annotations

import json
import logging
import socket
import sqlite3
import ssl
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from traceback import extract_tb
from urllib.parse import urlsplit

import httpx
import pymysql
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.errors import (
    ConnectorError,
    MissingSourceReferenceError,
    SourceDatabaseError,
)
from open_carrier_portal.interactions import (
    Interaction,
    attach_interaction,
    current_interaction,
    interaction_context,
    request_interaction,
)


@dataclass
class LogContext:
    store: AdminStore | None
    request_id: str
    is_request: bool = False
    quiet: bool = False


_context: ContextVar[LogContext | None] = ContextVar("connector_log_context", default=None)


class _SafeServerExceptions(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            kind, _, traceback = record.exc_info
            frames = [
                f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
                for frame in extract_tb(traceback)
            ]
            record.msg = "Unhandled server error (%s); stack=%s"
            record.args = (kind.__name__ if kind else "unknown", " -> ".join(frames))
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging() -> None:
    logger = logging.getLogger("open_carrier_portal")
    logger.setLevel(logging.INFO)
    if not any(handler.name == "connector-console" for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.name = "connector-console"
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    server = logging.getLogger("uvicorn.error")
    if not any(item.name == "connector-safe-errors" for item in server.filters):
        server.addFilter(_SafeServerExceptions("connector-safe-errors"))


@contextmanager
def log_context(
    store: AdminStore | None = None, *, is_request: bool = False, quiet: bool = False,
    request_id: str | None = None,
) -> Generator[LogContext]:
    parent = _context.get()
    context = LogContext(
        store or (parent.store if parent else None),
        request_id or str(uuid.uuid4()), is_request, quiet,
    )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def request_reference() -> str:
    context = _context.get()
    return context.request_id if context else ""


def log_event(
    component: str,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    store: AdminStore | None = None,
    **details: str | int,
) -> None:
    configure_logging()
    context = _context.get()
    if context is not None and context.quiet and level < logging.WARNING:
        return
    logger = logging.getLogger(f"open_carrier_portal.{component}")
    if not logger.isEnabledFor(level):
        return
    reference = request_reference() or str(uuid.uuid4())
    interaction = current_interaction()
    action_id = interaction.action_id if interaction else ""
    interaction_id = interaction.interaction_id if interaction else ""
    logger.log(
        level,
        "%s: %s reference=%s action_id=%s interaction_id=%s details=%s",
        event,
        message,
        reference or "-",
        action_id or "-",
        interaction_id or "-",
        json.dumps(details, sort_keys=True),
        extra={
            "event": event, "request_id": reference,
            "action_id": action_id, "interaction_id": interaction_id,
        },
    )
    store = store or (context.store if context else None)
    if store is not None:
        try:
            store.record_event(
                level=logging.getLevelName(level),
                component=component,
                event=event,
                message=message,
                request_id=reference,
                details=details,
                action_id=interaction.action_id if interaction else "",
                interaction_id=interaction.interaction_id if interaction else "",
            )
        except (sqlite3.Error, OSError) as exc:
            logger.error(
                "diagnostics.storage_failed: Cannot persist diagnostics; use service output. "
                "exception_type=%s",
                type(exc).__name__,
            )


def failure_reason(exc: BaseException) -> tuple[str, str]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 10 and not any(current is item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    for cause in chain:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return (
                "tls_verification_failed",
                "TLS certificate verification failed; check the CA bundle.",
            )
        if isinstance(cause, ssl.SSLError):
            return "tls_failed", "TLS negotiation failed; check HTTPS and certificate settings."
        if isinstance(cause, socket.gaierror):
            return "dns_failed", "Hostname resolution failed; check DNS and the configured host."
        if isinstance(cause, ConnectionRefusedError):
            return (
                "connection_refused",
                "Connection refused; check the listener, port and firewall.",
            )
        if isinstance(cause, PermissionError):
            return "permission_denied", "A required file or directory cannot be accessed."
        if isinstance(cause, FileNotFoundError):
            return "file_missing", "A required file is missing; check configured paths."
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)):
            return (
                "timeout",
                "The operation timed out; check connectivity and upstream availability.",
            )
        if isinstance(cause, pymysql.MySQLError) and cause.args and isinstance(cause.args[0], int):
            if cause.args[0] in {1044, 1045}:
                return (
                    "database_access_denied",
                    "Database authentication or permissions were rejected.",
                )
            if cause.args[0] in {1049, 1054, 1146}:
                return (
                    "database_schema_mismatch",
                    "A mapped database, table or column was not found.",
                )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return (
                "credential_rejected",
                "The upstream service rejected the credential or permission.",
            )
        return "http_error", f"The upstream service returned HTTP {status}."
    if isinstance(exc, httpx.RequestError):
        return "network_failed", "Network connection failed; check hostname, port, TLS and routing."
    if isinstance(exc, (SourceDatabaseError, sqlite3.Error, pymysql.MySQLError)):
        return (
            "database_failed",
            "Database operation failed; check connection, permissions and mapping.",
        )
    if isinstance(exc, ValidationError):
        return (
            "invalid_data",
            "Data does not match the expected schema; check the upstream or mapping.",
        )
    if isinstance(exc, MissingSourceReferenceError):
        return (
            exc.code,
            "A required source reference is missing or points to a record that was not found.",
        )
    if isinstance(exc, ConnectorError):
        return exc.code, "The connector operation was rejected or its upstream service failed."
    if isinstance(exc, (ValueError, httpx.InvalidURL)):
        return (
            "invalid_configuration",
            "Input or configuration is invalid; check the action's error.",
        )
    if isinstance(exc, OSError):
        return "file_or_network_failed", "A file or network operation failed."
    return "unexpected_error", "Unexpected application failure; check service diagnostics."


def log_failure(component: str, event: str, exc: BaseException, **details: str | int) -> None:
    reason, message = failure_reason(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        details["http_status"] = exc.response.status_code
    log_event(
        component,
        f"{event}.{reason}",
        message,
        level=logging.ERROR,
        exception_type=type(exc).__name__,
        **details,
    )


def record_data_issue(issues: list[str], message: str, *, event: str) -> None:
    """Record bounded, caller-authored warnings without logging raw source values."""
    if message in issues:
        return
    log_event("portal", event, message, level=logging.WARNING)
    if len(issues) < 19:
        issues.append(message)
    elif len(issues) == 19:
        issues.append("Additional data is unavailable; see connector Logs for details.")


def target_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return "invalid-target"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "invalid-target"


def install_request_logging(app: FastAPI, store: AdminStore) -> None:
    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _: Exception):
        reference = getattr(request.state, "request_id", str(uuid.uuid4()))
        interaction = getattr(request.state, "interaction", None)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected application failure; use X-Request-ID in Logs.",
                    "correlation_id": getattr(request.state, "correlation_id", reference),
                    "retry_after_seconds": None,
                }
            },
            status_code=500,
            headers={
                "X-Request-ID": reference, **(interaction.headers() if interaction else {}),
            },
        )

    @app.middleware("http")
    async def record_request(request: Request, call_next: RequestResponseEndpoint):
        existing = _context.get()
        if existing is not None and existing.is_request:
            return await call_next(request)
        invalid = False
        try:
            interaction = request_interaction(request, default="connector.direct")
        except ValueError:
            invalid = True
            interaction = Interaction(
                "connector.invalid-trace", str(uuid.uuid4()), scope=request.scope
            )
        request.state.interaction = interaction
        quiet = (
            request.url.path.startswith("/admin/assets/") or request.url.path in {
                "/health/live", "/health/ready", "/admin/session",
            }
        ) and (
            not interaction.explicit or interaction.action_id.endswith(".background")
        )
        with (
            interaction_context(interaction),
            log_context(
                store, is_request=True, quiet=quiet, request_id=interaction.request_id,
            ) as context,
        ):
            request.state.request_id = context.request_id
            started = time.monotonic()
            response = None
            try:
                response = (
                    JSONResponse({"error": "Invalid interaction identifiers."}, status_code=400)
                    if invalid else await call_next(request)
                )
                attach_interaction(response, request, interaction)
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                component = "admin" if request.url.path.startswith("/admin") else "api"
                status = response.status_code if response is not None else 500
                if status >= 400 or (
                    not quiet and request.url.path != "/admin/interactions"
                ):
                    exc = sys.exception()
                    await run_in_threadpool(
                        log_event,
                        component,
                        "request.failed" if status >= 400 else "request.completed",
                        "HTTP request failed." if status >= 400 else "HTTP request completed.",
                        level=logging.ERROR
                        if status >= 500
                        else (logging.WARNING if status >= 400 else logging.INFO),
                        method=request.method
                        if request.method
                        in {
                            "GET",
                            "POST",
                            "PUT",
                            "PATCH",
                            "DELETE",
                            "HEAD",
                            "OPTIONS",
                            "TRACE",
                            "CONNECT",
                        }
                        else "OTHER",
                        route=route,
                        http_status=status,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        exception_type=type(exc).__name__ if exc else "",
                    )
