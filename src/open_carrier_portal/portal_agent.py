from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from open_carrier_portal.company_policies import PolicySnapshot
from open_carrier_portal.config import validate_portal_url
from open_carrier_portal.diagnostics import log_event, log_failure, target_origin
from open_carrier_portal.live_contract import MAX_RESULT_BYTES, LiveJob, LiveResult

if TYPE_CHECKING:
    from open_carrier_portal.config import Settings
    from open_carrier_portal.repository import Repository

DEFAULT_CONNECTION_FILE = Path.home() / ".open-carrier-portal" / "portal-connection.json"


@dataclass(frozen=True)
class PortalConnection:
    portal_url: str
    connector_id: str
    organization_id: str
    token: str

    @property
    def binding_id(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    self.portal_url,
                    self.organization_id,
                    self.connector_id,
                    self.token,
                ]
            ).encode()
        ).hexdigest()

    @classmethod
    def load(cls, path: Path, *, allow_insecure_http: bool = False) -> PortalConnection:
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
            validate_portal_url(data["portal_url"], allow_insecure_http=allow_insecure_http),
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
    allow_insecure_http: bool = False,
) -> PortalConnection:
    portal_url = validate_portal_url(portal_url, allow_insecure_http=allow_insecure_http)
    connector_id = str(UUID(connector_id))
    if not 40 <= len(code) <= 128:
        raise ValueError("invalid pairing code")
    if allow_insecure_http and portal_url.startswith("http://"):
        log_event(
            "portal",
            "pairing.insecure_http",
            "HTTP portal pairing sends the pairing code and portal credential without encryption.",
            level=logging.WARNING,
        )
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


def send_heartbeat(
    client: httpx.Client,
    connection: PortalConnection,
    *,
    allow_insecure_http: bool = False,
    repository: Repository | None = None,
) -> None:
    portal_url = validate_portal_url(connection.portal_url, allow_insecure_http=allow_insecure_http)
    log_event("portal", "heartbeat.started", "Sending portal heartbeat.")
    payload: dict[str, object] = {"connector_id": connection.connector_id}
    if repository is not None:
        payload.update(
            policy_version=1,
            policy_receipts=[
                receipt.model_dump(mode="json")
                for receipt in repository.portal_policy_receipts(connection)
            ],
        )
    response = client.post(
        f"{portal_url}/api/connectors/heartbeat",
        headers={"Authorization": f"Bearer {connection.token}"},
        json=payload,
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
    if repository is not None:
        try:
            snapshot = PolicySnapshot.model_validate(data.get("company_policies"))
            repository.synchronize_portal_policies(connection, snapshot)
        except ValueError:
            log_event(
                "portal",
                "policies.invalid_response",
                "Company policy synchronization failed. Update both apps and check pairing; "
                "response contents were not logged.",
                level=logging.ERROR,
            )
            raise ValueError("portal returned an invalid company policy snapshot") from None
    log_event("portal", "heartbeat.accepted", "Portal heartbeat accepted.")


def serve_live_request(
    client: httpx.Client,
    connection: PortalConnection,
    *,
    repository: Repository,
    settings: Settings,
    settings_loader: Callable[[], Settings] | None = None,
) -> bool:
    from open_carrier_portal.live_reader import execute_live_request, monitoring_for_live

    portal_url = validate_portal_url(
        connection.portal_url, allow_insecure_http=settings.allow_insecure_http
    )
    headers = {"Authorization": f"Bearer {connection.token}"}
    with client.stream(
        "POST",
        f"{portal_url}/api/connectors/live/poll",
        headers=headers,
        json={"connector_id": connection.connector_id, "wait_seconds": 5},
    ) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 16_384:
                raise ValueError("portal live request exceeds its metadata size limit")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {"request"}:
            raise ValueError("invalid live poll envelope")
        if payload["request"] is None:
            return False
        job = TypeAdapter(LiveJob).validate_python(payload["request"])
    except ValueError:
        raise ValueError("portal returned an invalid live request contract") from None

    async def execute() -> LiveResult:
        effective = settings
        if job.operation == "circuits.read" and settings_loader is not None:
            try:
                effective = settings_loader()
            except ValueError:
                log_event(
                    "portal",
                    "live.configuration_invalid",
                    "Live monitoring configuration is invalid; check Connections.",
                    level=logging.ERROR,
                )
                return LiveResult(request_id=job.request_id, error="unavailable")
        async with monitoring_for_live(
            effective, repository, enabled=job.operation == "circuits.read"
        ) as monitoring:
            return await execute_live_request(
                repository,
                connection,
                job,
                metadata_path=settings.database_path,
                monitoring=monitoring,
            )

    result = asyncio.run(execute())
    encoded = result.model_dump_json().encode()
    if len(encoded) > MAX_RESULT_BYTES:
        log_event(
            "portal",
            "live.result_too_large",
            "Live result exceeded the response size limit.",
            level=logging.ERROR,
        )
        encoded = (
            LiveResult(request_id=job.request_id, error="unavailable").model_dump_json().encode()
        )
    response = client.post(
        f"{portal_url}/api/connectors/live/results",
        params={"connector_id": connection.connector_id},
        headers={**headers, "Content-Type": "application/json"},
        content=encoded,
    )
    if response.status_code in {403, 409}:
        log_event(
            "portal",
            "live.result_obsolete",
            "Live request expired or was revoked; result discarded.",
            level=logging.WARNING,
        )
        return True
    response.raise_for_status()
    if response.json() != {"status": "accepted"}:
        raise ValueError("portal returned an invalid live result acknowledgement")
    return True


def run_agent(
    client: httpx.Client,
    connection: PortalConnection,
    *,
    once: bool = False,
    allow_insecure_http: bool = False,
    repository: Repository | None = None,
    settings: Settings | None = None,
    settings_loader: Callable[[], Settings] | None = None,
) -> None:
    next_heartbeat = 0.0
    while True:
        operation = "heartbeat"
        try:
            if time.monotonic() >= next_heartbeat:
                send_heartbeat(
                    client,
                    connection,
                    allow_insecure_http=allow_insecure_http,
                    repository=repository,
                )
                next_heartbeat = time.monotonic() + 30
            if once:
                return
            if repository is not None:
                operation = "live.transport"
                if settings is None:
                    raise ValueError("connector settings are required to serve live requests")
                serve_live_request(
                    client,
                    connection,
                    repository=repository,
                    settings=settings,
                    settings_loader=settings_loader,
                )
            else:
                time.sleep(30)
        except httpx.HTTPStatusError as exc:
            log_failure("portal", f"{operation}.failed", exc)
            if once or exc.response.status_code < 500:
                raise
            time.sleep(5)
        except httpx.RequestError as exc:
            log_failure("portal", f"{operation}.failed", exc)
            if once:
                raise
            time.sleep(5)
