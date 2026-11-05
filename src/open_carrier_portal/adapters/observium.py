from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from open_carrier_portal.adapters.base import MonitoringAdapter
from open_carrier_portal.diagnostics import log_event, log_failure, target_origin
from open_carrier_portal.errors import (
    CapabilityNotSupportedError,
    IntegrationUnavailableError,
    RateLimitError,
    ResourceNotFoundError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)
from open_carrier_portal.models import (
    Alarm,
    CapabilitySet,
    Device,
    Interface,
    MetricSeries,
    Page,
)
from open_carrier_portal.repository import Repository


class ObserviumAdapter(MonitoringAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        repository: Repository,
        timeout_seconds: int = 15,
        ca_bundle: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        verify: bool | str = ca_bundle or True
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "User-Agent": "open-carrier-portal/0.1",
            },
            timeout=httpx.Timeout(timeout_seconds),
            verify=verify,
            transport=transport,
        )
        self._repository = repository
        self._target = target_origin(base_url)

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> None:
        payload = await self._request_json(
            "api/v0/devices/",
            params={"pageno": 1, "pagesize": 1},
            health_check=True,
        )
        self._records(payload, "devices")

    async def capabilities(self) -> CapabilitySet:
        await self.health_check()
        return CapabilitySet(
            adapter="observium",
            adapter_version="v0",
            resources=["devices", "interfaces", "alarms"],
            metrics=[],
        )

    async def list_devices(self, *, page: int, page_size: int) -> Page[Device]:
        payload = await self._request_json(
            "api/v0/devices/",
            params={"pageno": page, "pagesize": page_size},
        )
        records = self._records(payload, "devices")
        items = [self._map_device(record) for record in records]
        return Page(items=items, next_cursor=str(page + 1) if len(items) == page_size else None)

    async def get_device(self, device_id: str) -> Device:
        source_id = self._repository.source_resource_id(device_id, "observium", "device")
        if source_id is None:
            raise ResourceNotFoundError()
        payload = await self._request_json(f"api/v0/devices/{source_id}/")
        return self._map_device(self._detail_record(payload, "devices", "device"))

    async def list_interfaces(self, *, page: int, page_size: int) -> Page[Interface]:
        payload = await self._request_json(
            "api/v0/ports/",
            params={"pageno": page, "pagesize": page_size},
        )
        records = self._records(payload, "ports")
        items = [self._map_interface(record) for record in records]
        return Page(items=items, next_cursor=str(page + 1) if len(items) == page_size else None)

    async def get_interface(self, interface_id: str) -> Interface:
        source_id = self._repository.source_resource_id(interface_id, "observium", "interface")
        if source_id is None:
            raise ResourceNotFoundError()
        payload = await self._request_json(f"api/v0/ports/{source_id}/")
        return self._map_interface(self._detail_record(payload, "ports", "port"))

    async def list_alarms(self, *, page: int, page_size: int) -> Page[Alarm]:
        payload = await self._request_json(
            "api/v0/alerts/",
            params={"status": "failed", "pageno": page, "pagesize": page_size},
        )
        records = self._records(payload, "alerts")
        items = [self._map_alarm(record) for record in records]
        return Page(items=items, next_cursor=str(page + 1) if len(items) == page_size else None)

    async def query_metrics(
        self,
        *,
        resource_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> MetricSeries:
        raise CapabilityNotSupportedError(
            "This Observium API does not advertise a numeric time-series capability"
        )

    async def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        health_check: bool = False,
    ) -> dict[str, Any]:
        operation = (
            "health"
            if health_check
            else "devices"
            if path.startswith("api/v0/devices/")
            else "ports"
            if path.startswith("api/v0/ports/")
            else "alerts"
            if path.startswith("api/v0/alerts/")
            else "other"
        )
        started = time.monotonic()
        log_event(
            "observium",
            "request.started",
            "Starting Observium health check (GET api/v0/devices/, page 1, page size 1)."
            if operation == "health"
            else "Starting Observium API request.",
            operation=operation,
            target=self._target,
        )
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            log_failure("observium", "request", exc, operation=operation, target=self._target)
            raise UpstreamTimeoutError() from None
        except httpx.RequestError as exc:
            log_failure("observium", "request", exc, operation=operation, target=self._target)
            raise IntegrationUnavailableError() from None
        status = response.status_code
        if status >= 300:
            detail_lookup = (
                not health_check and operation in {"devices", "ports"}
                and len(path.rstrip("/").split("/")) == 4
            )
            reason = (
                "credential_rejected"
                if status in {401, 403}
                else ("resource_missing" if detail_lookup else "endpoint_missing")
                if status == 404
                else "rate_limited"
                if status == 429
                else "upstream_failed"
                if status >= 500
                else "redirected"
                if status < 400
                else "http_error"
            )
            explanation = (
                "Observium rejected the token or its permissions."
                if status in {401, 403}
                else (
                    "Observium detail lookup returned 404; "
                    "check the mapped record ID and API route."
                    if detail_lookup
                    else "Observium API endpoint was not found; check the base URL and REST API."
                )
                if status == 404
                else "Observium returned an HTTP error or redirect; check its service and base URL."
            )
            log_event(
                "observium",
                f"request.{reason}",
                explanation,
                level=logging.ERROR,
                http_status=status,
                operation=operation,
                target=self._target,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        if 300 <= status < 400:
            raise UpstreamResponseError()
        if response.status_code == 404:
            raise ResourceNotFoundError()
        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise RateLimitError(retry_after)
        if response.status_code in {401, 403}:
            raise IntegrationUnavailableError("Observium rejected the connector credential")
        if response.status_code >= 500:
            raise IntegrationUnavailableError()
        if response.status_code >= 400:
            raise UpstreamResponseError()
        try:
            payload = response.json()
        except ValueError:
            log_event(
                "observium",
                "response.invalid_json",
                "Observium did not return JSON; check the API URL and login/proxy redirects.",
                level=logging.ERROR,
                operation=operation,
                http_status=status,
            )
            raise UpstreamResponseError() from None
        if not isinstance(payload, dict):
            log_event(
                "observium",
                "response.invalid_shape",
                "Observium returned an unexpected JSON shape.",
                level=logging.ERROR,
                operation=operation,
            )
            raise UpstreamResponseError()
        if payload.get("status") not in {None, "ok"}:
            log_event(
                "observium",
                "response.failed_status",
                "Observium JSON reported a non-success status; response contents were not logged.",
                level=logging.ERROR,
                operation=operation,
            )
            raise UpstreamResponseError()
        log_event(
            "observium",
            "request.completed",
            "Observium returned a successful JSON response.",
            operation=operation,
            target=self._target,
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return payload

    @staticmethod
    def _detail_record(
        payload: dict[str, Any], plural: str, singular: str
    ) -> dict[str, Any]:
        if plural not in payload and singular in payload:
            record = payload[singular]
            if not isinstance(record, dict):
                log_event(
                    "observium", "response.invalid_record",
                    "Observium returned an invalid detail record; contents were not logged.",
                    level=logging.ERROR,
                )
                raise UpstreamResponseError()
            if not record:
                raise ResourceNotFoundError()
            return record
        records = ObserviumAdapter._records(payload, plural)
        if not records:
            raise ResourceNotFoundError()
        if len(records) != 1:
            log_event(
                "observium", "response.ambiguous_detail",
                "Observium returned multiple records for a single-resource lookup.",
                level=logging.ERROR,
            )
            raise UpstreamResponseError()
        return records[0]

    @staticmethod
    def _records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        raw = payload.get(key)
        if raw is None and key == "alerts":
            raw = payload.get("alert")
        if isinstance(raw, dict):
            values = list(raw.values())
        elif isinstance(raw, list):
            values = raw
        else:
            log_event(
                "observium",
                "response.invalid_collection",
                "Observium returned an invalid record collection; contents were not logged.",
                level=logging.ERROR,
            )
            raise UpstreamResponseError()
        if not all(isinstance(record, dict) for record in values):
            log_event(
                "observium",
                "response.invalid_record",
                "Observium returned an invalid record shape; contents were not logged.",
                level=logging.ERROR,
            )
            raise UpstreamResponseError()
        return values

    def _map_device(self, record: dict[str, Any]) -> Device:
        source_id = _required_id(record, "device_id")
        return Device(
            id=self._repository.ocp_resource_id("observium", "device", source_id),
            name=_required_string(record, "hostname"),
            type=_optional_string(record, "type"),
            vendor=_optional_string(record, "vendor"),
            model=_optional_string(record, "hardware"),
            operating_system=_optional_string(record, "os"),
            operational_status=_status(record.get("status")),
            site=_optional_string(record, "location") or _optional_string(record, "location_text"),
            last_observed_at=_optional_datetime(record.get("last_polled")),
        )

    def _map_interface(self, record: dict[str, Any]) -> Interface:
        source_id = _required_id(record, "port_id")
        device_source_id = _required_id(record, "device_id")
        return Interface(
            id=self._repository.ocp_resource_id("observium", "interface", source_id),
            device_id=self._repository.ocp_resource_id("observium", "device", device_source_id),
            name=(
                _optional_string(record, "ifName")
                or _optional_string(record, "ifDescr")
                or source_id
            ),
            description=_optional_string(record, "ifAlias"),
            speed_bps=_optional_nonnegative_int(record.get("ifSpeed")),
            administrative_status=_optional_string(record, "ifAdminStatus"),
            operational_status=_optional_string(record, "ifOperStatus")
            or _status(record.get("status")),
            optical_rx_dbm=_optical_power(record.get("sensor_rx_power"), "sensor_rx_power"),
            optical_tx_dbm=_optical_power(record.get("sensor_tx_power"), "sensor_tx_power"),
            last_observed_at=_optional_datetime(record.get("last_polled")),
        )

    def _map_alarm(self, record: dict[str, Any]) -> Alarm:
        source_id = _required_id(record, "alert_table_id", fallback="alert_id")
        entity_source_id = record.get("entity_id")
        resource_id = None
        if entity_source_id is not None:
            entity_type = str(record.get("entity_type", "entity"))
            resource_id = self._repository.ocp_resource_id(
                "observium", entity_type, str(entity_source_id)
            )
        return Alarm(
            id=self._repository.ocp_resource_id("observium", "alarm", source_id),
            resource_id=resource_id,
            severity=_optional_string(record, "severity") or "unknown",
            state=_optional_string(record, "status")
            or _optional_string(record, "alert_status")
            or "unknown",
            summary=(
                _optional_string(record, "message")
                or _optional_string(record, "alert_message")
                or "Observium alert"
            ),
            raised_at=_optional_datetime(record.get("last_changed") or record.get("timestamp")),
            cleared_at=_optional_datetime(record.get("cleared")),
        )


def _optical_power(value: object, field: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        reading = float(str(value))
        if not math.isfinite(reading):
            raise ValueError
    except (TypeError, ValueError):
        log_event(
            "observium", "response.optical_invalid",
            "An optical reading is invalid; it was omitted without discarding port status.",
            level=logging.WARNING, field=field,
        )
        return None
    return reading


def _required_id(
    record: dict[str, Any],
    key: str,
    *,
    fallback: str | None = None,
) -> str:
    value = record.get(key)
    if value is None and fallback is not None:
        value = record.get(fallback)
    if value is None or isinstance(value, (dict, list, bool)):
        raise UpstreamResponseError()
    return str(value)


def _required_string(record: dict[str, Any], key: str) -> str:
    value = _optional_string(record, key)
    if value is None:
        raise UpstreamResponseError()
    return value


def _optional_string(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise UpstreamResponseError()
    text = str(value).strip()
    return text or None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        raise UpstreamResponseError() from None
    if parsed < 0:
        raise UpstreamResponseError()
    return parsed


def _status(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        raise UpstreamResponseError()
    if value in {1, "1", "up", "ok"}:
        return "up"
    if value in {0, "0", "down", "failed"}:
        return "down"
    return str(value)


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "" or value == "0000-00-00 00:00:00":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str):
        raise UpstreamResponseError()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise UpstreamResponseError() from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return max(parsed, 0)
