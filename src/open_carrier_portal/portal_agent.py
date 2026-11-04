from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from open_carrier_portal.diagnostics import log_event, log_failure, target_origin

DEFAULT_CONNECTION_FILE = Path.home() / ".open-carrier-portal" / "portal-connection.json"


def validate_portal_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("portal URL must be an HTTPS origin (HTTP allowed only on localhost)")
    return value.rstrip("/")


@dataclass(frozen=True)
class PortalConnection:
    portal_url: str
    connector_id: str
    organization_id: str
    token: str

    @classmethod
    def load(cls, path: Path) -> PortalConnection:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("connection file must be private; run chmod 600 on it")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str)
            for key in ("portal_url", "connector_id", "organization_id", "token")
        ):
            raise ValueError("invalid portal connection file; pair the connector again")
        token = data["token"]
        if not 40 <= len(token) <= 128 or not token.isascii() or not token.isprintable():
            raise ValueError("invalid portal connection credential")
        return cls(
            validate_portal_url(data["portal_url"]),
            str(UUID(data["connector_id"])),
            str(UUID(data["organization_id"])),
            token,
        )


def pair_portal(
    *,
    client: httpx.Client,
    portal_url: str,
    connector_id: str,
    code: str,
    connection_file: Path,
) -> PortalConnection:
    portal_url = validate_portal_url(portal_url)
    connector_id = str(UUID(connector_id))
    if not 40 <= len(code) <= 128:
        raise ValueError("invalid pairing code")
    log_event(
        "portal",
        "pairing.started",
        "Starting portal pairing.",
        target=target_origin(portal_url),
    )
    connection_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(connection_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            response = client.post(
                f"{portal_url}/api/connectors/pair",
                json={"code": code, "connector_id": connector_id},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not all(
                isinstance(data.get(key), str) for key in ("organization_id", "token")
            ):
                raise ValueError("portal returned an invalid pairing response")
            UUID(data["organization_id"])
            if not 40 <= len(data["token"]) <= 128:
                raise ValueError("portal returned an invalid connector credential")
            connection = PortalConnection(
                portal_url, connector_id, data["organization_id"], data["token"]
            )
            json.dump(
                {
                    "portal_url": connection.portal_url,
                    "connector_id": connection.connector_id,
                    "organization_id": connection.organization_id,
                    "token": connection.token,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
    except (httpx.HTTPError, ValueError, OSError) as exc:
        log_failure("portal", "pairing.failed", exc)
        connection_file.unlink()
        raise
    log_event("portal", "pairing.completed", "Portal pairing completed.")
    return connection


def send_heartbeat(client: httpx.Client, connection: PortalConnection) -> None:
    log_event("portal", "heartbeat.started", "Sending portal heartbeat.")
    response = client.post(
        f"{connection.portal_url}/api/connectors/heartbeat",
        headers={"Authorization": f"Bearer {connection.token}"},
        json={"connector_id": connection.connector_id},
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        log_event(
            "portal",
            "heartbeat.invalid_json",
            "Portal heartbeat did not return JSON; response contents were not logged.",
            level=logging.ERROR,
        )
        raise
    if not isinstance(data, dict) or data.get("status") != "connected":
        log_event(
            "portal",
            "heartbeat.invalid_response",
            "Portal returned an invalid heartbeat response; contents were not logged.",
            level=logging.ERROR,
        )
        raise ValueError("portal returned an invalid heartbeat response")
    log_event("portal", "heartbeat.accepted", "Portal heartbeat accepted.")


def run_agent(client: httpx.Client, connection: PortalConnection, *, once: bool = False) -> None:
    while True:
        try:
            send_heartbeat(client, connection)
        except httpx.HTTPStatusError as exc:
            log_failure("portal", "heartbeat.failed", exc)
            if once or exc.response.status_code < 500:
                raise
        except httpx.RequestError as exc:
            log_failure("portal", "heartbeat.failed", exc)
            if once:
                raise
        if once:
            return
        time.sleep(30)
