from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.errors import CapabilityNotSupportedError, UpstreamResponseError
from open_carrier_portal.repository import Repository

pytestmark = pytest.mark.anyio


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    value = Repository(tmp_path / "test.db", "k" * 32)
    value.initialize()
    return value


async def test_maps_devices_and_keeps_token_in_authorization_header(
    repository: Repository,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer observium-secret"
        assert "observium-secret" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "count": 1,
                "devices": {
                    "277": {
                        "device_id": "277",
                        "hostname": "router.example",
                        "vendor": "Cisco",
                        "hardware": "CISCO1841",
                        "os": "ios",
                        "status": "1",
                        "last_polled": "2026-10-09 12:00:00",
                    }
                },
            },
        )

    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="observium-secret",
        repository=repository,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.list_devices(page=1, page_size=50)
    finally:
        await adapter.close()

    assert len(result.items) == 1
    assert result.items[0].name == "router.example"
    assert result.items[0].operational_status == "up"
    assert result.items[0].id.startswith("device_")
    assert "277" not in result.items[0].id


async def test_rejects_malformed_upstream_shape(repository: Repository) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            content=json.dumps({"status": "ok", "devices": "not-a-collection"}),
        )
    )
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="secret",
        repository=repository,
        transport=transport,
    )
    try:
        with pytest.raises(UpstreamResponseError):
            await adapter.list_devices(page=1, page_size=50)
    finally:
        await adapter.close()


async def test_numeric_metrics_are_not_fabricated(repository: Repository) -> None:
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="secret",
        repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(CapabilityNotSupportedError):
            await adapter.query_metrics(
                resource_id="interface_1",
                metric="bits_in",
                start=datetime(2026, 10, 9, tzinfo=UTC),
                end=datetime(2026, 10, 10, tzinfo=UTC),
                step_seconds=60,
            )
    finally:
        await adapter.close()
