from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from open_carrier_portal.adapters.base import MonitoringAdapter
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

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> None:
        await self._request_json("api/v0/openapi.json")

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
        records = self._records(payload, "devices")
        if not records:
            raise ResourceNotFoundError()
        return self._map_device(records[0])

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
        records = self._records(payload, "ports")
        if not records:
            raise ResourceNotFoundError()
        return self._map_interface(records[0])

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
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException:
            raise UpstreamTimeoutError() from None
        except httpx.RequestError:
            raise IntegrationUnavailableError() from None
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
            raise UpstreamResponseError() from None
        if not isinstance(payload, dict):
            raise UpstreamResponseError()
        if payload.get("status") not in {None, "ok"}:
            raise UpstreamResponseError()
        return payload

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
            raise UpstreamResponseError()
        if not all(isinstance(record, dict) for record in values):
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
